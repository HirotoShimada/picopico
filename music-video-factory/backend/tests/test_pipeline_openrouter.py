"""The pipeline with the OpenRouter video provider over respx-mocked HTTP (never the real API).

Checks that the batch budget is reserved at the estimated price before each paid attempt, that
OpenRouter's reported ``usage.cost`` is what gets booked, that a job reported before a crash is
resumed instead of resubmitted, and that the estimate prices models from the (cached) listing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import pipeline
from app.models import Batch, Clip, CostRecord, Project
from app.schemas import GenerationSettings
from app.services import cost as cost_service
from app.services.errors import ServiceError
from app.services.providers.base import PollConfig
from app.services.providers.openrouter import OpenRouterProvider, reset_model_cache

KEY = "or-test-key-not-real"
BASE = "https://openrouter.ai/api/v1"
MODEL = "google/veo-3.1-lite"
JOB = "gen-vid-1791763200-pipeline0000000001"


def _listing(price: str = "0.05", **extra: Any) -> httpx.Response:
    entry = {
        "id": MODEL, "name": "Veo 3.1 Lite", "canonical_slug": MODEL, "created": 1776902400,
        "supported_durations": [4, 6, 8], "supported_aspect_ratios": ["9:16", "16:9"],
        "supported_resolutions": ["720p"], "supported_frame_images": ["first_frame"], "generate_audio": True,
        "seed": False, "allowed_passthrough_parameters": [], "pricing_skus": {"per-video-second": price},
        "creativity": None, "upscale_factor": None,
    }  # fmt: skip
    entries = [entry, {**entry, "id": "vendor/no-price", "name": "No price", "pricing_skus": None}]
    return httpx.Response(200, json={"data": entries, **extra})


@pytest.fixture
def openrouter(set_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(OPENROUTER_API_KEY=KEY)
    monkeypatch.setattr(OpenRouterProvider, "poll_config",
                        PollConfig(initial=0.0, maximum=0.0, factor=1.0, jitter=0.0, minimum=0.0))  # fmt: skip


def _no_zdr_claude(router: respx.MockRouter) -> respx.Route:
    """The OpenRouter key also routes Claude storyboards; here no ZDR endpoint is found → template."""
    return router.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(
        404, json={"error": {"code": 404, "message": "No endpoints found matching your data policy"}}
    ))  # fmt: skip


def _batch(db: Session, project: Project, **settings: Any) -> Batch:
    values = {"provider": "openrouter", "model": MODEL, "count": 1, "duration_sec": 5, "seed": 7,
              "max_new_clips": 1, **settings}  # fmt: skip
    return pipeline.create_batch(db, project, GenerationSettings(**values))


def _clips(db: Session, batch: Batch) -> list[Clip]:
    db.expire_all()
    return [c for c in db.scalars(select(Clip)) if (c.params or {}).get("batch_id") == batch.id]


def test_estimate_uses_listing_prices_and_unknown_prices_are_refused(
    db: Session, ready_project: Project, openrouter: None
) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BASE}/videos/models").mock(return_value=_listing("0.07"))
        preview = pipeline.preview_batch(db, ready_project, GenerationSettings(provider="openrouter", model=MODEL,
                                                                               count=2, duration_sec=5, seed=1))
        estimate = preview.estimate
        assert estimate["price_per_second_usd"] == 0.07 and estimate["clip_duration"] == 6.0
        assert estimate["video_generation_usd"] == pytest.approx(estimate["new_clips"] * 6 * 0.07)
        assert estimate["configured"] is True and any("usage.cost" in n for n in estimate["notes"])
        rows = {(r["provider"], r["model"]) for r in estimate["comparison"]}
        assert ("openrouter", MODEL) in rows and ("openrouter", "vendor/no-price") not in rows
        assert cost_service.get_pricing("openrouter", MODEL).price_per_second_usd == 0.07
        with pytest.raises(ServiceError) as info:
            _batch(db, ready_project, model="vendor/no-price")
        assert info.value.code == "provider_not_configured" and "料金" in info.value.message


def test_reserves_the_estimate_and_books_the_reported_usage_cost(
    db: Session, ready_project: Project, openrouter: None, sync_tasks: list, fake_render: Any, demo_dir: Path
) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BASE}/videos/models").mock(return_value=_listing())
        chat = _no_zdr_claude(router)
        batch = _batch(db, ready_project)
        reserved: list[float] = []

        def submit(request: httpx.Request) -> httpx.Response:
            db.expire_all()
            reserved.append(db.get(Batch, batch.id).cost_usd)  # the attempt's estimate is reserved before submitting
            return httpx.Response(202, json={"id": JOB, "polling_url": f"{BASE}/videos/{JOB}", "status": "pending"})

        create = router.post(f"{BASE}/videos").mock(side_effect=submit)
        router.get(f"{BASE}/videos/{JOB}").mock(side_effect=[
            httpx.Response(200, json={"id": JOB, "status": "in_progress"}),
            httpx.Response(200, json={"id": JOB, "status": "completed", "usage": {"cost": 0.27, "is_byok": False}}),
        ])  # fmt: skip
        router.get(f"{BASE}/videos/{JOB}/content").mock(
            return_value=httpx.Response(200, content=(demo_dir / "clip_01.mp4").read_bytes())
        )
        pipeline.run_batch(batch.id)

    assert create.call_count == 1 and reserved == [pytest.approx(6 * 0.05)]
    storyboard = db.get(Batch, batch.id).storyboard
    assert chat.call_count == 1 and storyboard["source"] == "template" and storyboard["route"] == "openrouter"
    assert any("提供元" in w for w in storyboard["warnings"])
    assert db.scalars(select(CostRecord).where(CostRecord.kind == "llm")).all() == []
    (clip,) = _clips(db, batch)
    assert clip.status == "ready" and clip.external_job_id == JOB and clip.cost_usd == pytest.approx(0.27)
    (record,) = db.scalars(select(CostRecord).where(CostRecord.clip_id == clip.id)).all()
    assert (record.provider, record.model, record.kind) == ("openrouter", MODEL, "video_generation")
    assert record.amount_usd == pytest.approx(0.27) and record.unit_price_usd == pytest.approx(0.05)
    assert "usage.cost" in (record.note or "")
    batch = db.get(Batch, batch.id)
    assert batch.cost_usd == pytest.approx(0.27) and batch.status == "completed"  # reservation settled to the real cost


def test_job_reported_before_a_crash_is_resumed_not_resubmitted(
    db: Session, ready_project: Project, openrouter: None, sync_tasks: list, fake_render: Any, demo_dir: Path
) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BASE}/videos/models").mock(return_value=_listing())
        _no_zdr_claude(router)
        batch = _batch(db, ready_project)
        token = pipeline.utcnow()
        assert pipeline._transition_batch(db, batch.id, ["queued"], "planning", started_at=token)
        db.refresh(batch)
        assert pipeline._plan_batch_rows(db, batch, token)
        (clip,) = _clips(db, batch)
        create = router.post(f"{BASE}/videos").mock(
            return_value=httpx.Response(202, json={"id": JOB, "polling_url": f"{BASE}/videos/{JOB}", "status": "pending"})
        )

        def die(request: httpx.Request) -> httpx.Response:
            raise KeyboardInterrupt  # the worker dies while polling

        poll = router.get(f"{BASE}/videos/{JOB}").mock(side_effect=die)
        with pytest.raises(KeyboardInterrupt):
            pipeline.generate_clip(clip.id)
        db.expire_all()
        clip = db.get(Clip, clip.id)
        assert clip.status == "generating" and clip.params["open_job"] == JOB and clip.external_job_id == JOB

        poll.mock(side_effect=None, return_value=httpx.Response(200, json={
            "id": JOB, "status": "completed", "usage": {"cost": 0.3}}))  # fmt: skip
        router.get(f"{BASE}/videos/{JOB}/content").mock(
            return_value=httpx.Response(200, content=(demo_dir / "clip_01.mp4").read_bytes())
        )
        assert pipeline.recover_interrupted_jobs()["batches"] == 1

    assert create.call_count == 1  # never submitted again
    db.expire_all()
    clip = db.get(Clip, clip.id)
    assert clip.status == "ready" and "open_job" not in clip.params and clip.cost_usd == pytest.approx(0.3)
    assert db.get(Batch, batch.id).cost_usd == pytest.approx(0.3)


def test_timed_out_job_is_presumed_billed_and_not_resubmitted(
    db: Session, ready_project: Project, openrouter: None, sync_tasks: list, fake_render: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    monkeypatch.setattr(pipeline, "CLIP_TIMEOUT_SEC", 0.0)
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BASE}/videos/models").mock(return_value=_listing())
        _no_zdr_claude(router)
        batch = _batch(db, ready_project)
        create = router.post(f"{BASE}/videos").mock(
            return_value=httpx.Response(202, json={"id": JOB, "status": "pending"})
        )
        router.get(f"{BASE}/videos/{JOB}").mock(return_value=httpx.Response(200, json={"id": JOB, "status": "pending"}))
        pipeline.run_batch(batch.id)
    (clip,) = _clips(db, batch)
    assert create.call_count == 1 and clip.status == "failed" and "再送信しませんでした" in (clip.error or "")
    (record,) = db.scalars(select(CostRecord).where(CostRecord.clip_id == clip.id)).all()
    assert record.amount_usd == pytest.approx(6 * 0.05) and "推定" in record.note  # no cancel API: presumed billed


def test_insufficient_credits_fail_the_clip_without_cost(
    db: Session, ready_project: Project, openrouter: None, sync_tasks: list, fake_render: Any
) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BASE}/videos/models").mock(return_value=_listing())
        _no_zdr_claude(router)
        batch = _batch(db, ready_project)
        create = router.post(f"{BASE}/videos").mock(
            return_value=httpx.Response(402, json={"error": {"code": 402, "message": "Insufficient credits"}})
        )
        pipeline.run_batch(batch.id)
    (clip,) = _clips(db, batch)
    assert create.call_count == 1 and clip.status == "failed" and "クレジットが不足" in (clip.error or "")
    assert db.scalars(select(CostRecord).where(CostRecord.clip_id == clip.id)).all() == []
    assert db.get(Batch, batch.id).cost_usd == pytest.approx(0.0)


# --- ambiguous submissions (POST /videos has no idempotency key) ---------------------------------------------------


@pytest.mark.parametrize("first", ["read-timeout", "disconnected", "502", "524", "202-not-json", "202-bad-id"])
def test_ambiguous_submit_is_not_resent_and_its_estimate_is_booked(
    first: str, db: Session, ready_project: Project, openrouter: None, sync_tasks: list, fake_render: Any,
    demo_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """The first POST may have created (and billed) a job whose answer was lost: never send it again."""
    monkeypatch.setattr(pipeline, "RETRY_BACKOFF_SEC", (0.0, 0.0))
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BASE}/videos/models").mock(return_value=_listing())
        _no_zdr_claude(router)
        batch = _batch(db, ready_project)
        sent: list[int] = []

        def submit(request: httpx.Request) -> httpx.Response:
            sent.append(1)
            if len(sent) > 1:  # a resend would create a second, separately billed job
                return httpx.Response(202, json={"id": JOB, "status": "pending"})
            if first == "read-timeout":
                raise httpx.ReadTimeout("read timed out", request=request)
            if first == "disconnected":
                raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=request)
            if first == "202-not-json":
                return httpx.Response(202, text="not json")
            if first == "202-bad-id":
                return httpx.Response(202, json={"id": "gen vid with spaces", "status": "pending"})
            return httpx.Response(int(first), text="edge error")

        create = router.post(f"{BASE}/videos").mock(side_effect=submit)
        router.get(f"{BASE}/videos/{JOB}").mock(return_value=httpx.Response(
            200, json={"id": JOB, "status": "completed", "usage": {"cost": 0.3}}))  # fmt: skip
        router.get(f"{BASE}/videos/{JOB}/content").mock(
            return_value=httpx.Response(200, content=(demo_dir / "clip_01.mp4").read_bytes())
        )
        pipeline.run_batch(batch.id)

    (clip,) = _clips(db, batch)
    assert create.call_count == 1
    assert clip.status == "failed" and "重複課金を避けて再送信しませんでした" in (clip.error or "")
    (record,) = db.scalars(select(CostRecord).where(CostRecord.clip_id == clip.id)).all()
    assert record.amount_usd == pytest.approx(6 * 0.05) and "推定" in (record.note or "")  # presumed billed
    assert clip.cost_usd == pytest.approx(6 * 0.05)
    assert db.get(Batch, batch.id).cost_usd == pytest.approx(6 * 0.05)  # the budget sees the possible spend


def test_submit_refused_before_any_job_exists_is_still_retried(
    db: Session, ready_project: Project, openrouter: None, sync_tasks: list, fake_render: Any,
    demo_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    monkeypatch.setattr(pipeline, "RETRY_BACKOFF_SEC", (0.0, 0.0))
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BASE}/videos/models").mock(return_value=_listing())
        _no_zdr_claude(router)
        batch = _batch(db, ready_project)
        create = router.post(f"{BASE}/videos").mock(side_effect=[
            httpx.ConnectError("connection refused"),
            httpx.Response(503, json={"error": {"code": 503, "message": "No available provider"}}),
            httpx.Response(202, json={"id": JOB, "status": "pending"}),
        ])  # fmt: skip
        router.get(f"{BASE}/videos/{JOB}").mock(return_value=httpx.Response(
            200, json={"id": JOB, "status": "completed", "usage": {"cost": 0.28}}))  # fmt: skip
        router.get(f"{BASE}/videos/{JOB}/content").mock(
            return_value=httpx.Response(200, content=(demo_dir / "clip_01.mp4").read_bytes())
        )
        pipeline.run_batch(batch.id)
    (clip,) = _clips(db, batch)
    assert create.call_count == 3 and clip.status == "ready" and clip.cost_usd == pytest.approx(0.28)
    assert [r.amount_usd for r in db.scalars(select(CostRecord).where(CostRecord.clip_id == clip.id))] == [
        pytest.approx(0.28)
    ]
    assert db.get(Batch, batch.id).cost_usd == pytest.approx(0.28)


# --- resuming a submitted job after a restart, whatever the listing says now ----------------------------------------

UNLISTED = "bytedance/seedance-2.0"  # not in the built-in list / static prices


def _entry(model_id: str, price: str | None) -> dict[str, Any]:
    return {"id": model_id, "name": model_id, "supported_durations": [4, 6, 8], "supported_aspect_ratios": ["9:16"],
            "supported_resolutions": ["720p"], "pricing_skus": {"per-video-second": price} if price else None}


def _crash_while_polling(
    db: Session, project: Project, router: respx.MockRouter
) -> tuple[Batch, Clip, respx.Route, list[str]]:
    """Submit the batch's only clip (UNLISTED at $0.07/s → 6 s = $0.42 reserved), then kill the worker mid-poll."""
    batch = _batch(db, project, model=UNLISTED)
    token = pipeline.utcnow()
    assert pipeline._transition_batch(db, batch.id, ["queued"], "planning", started_at=token)
    db.refresh(batch)
    assert pipeline._plan_batch_rows(db, batch, token)
    (clip,) = _clips(db, batch)
    create = router.post(f"{BASE}/videos").mock(return_value=httpx.Response(202, json={"id": JOB, "status": "pending"}))
    polls: list[str] = []

    def die(request: httpx.Request) -> httpx.Response:
        polls.append("before-crash")
        raise KeyboardInterrupt  # the worker dies while polling

    router.get(f"{BASE}/videos/{JOB}").mock(side_effect=die)
    with pytest.raises(KeyboardInterrupt):
        pipeline.generate_clip(clip.id)
    db.expire_all()
    clip = db.get(Clip, clip.id)
    assert clip.status == "generating" and clip.params["open_job"] == JOB
    assert clip.params["submitted_usd"] == pytest.approx(0.42)  # the job's price is kept with the job
    return batch, clip, create, polls


@pytest.mark.parametrize("change", ["model-removed", "price-unreadable"])
def test_open_job_is_resumed_and_booked_after_its_model_left_the_listing(
    change: str, db: Session, ready_project: Project, openrouter: None, sync_tasks: list, fake_render: Any,
    demo_dir: Path,
) -> None:  # fmt: skip
    with respx.mock(assert_all_called=False) as router:
        listing = router.get(f"{BASE}/videos/models").mock(
            return_value=httpx.Response(200, json={"data": [_entry(UNLISTED, "0.07"), _entry(MODEL, "0.05")]})
        )
        _no_zdr_claude(router)
        batch, clip, create, polls = _crash_while_polling(db, ready_project, router)

        # Restart: the listing is fetched again and no longer lists (or prices) the model.
        reset_model_cache()
        now = [_entry(MODEL, "0.05")] if change == "model-removed" else [_entry(UNLISTED, None), _entry(MODEL, "0.05")]
        listing.mock(return_value=httpx.Response(200, json={"data": now}))

        def finished(request: httpx.Request) -> httpx.Response:
            polls.append("after-restart")
            return httpx.Response(200, json={"id": JOB, "status": "completed", "usage": {"cost": 0.39}})

        router.get(f"{BASE}/videos/{JOB}").mock(side_effect=finished)
        router.get(f"{BASE}/videos/{JOB}/content").mock(
            return_value=httpx.Response(200, content=(demo_dir / "clip_01.mp4").read_bytes())
        )
        assert pipeline.recover_interrupted_jobs()["batches"] == 1

    assert create.call_count == 1 and "after-restart" in polls  # the same job was followed, never resubmitted
    db.expire_all()
    clip = db.get(Clip, clip.id)
    assert clip.status == "ready" and clip.cost_usd == pytest.approx(0.39)
    assert "open_job" not in clip.params and "submitted_usd" not in clip.params
    (record,) = db.scalars(select(CostRecord).where(CostRecord.clip_id == clip.id)).all()
    assert record.amount_usd == pytest.approx(0.39) and record.unit_price_usd == pytest.approx(0.07)
    assert db.get(Batch, batch.id).cost_usd == pytest.approx(0.39)


@pytest.mark.parametrize("why", ["key-removed", "corrupt-token"])
def test_open_job_that_cannot_be_resumed_is_booked_as_presumed_billed(
    why: str, db: Session, ready_project: Project, openrouter: None, sync_tasks: list, fake_render: Any,
    set_env: Any,
) -> None:  # fmt: skip
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BASE}/videos/models").mock(
            return_value=httpx.Response(200, json={"data": [_entry(UNLISTED, "0.07"), _entry(MODEL, "0.05")]})
        )
        _no_zdr_claude(router)
        batch, clip, create, polls = _crash_while_polling(db, ready_project, router)
        # Restart: the job keeps running (and is billed) at OpenRouter but cannot be followed.
        if why == "key-removed":
            set_env(OPENROUTER_API_KEY="")
        else:
            clip.params = {**clip.params, "open_job": "../../not-a-job"}
            db.commit()
        assert pipeline.recover_interrupted_jobs()["batches"] == 1

    assert create.call_count == 1 and polls == ["before-crash"]
    db.expire_all()
    clip = db.get(Clip, clip.id)
    assert clip.status == "failed" and "open_job" not in clip.params
    assert "再開できなかったため、推定額を課金済みとして計上しました" in (clip.error or "")
    (record,) = db.scalars(select(CostRecord).where(CostRecord.clip_id == clip.id)).all()
    assert record.amount_usd == pytest.approx(0.42) and "推定" in (record.note or "")  # the submit-time estimate
    assert clip.cost_usd == pytest.approx(0.42) and db.get(Batch, batch.id).cost_usd == pytest.approx(0.42)
