"""Clip/video list filters (``include_cancelled``, ``used_in_videos``), the ``allow_mirror`` generation setting and
the Claude storyboard preview guard (lyrics rights + per-user rate limit)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Batch, Clip, CostRecord, Project, Video
from app.routers.generation import STORYBOARD_LYRICS_RIGHTS_WARNING, claude_preview_limiter
from app.security import SlidingWindowLimiter

# --- A5: lists --------------------------------------------------------------------------------------------------


def test_cancelled_items_can_be_hidden_and_do_not_count_as_usage(
    client: TestClient, db: Session, make_project: Callable[..., Project]
) -> None:
    project = make_project()
    batch = Batch(project_id=project.id, status="partial", settings={})
    db.add(batch)
    db.flush()
    ready = Clip(project_id=project.id, source="mock", provider="mock", status="ready")
    cancelled_clip = Clip(project_id=project.id, source="mock", provider="mock", status="cancelled")
    db.add_all([ready, cancelled_clip])
    db.flush()
    shots = {"shots": [{"clip_id": ready.id, "duration": 2.0}]}
    db.add_all(
        [
            Video(project_id=project.id, batch_id=batch.id, index=0, status="done", plan=dict(shots)),
            Video(project_id=project.id, batch_id=batch.id, index=1, status="cancelled", plan=dict(shots)),
            Video(project_id=project.id, batch_id=batch.id, index=2, status="failed", plan=dict(shots)),
        ]
    )
    db.commit()

    clips_url = f"/api/projects/{project.id}/clips"
    clips = client.get(clips_url).json()  # default: everything (backward compatible)
    assert {c["status"] for c in clips} == {"ready", "cancelled"}
    assert next(c for c in clips if c["id"] == ready.id)["used_in_videos"] == 2  # done + failed, not cancelled
    assert [c["id"] for c in client.get(clips_url, params={"include_cancelled": "false"}).json()] == [ready.id]

    videos_url = f"/api/projects/{project.id}/videos"
    assert [v["index"] for v in client.get(videos_url).json()] == [0, 1, 2]
    assert [v["index"] for v in client.get(videos_url, params={"include_cancelled": "false"}).json()] == [0, 2]
    batch_detail = client.get(f"/api/batches/{batch.id}").json()
    assert next(c for c in batch_detail["clips"] if c["id"] == ready.id)["used_in_videos"] == 2


# --- A6: allow_mirror -------------------------------------------------------------------------------------------


def test_allow_mirror_reaches_the_batch_and_the_planner(
    client: TestClient,
    ready_project: Project,
    sync_tasks: list,
    fake_clips: Any,
    fake_render: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import pipeline

    seen: list[dict[str, Any]] = []
    real = pipeline.plan_batch

    def capture(**kwargs: Any) -> dict[str, Any]:
        seen.append(dict(kwargs["settings"]))
        return real(**kwargs)

    monkeypatch.setattr(pipeline, "plan_batch", capture)
    settings = {"provider": "mock", "count": 1, "duration_sec": 5, "max_new_clips": 2, "allow_mirror": True}
    client.post(f"/api/projects/{ready_project.id}/estimate", json=settings)
    batch = client.post(f"/api/projects/{ready_project.id}/batches", json=settings).json()
    assert batch["settings"]["allow_mirror"] is True
    assert seen and all(s["allow_mirror"] is True for s in seen)  # estimate, batch preview and run_batch
    assert len(seen) >= 3

    seen.clear()
    default = client.post(f"/api/projects/{ready_project.id}/estimate", json={"provider": "mock"})
    assert default.status_code == 200 and seen[-1]["allow_mirror"] is False


def test_allow_mirror_is_advertised(client: TestClient) -> None:
    caps = client.get("/api/system/capabilities").json()
    (option,) = [o for o in caps["generation_options"] if o["id"] == "allow_mirror"]
    assert option["default"] is False and "鏡文字" in option["description_ja"]
    schema = client.get("/openapi.json").json()["components"]["schemas"]["GenerationSettings"]
    assert schema["properties"]["allow_mirror"]["default"] is False
    assert schema["properties"]["allow_mirror"]["type"] == "boolean"


# --- A7: Claude storyboard preview ---------------------------------------------------------------------------------


@pytest.fixture
def claude_client(make_client: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """A client with an Anthropic key configured; the storyboard service is stubbed (no network)."""
    from app.services import storyboard

    real = storyboard.generate_storyboard
    calls: list[bool] = []

    def fake(**kwargs: Any) -> dict[str, Any]:
        calls.append(bool(kwargs["use_claude"]))
        result = real(**{**kwargs, "use_claude": False})
        if kwargs["use_claude"]:
            result.update(source="claude", model="claude-opus-5-5", cost_usd=0.05,
                          usage={"input_tokens": 6000, "output_tokens": 1000})  # fmt: skip
        return result

    monkeypatch.setattr(storyboard, "generate_storyboard", fake)
    claude_preview_limiter.reset()
    client = make_client(ANTHROPIC_API_KEY="sk-ant-test-not-a-real-key")
    yield client, calls
    claude_preview_limiter.reset()


def test_claude_preview_requires_confirmed_lyrics_rights(
    claude_client: Any, db: Session, ready_project: Project, make_project: Callable[..., Project]
) -> None:
    client, calls = claude_client
    url = f"/api/projects/{ready_project.id}/storyboard"
    assert client.post(url, json={"scenes": 3}).json()["source"] == "claude" and calls == [True]

    ready_project.rights = {**ready_project.rights, "lyrics_rights": False}
    ready_project.rights_confirmed_at = None
    db.commit()
    body = client.post(url, json={"scenes": 3}).json()
    assert calls[-1] is False and body["source"] == "template" and body["cost_usd"] == 0
    assert body["warnings"][0] == STORYBOARD_LYRICS_RIGHTS_WARNING
    assert len(db.scalars(select(CostRecord)).all()) == 1  # only the Claude preview was booked

    no_lyrics = make_project()  # nothing of the lyrics would be sent
    assert client.post(f"/api/projects/{no_lyrics.id}/storyboard", json={"scenes": 3}).json()["source"] == "claude"


def test_claude_previews_are_rate_limited_per_user(claude_client: Any, db: Session, ready_project: Project) -> None:
    client, calls = claude_client
    url = f"/api/projects/{ready_project.id}/storyboard"
    for _ in range(10):
        assert client.post(url, json={"scenes": 1}).status_code == 200
    limited = client.post(url, json={"scenes": 1})
    assert limited.status_code == 429 and limited.json()["code"] == "rate_limited"
    assert "10 分間に 10 回" in limited.json()["detail"]
    assert 1 <= int(limited.headers["retry-after"]) <= 600
    assert calls == [True] * 10  # the refused request did not reach the service

    ready_project.rights = {**ready_project.rights, "lyrics_rights": False}
    db.commit()
    for _ in range(3):  # template previews are not limited
        response = client.post(url, json={"scenes": 1})
        assert response.status_code == 200 and response.json()["source"] == "template"


def test_sliding_window_limiter() -> None:
    now = [1000.0]
    limiter = SlidingWindowLimiter(2, 60, clock=lambda: now[0])
    assert limiter.acquire("a") is None and limiter.acquire("a") is None
    assert limiter.acquire("a") == 60 and limiter.acquire("b") is None  # per key
    now[0] += 59.5
    assert limiter.acquire("a") == 1
    now[0] += 0.5
    assert limiter.acquire("a") is None  # the first event left the window
