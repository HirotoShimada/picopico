"""§5.6 storyboard preview, estimate and batches (guards, run, cancel, ZIP)."""

from __future__ import annotations

import io
import zipfile
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Batch, CostRecord, Project

SETTINGS = {"provider": "mock", "count": 2, "duration_sec": 5, "max_new_clips": 2}


def _batches_url(project: Project) -> str:
    return f"/api/projects/{project.id}/batches"


def test_storyboard_preview_template(client: TestClient, ready_project: Project, db: Session) -> None:
    body = client.post(
        f"/api/projects/{ready_project.id}/storyboard", json={"preset": "urban_night", "scenes": 3}
    ).json()
    assert body["source"] == "template" and body["cost_usd"] == 0
    assert len(body["scenes"]) == 3 and body["world_bible"]["consistency_tokens_en"]
    for scene in body["scenes"]:
        assert "9:16" in scene["prompt_en"] and "no celebrities" in scene["prompt_en"]
        assert "デモアーティスト" not in scene["prompt_en"]
    assert db.scalars(select(CostRecord)).all() == []  # nothing billed, nothing stored


def test_storyboard_preview_records_llm_cost(
    client: TestClient, ready_project: Project, db: Session, monkeypatch: Any
) -> None:
    import app.services.storyboard as storyboard

    real = storyboard.generate_storyboard

    def billed(**kwargs: Any) -> dict[str, Any]:
        result = real(**{**kwargs, "use_claude": False})
        return {
            **result,
            "source": "claude",
            "model": "claude-opus-5-5",
            "cost_usd": 0.084,
            "usage": {"input_tokens": 6000, "output_tokens": 3000},
        }

    monkeypatch.setattr(storyboard, "generate_storyboard", billed)
    assert client.post(f"/api/projects/{ready_project.id}/storyboard", json={}).json()["source"] == "claude"
    (record,) = db.scalars(select(CostRecord)).all()
    assert record.kind == "llm" and record.provider == "anthropic" and record.amount_usd == pytest.approx(0.084)
    assert record.units == 9000 and record.batch_id is None


def test_estimate_mock_and_api_providers(client: TestClient, ready_project: Project) -> None:
    url = f"/api/projects/{ready_project.id}/estimate"
    mock = client.post(url, json=SETTINGS).json()
    assert mock["provider"] == "mock" and mock["model"] == "mock-v1" and mock["total_usd"] == 0
    assert mock["videos"] == 2 and mock["new_clips"] <= 2 and mock["within_budget"] is True
    assert mock["budget_limit_usd"] == 50.0  # DEFAULT_BUDGET_USD
    assert {row["provider"] for row in mock["comparison"]} >= {"mock", "veo", "runway", "kling"}

    veo = client.post(url, json={**SETTINGS, "provider": "veo", "max_new_clips": None, "reuse": "max_variety"}).json()
    assert veo["configured"] is False and veo["price_per_second_usd"] > 0
    assert veo["total_usd"] == pytest.approx(veo["new_clips"] * veo["clip_duration"] * veo["price_per_second_usd"])
    assert any("GOOGLE_API_KEY" in note for note in veo["notes"])

    bad_model = client.post(url, json={**SETTINGS, "model": "nope"})
    assert bad_model.status_code == 422 and bad_model.json()["code"] == "provider_unknown_model"
    library = client.post(url, json={**SETTINGS, "provider": "library"})
    assert library.status_code == 422 and library.json()["code"] == "library_empty"


def test_estimate_uses_project_budget_and_world(client: TestClient, ready_project: Project, db: Session) -> None:
    ready_project.budget_limit_usd = 12.5
    ready_project.world_preset = "dreamy"
    db.commit()
    body = client.post(f"/api/projects/{ready_project.id}/estimate", json={"provider": "mock"}).json()
    assert body["budget_limit_usd"] == 12.5
    explicit = client.post(f"/api/projects/{ready_project.id}/estimate", json={"budget_limit_usd": 3}).json()
    assert explicit["budget_limit_usd"] == 3


def test_batch_guards(
    client: TestClient, ready_project: Project, db: Session, enqueued: list, priced_mock: Any
) -> None:
    def code(project_id: str, **overrides: Any) -> tuple[int, str | None]:
        response = client.post(f"/api/projects/{project_id}/batches", json={**SETTINGS, **overrides})
        return response.status_code, response.json().get("code")

    empty = client.post("/api/projects", json={"title": "空", "artist_name": "A"}).json()["id"]
    assert code(empty) == (422, "audio_missing")

    ready_project.analysis_status = "running"
    db.commit()
    assert code(ready_project.id) == (422, "analysis_missing")
    ready_project.analysis_status = "done"
    ready_project.rights = {**ready_project.rights, "platform_terms": False}
    db.commit()
    assert code(ready_project.id) == (422, "rights_unconfirmed")
    client.put(f"/api/projects/{ready_project.id}/rights", json={"items": {"platform_terms": True}})

    assert code(ready_project.id, provider="veo") == (422, "provider_not_configured")
    assert code(ready_project.id, provider="runway", model="gen4_turbo") == (422, "provider_not_configured")
    assert code(ready_project.id, provider="library") == (422, "library_empty")
    assert code(ready_project.id, count=0) == (422, None)
    assert code(ready_project.id, start_strategy="custom") == (422, None)

    priced_mock(0.10)  # every mock second now costs $0.10
    response = client.post(_batches_url(ready_project), json={**SETTINGS, "budget_limit_usd": 0.1})
    assert response.status_code == 422 and response.json()["code"] == "budget_exceeded"
    assert "予算上限" in response.json()["detail"]
    assert db.scalars(select(Batch)).all() == [] and enqueued == []


def test_create_batch_queues_run(client: TestClient, ready_project: Project, enqueued: list) -> None:
    response = client.post(_batches_url(ready_project), json={**SETTINGS, "lyric_style": "auto"})
    assert response.status_code == 201
    batch = response.json()
    assert batch["status"] == "queued" and batch["settings"]["model"] == "mock-v1"
    assert isinstance(batch["settings"]["seed"], int) and batch["settings"]["lyric_style"] == "auto"
    assert batch["estimate"]["videos"] == 2 and batch["counts"]["total"] == 0
    assert enqueued == [("run_batch", batch["id"])]
    listed = client.get(_batches_url(ready_project)).json()
    assert [b["id"] for b in listed] == [batch["id"]]


def test_batch_runs_to_completion(
    client: TestClient, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any
) -> None:
    created = client.post(_batches_url(ready_project), json=SETTINGS).json()
    batch = client.get(f"/api/batches/{created['id']}").json()
    assert batch["status"] == "completed" and batch["started_at"] and batch["finished_at"]
    assert batch["counts"] == {"total": 2, "done": 2, "rendering": 0, "queued": 0, "failed": 0, "cancelled": 0}
    assert batch["clip_counts"]["ready"] == batch["clip_counts"]["total"] >= 1
    assert batch["storyboard"]["source"] == "template"
    assert len(batch["videos"]) == 2 and all(v["url"] and v["thumbnail_url"] for v in batch["videos"])
    assert all(c["status"] == "ready" and c["used_in_videos"] >= 1 for c in batch["clips"])
    video_url = batch["videos"][0]["url"].replace("http://testserver", "")
    media = client.get(video_url)
    assert media.status_code == 200 and media.headers["content-type"] == "video/mp4"

    dashboard = client.get("/api/dashboard").json()
    assert dashboard["videos_done"] == 2 and dashboard["active_jobs"] == []
    assert dashboard["usage"]["clips_generated"] == batch["clip_counts"]["total"]


def test_cancel_batch(client: TestClient, ready_project: Project, enqueued: list, db: Session) -> None:
    batch_id = client.post(_batches_url(ready_project), json=SETTINGS).json()["id"]
    response = client.post(f"/api/batches/{batch_id}/cancel")
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    assert response.json()["error"] and response.json()["finished_at"]
    assert client.post(f"/api/batches/{batch_id}/cancel").json()["status"] == "cancelled"  # idempotent
    db.get(Batch, batch_id).status = "completed"
    db.commit()
    finished = client.post(f"/api/batches/{batch_id}/cancel")
    assert finished.status_code == 409 and finished.json()["code"] == "batch_finished"


def test_cancelled_batch_is_not_run(
    client: TestClient, ready_project: Project, enqueued: list, fake_clips: Any, fake_render: Any
) -> None:
    from app import pipeline

    batch_id = client.post(_batches_url(ready_project), json=SETTINGS).json()["id"]
    client.post(f"/api/batches/{batch_id}/cancel")
    pipeline.run_batch(batch_id)  # a queued message delivered after the cancel
    assert client.get(f"/api/batches/{batch_id}").json()["status"] == "cancelled"
    assert fake_clips.calls == [] and fake_render.specs == []


def test_batch_zip(
    client: TestClient, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any
) -> None:
    batch_id = client.post(_batches_url(ready_project), json=SETTINGS).json()["id"]
    response = client.get(f"/api/batches/{batch_id}/zip")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    assert "attachment" in response.headers["content-disposition"]
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert archive.testzip() is None
    names = archive.namelist()
    assert names == ["デモアーティスト_夜明けのうた_01.mp4", "デモアーティスト_夜明けのうた_02.mp4"]
    assert all(info.compress_type == zipfile.ZIP_STORED and info.file_size > 10_000 for info in archive.infolist())
    assert archive.read(names[0])[4:8] == b"ftyp"


def test_zip_without_done_videos_is_409(client: TestClient, ready_project: Project, enqueued: list) -> None:
    batch_id = client.post(_batches_url(ready_project), json=SETTINGS).json()["id"]
    response = client.get(f"/api/batches/{batch_id}/zip")
    assert response.status_code == 409 and response.json()["code"] == "no_videos"


def test_queue_outage_fails_batch_with_503(
    client: TestClient, ready_project: Project, monkeypatch: Any, db: Session
) -> None:
    from app import worker

    def down(name: str, *args: Any) -> None:
        raise worker.QueueUnavailable("ジョブキュー（Redis）に接続できません")

    monkeypatch.setattr(worker, "enqueue", down)
    response = client.post(_batches_url(ready_project), json=SETTINGS)
    assert response.status_code == 503 and response.json()["code"] == "queue_unavailable"
    (batch,) = db.scalars(select(Batch)).all()
    assert batch.status == "failed" and batch.error


def test_batch_endpoints_are_owner_scoped(make_client: Any, ready_project: Project, db: Session) -> None:
    batch = Batch(project_id=ready_project.id, status="completed", settings={})
    db.add(batch)
    db.commit()
    other = make_client(AUTH_MODE="token", API_TOKENS="bob:bob-token-0123456789abcdef")
    headers = {"Authorization": "Bearer bob-token-0123456789abcdef"}
    for method, url in (
        ("get", f"/api/batches/{batch.id}"),
        ("post", f"/api/batches/{batch.id}/cancel"),
        ("get", f"/api/batches/{batch.id}/zip"),
        ("get", f"/api/projects/{ready_project.id}/batches"),
        ("post", f"/api/projects/{ready_project.id}/estimate"),
        ("post", f"/api/projects/{ready_project.id}/batches"),
        ("post", f"/api/projects/{ready_project.id}/storyboard"),
    ):
        kwargs: dict[str, Any] = {"headers": headers}
        if method == "post":
            kwargs["json"] = {}
        assert getattr(other, method)(url, **kwargs).status_code == 404, url
