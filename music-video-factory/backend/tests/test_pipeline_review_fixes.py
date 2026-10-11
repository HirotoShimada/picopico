"""Regression tests for the pipeline review findings (cancel/retry races, budget, rights, substitution,
duplicate generation, billed attempts, recovery, storyboard reuse).

Fixtures come from ``conftest.py``: ``fake_clips`` copies a demo clip instead of generating,
``fake_render`` copies a canned 5 s MP4, ``sync_tasks`` runs enqueued tasks synchronously.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import pipeline, worker
from app.db import SessionLocal
from app.models import Batch, Clip, CostRecord, Project, Video, utcnow
from app.schemas import GenerationSettings, VideoEdit
from app.serializers import to_batch_out
from app.services.errors import BudgetExceeded, ProviderError, RenderError, ServiceError
from app.services.providers import ProviderTaskError


def _create_batch(db: Session, project: Project, **settings: Any) -> Batch:
    values = {"provider": "mock", "count": 2, "duration_sec": 5, "seed": 7, **settings}
    return pipeline.create_batch(db, project, GenerationSettings(**values))


def _reload(db: Session, obj: Any) -> Any:
    db.expire_all()
    return db.get(type(obj), obj.id)


def _videos(db: Session, batch: Batch) -> list[Video]:
    db.expire_all()
    return list(db.scalars(select(Video).where(Video.batch_id == batch.id).order_by(Video.index)))


def _clips_of(db: Session, batch_id: str) -> list[Clip]:
    db.expire_all()
    return [c for c in db.scalars(select(Clip)) if (c.params or {}).get("batch_id") == batch_id]


def _cancel(batch_id: str) -> None:
    with SessionLocal() as other:
        pipeline.cancel_batch(other, other.get(Batch, batch_id))


def _plan_only(db: Session, batch: Batch) -> None:
    """Run just the planning step of ``run_batch`` (rows created, nothing generated)."""
    token = utcnow()
    assert pipeline._transition_batch(db, batch.id, ["queued"], "planning", started_at=token)
    db.refresh(batch)
    assert pipeline._plan_batch_rows(db, batch, token)
    db.refresh(batch)


# --- cancel while planning ----------------------------------------------------------------------------------------


def test_cancel_during_planning_creates_no_stuck_rows(
    db: Session, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any, monkeypatch: Any
) -> None:
    batch = _create_batch(db, ready_project, count=3, max_new_clips=3)
    real = pipeline.build_storyboard

    def cancel_while_planning(project: Any, settings: Any, *, use_claude: bool) -> Any:
        _cancel(batch.id)  # e.g. during a long Claude call
        return real(project, settings, use_claude=use_claude)

    monkeypatch.setattr(pipeline, "build_storyboard", cancel_while_planning)
    pipeline.run_batch(batch.id)

    batch = _reload(db, batch)
    assert batch.status == "cancelled" and batch.plan is None
    assert _videos(db, batch) == [] and _clips_of(db, batch.id) == []
    assert fake_clips.calls == [] and fake_render.specs == []


def test_recovery_settles_active_rows_of_terminal_batches(db: Session, ready_project: Project, enqueued: list) -> None:
    """Rows left active in a cancelled batch (e.g. by an older version) are cancelled, not resumed."""
    batch = Batch(project_id=ready_project.id, status="cancelled", settings={"provider": "mock"}, plan={})
    db.add(batch)
    db.flush()
    video = Video(project_id=ready_project.id, batch_id=batch.id, index=0, status="waiting_clips", plan={})
    clip = Clip(project_id=ready_project.id, source="mock", provider="mock", model="mock-v1", status="pending",
                params={"batch_id": batch.id, "duration": 5})  # fmt: skip
    finished = Batch(project_id=ready_project.id, status="completed", settings={"provider": "mock"}, plan={})
    db.add_all([video, clip, finished])
    db.flush()
    orphan = Clip(project_id=ready_project.id, source="mock", provider="mock", model="mock-v1", status="pending",
                  params={"batch_id": finished.id, "duration": 5})  # fmt: skip
    db.add(orphan)
    db.commit()

    pipeline.recover_interrupted_jobs()
    assert _reload(db, video).status == "cancelled" and _reload(db, clip).status == "cancelled"
    assert _reload(db, orphan).status == "cancelled"
    assert enqueued == []


# --- regenerate + cancel, retry -----------------------------------------------------------------------------------


def test_cancel_after_regenerate_cancels_its_pending_clips(
    db: Session, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any, monkeypatch: Any
) -> None:
    batch = _create_batch(db, ready_project, count=1, max_new_clips=1)
    pipeline.run_batch(batch.id)
    (video,) = _videos(db, batch)
    done_plan, done_asset = video.plan, video.asset_id
    pipeline.regenerate_video_plan(db, db.get(Video, video.id), new_clips=True)
    assert _reload(db, video).status == "waiting_clips"
    assert any(c.status == "pending" for c in _clips_of(db, batch.id))

    pipeline.cancel_batch(db, _reload(db, batch))
    pipeline.render_video(video.id)  # the queued render task runs after the cancel
    assert not [c for c in _clips_of(db, batch.id) if c.status in ("pending", "generating")]
    batch = _reload(db, batch)
    assert to_batch_out(db, batch).clip_counts.generating == 0
    # The finished render was kept and the re-opened batch is complete again.
    video = _reload(db, video)
    assert video.status == "done" and video.asset_id == done_asset and video.plan["shots"] == done_plan["shots"]
    assert batch.status == "completed"


def test_retry_regenerates_failed_clips(
    db: Session, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any
) -> None:
    fake_clips.script = {i: ProviderError("一時的な障害", retryable=False) for i in range(3)}
    batch = _create_batch(db, ready_project, count=1, max_new_clips=2)
    pipeline.run_batch(batch.id)
    (video,) = _videos(db, batch)
    assert video.status == "failed" and _reload(db, batch).status == "failed"
    calls = len(fake_clips.calls)

    fake_clips.script = {}  # the provider recovered
    pipeline.prepare_retry(db, db.get(Video, video.id))
    assert _reload(db, video).status == "waiting_clips"
    pipeline.render_video(video.id)
    video = _reload(db, video)
    assert video.status == "done" and not video.plan.get("substitutions")
    assert len(fake_clips.calls) > calls
    assert all(c.status == "ready" for c in _clips_of(db, batch.id))
    assert _reload(db, batch).status == "completed"


def test_retry_after_budget_stop_keeps_reason_and_checks_budget(
    db: Session, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any, priced_mock: Any
) -> None:
    priced_mock(0.10)  # $0.50 per 5 s clip
    batch = _create_batch(db, ready_project, count=1, max_new_clips=2, budget_limit_usd=100)
    batch.estimate = {**batch.estimate, "budget_limit_usd": 0.6}  # the run no longer fits
    db.commit()
    pipeline.run_batch(batch.id)
    batch = _reload(db, batch)
    assert batch.status == "budget_exceeded"
    reason = batch.error
    (video,) = _videos(db, batch)
    with pytest.raises(BudgetExceeded):  # still over budget: refused, nothing changes
        pipeline.prepare_retry(db, db.get(Video, video.id))
    assert _reload(db, video).status == "cancelled" and _reload(db, batch).status == "budget_exceeded"

    batch.estimate = {**batch.estimate, "budget_limit_usd": 5.0}  # the user raised the budget
    db.commit()
    pipeline.prepare_retry(db, db.get(Video, video.id))
    pipeline.render_video(video.id)
    batch = _reload(db, batch)
    assert _reload(db, video).status == "done" and batch.status == "completed"
    assert batch.plan["previous_error"] == reason


# --- budget ------------------------------------------------------------------------------------------------------


def test_regenerate_admission_counts_other_pending_clips(
    db: Session, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any, priced_mock: Any,
    monkeypatch: Any,
) -> None:  # fmt: skip
    priced_mock(0.10)
    batch = _create_batch(db, ready_project, count=2, max_new_clips=1, budget_limit_usd=1.2)
    pipeline.run_batch(batch.id)
    assert _reload(db, batch).cost_usd == pytest.approx(0.5)
    v0, v1 = _videos(db, batch)
    monkeypatch.setattr(worker, "enqueue", lambda *a: None)  # renders not run yet
    pipeline.regenerate_video_plan(db, db.get(Video, v0.id), new_clips=True)  # 0.5 + 0.5 ≤ 1.2
    with pytest.raises(BudgetExceeded):  # 0.5 spent + 0.5 pending (v0) + 0.5 new > 1.2
        pipeline.regenerate_video_plan(db, db.get(Video, v1.id), new_clips=True)
    assert _reload(db, v1).status == "done" and _reload(db, v1).render_version == 1


def test_parallel_regenerate_renders_cannot_overspend(
    db: Session, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any, priced_mock: Any,
    monkeypatch: Any,
) -> None:  # fmt: skip
    priced_mock(0.10)
    batch = _create_batch(db, ready_project, count=2, max_new_clips=1, budget_limit_usd=10)
    pipeline.run_batch(batch.id)
    v0, v1 = _videos(db, batch)
    monkeypatch.setattr(worker, "enqueue", lambda *a: None)
    pipeline.regenerate_video_plan(db, db.get(Video, v0.id), new_clips=True)
    pipeline.regenerate_video_plan(db, db.get(Video, v1.id), new_clips=True)
    batch = _reload(db, batch)
    batch.estimate = {**batch.estimate, "budget_limit_usd": 1.0}  # room for one more clip only
    db.commit()

    arrived: list[int] = []
    both = threading.Event()

    def overlap(number: int, req: Any, cancel_check: Any) -> None:
        arrived.append(number)
        if len(arrived) >= 2:
            both.set()
        both.wait(1.0)  # both renders inside the provider at once, if the budget lets them

    fake_clips.hook = overlap
    threads = [threading.Thread(target=pipeline.render_video, args=(v.id,)) for v in (v0, v1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    batch = _reload(db, batch)
    assert batch.cost_usd <= 1.0 + 1e-9
    paid = db.scalars(select(CostRecord).where(CostRecord.batch_id == batch.id, CostRecord.cached.is_(False))).all()
    assert sum(r.amount_usd for r in paid) == pytest.approx(batch.cost_usd)
    videos = _videos(db, batch)
    refused = [v for v in videos if v.error and "予算" in v.error]
    assert len(refused) == 1  # not silently rendered with old footage
    assert refused[0].status == "done" and refused[0].render_version == 1  # back to its previous render
    assert not refused[0].plan.get("regenerated")
    rendered = [v for v in videos if v is not refused[0]]
    assert rendered[0].status == "done" and rendered[0].render_version == 2 and rendered[0].plan["regenerated"]


# --- rights ------------------------------------------------------------------------------------------------------


def test_rights_withdrawn_after_queueing_stop_render_and_generation(
    db: Session, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any, monkeypatch: Any
) -> None:
    batch = _create_batch(db, ready_project, count=1, max_new_clips=1)
    pipeline.run_batch(batch.id)
    (video,) = _videos(db, batch)
    monkeypatch.setattr(worker, "enqueue", lambda *a: None)
    pipeline.regenerate_video_plan(db, db.get(Video, video.id), new_clips=True)
    pending = [c for c in _clips_of(db, batch.id) if c.status == "pending"]
    calls, renders = len(fake_clips.calls), len(fake_render.specs)

    project = db.get(Project, ready_project.id)
    project.rights = {**project.rights, "lyrics_rights": False}
    db.commit()
    with pytest.raises(ServiceError) as excinfo:
        pipeline.prepare_retry(db, db.get(Video, video.id))
    pipeline.render_video(video.id)
    assert pipeline.generate_clip(pending[0].id) in ("failed", "cancelled")
    assert len(fake_clips.calls) == calls and len(fake_render.specs) == renders
    video = _reload(db, video)
    assert video.status == "done" and "権利" in video.error and video.render_version == 1

    with pytest.raises(ServiceError) as excinfo:
        pipeline.apply_video_edit(db, db.get(Video, video.id), VideoEdit(lyric_style="fade"))
    assert excinfo.value.code == "rights_unconfirmed"


# --- substitution ------------------------------------------------------------------------------------------------


def test_substitution_prefers_own_world_clips(
    db: Session, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(pipeline, "CLIP_CONCURRENCY", 1)
    first = _create_batch(db, ready_project, preset="urban_night", count=2, max_new_clips=4)
    pipeline.run_batch(first.id)
    assert _reload(db, first).status == "completed"
    foreign = {c.id for c in _clips_of(db, first.id)}

    fake_clips.script = {len(fake_clips.calls): ProviderError("安全フィルタにより拒否", retryable=False)}
    second = _create_batch(db, ready_project, preset="nature", count=2, max_new_clips=4, seed=11)
    pipeline.run_batch(second.id)
    own_ready = {c.id for c in _clips_of(db, second.id) if c.status == "ready"}
    substituted = [s for v in _videos(db, second) for s in v.plan["shots"] if s.get("substituted_from")]
    assert substituted and own_ready
    assert all(s["clip_id"] in own_ready and s["clip_id"] not in foreign for s in substituted)


def test_substitution_never_uses_other_providers_footage(
    db: Session, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any, set_env: Any,
    monkeypatch: Any,
) -> None:  # fmt: skip
    from app.services.providers.veo import VeoProvider

    first = _create_batch(db, ready_project, count=1, max_new_clips=2)  # mock footage in the project
    pipeline.run_batch(first.id)
    mock_clips = {c.id for c in _clips_of(db, first.id)}

    set_env(GOOGLE_API_KEY="test-key-not-used")

    def rejected(self: Any, req: Any, out_path: str, **kwargs: Any) -> Any:
        raise ProviderError("コンテンツポリシーにより拒否されました", retryable=False)

    monkeypatch.setattr(VeoProvider, "generate", rejected)
    veo = _create_batch(db, ready_project, provider="veo", count=2, max_new_clips=2)
    pipeline.run_batch(veo.id)
    for video in _videos(db, veo):
        assert video.status == "failed" and "素材" in video.error
        assert not {s["clip_id"] for s in video.plan["shots"]} & mock_clips


# --- duplicate generation across batches -------------------------------------------------------------------------


def test_batch_planned_while_another_generates_reuses_in_flight_clips(
    db: Session, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any, priced_mock: Any
) -> None:
    priced_mock(0.10)
    a = _create_batch(db, ready_project, count=1, max_new_clips=2)
    b = _create_batch(db, ready_project, count=1, max_new_clips=2)
    _plan_only(db, a)
    preview = pipeline.preview_batch(db, db.get(Project, ready_project.id), dict(b.settings))
    assert preview.estimate["cached_clips"] >= 1  # in-flight clips count as cached
    _plan_only(db, b)  # a's clips are still pending
    b_specs = b.plan["clip_specs"]
    assert b_specs and all(spec["existing_clip_id"] for spec in b_specs)

    pipeline.run_batch(a.id)
    pipeline.run_batch(b.id)
    a, b = _reload(db, a), _reload(db, b)
    assert a.status == b.status == "completed"
    keys = {c.cache_key for c in _clips_of(db, a.id)}
    assert len(fake_clips.calls) == len(keys)  # every distinct clip generated exactly once
    assert b.cost_usd == 0.0 and a.cost_usd == pytest.approx(0.5 * len(keys))
    records = db.scalars(select(CostRecord).where(CostRecord.batch_id == b.id)).all()
    assert records and all(r.cached and r.amount_usd == 0 for r in records)


def test_clip_claimed_by_another_task_is_not_generated_twice(
    db: Session, ready_project: Project, sync_tasks: list, fake_clips: Any
) -> None:
    batch = _create_batch(db, ready_project, count=1, max_new_clips=1)
    _plan_only(db, batch)
    (clip,) = _clips_of(db, batch.id)
    with SessionLocal() as other:
        assert pipeline._claim_clip(other, clip.id)
    assert pipeline.generate_clip(clip.id) == "generating"
    assert fake_clips.calls == []


def test_wait_for_clips_returns_when_owner_finishes(
    db: Session, ready_project: Project, fake_clips: Any, monkeypatch: Any
) -> None:
    batch = _create_batch(db, ready_project, count=1, max_new_clips=1)
    _plan_only(db, batch)
    (clip,) = _clips_of(db, batch.id)
    monkeypatch.setattr(pipeline, "WAIT_POLL_SEC", 0.05)
    worker_thread = threading.Thread(target=pipeline.generate_clip, args=(clip.id,))
    with SessionLocal() as other:
        other.get(Batch, batch.id).status = "generating_clips"
        other.commit()
    worker_thread.start()
    assert pipeline._wait_for_clips([clip.id], lambda: False)
    worker_thread.join(10)
    assert _reload(db, clip).status == "ready"
    # A clip whose generating task died (no heartbeat) does not block forever.
    stale = Clip(project_id=ready_project.id, source="mock", provider="mock", status="generating",
                 params={"batch_id": batch.id})  # fmt: skip
    db.add(stale)
    db.commit()
    stale.updated_at = utcnow() - timedelta(seconds=pipeline.STALE_CLIP_SEC + 5)
    db.commit()
    assert pipeline._wait_for_clips([stale.id], lambda: False)


# --- cancel / retry races ----------------------------------------------------------------------------------------


def test_cancel_stops_a_render_that_is_already_running(
    db: Session, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any
) -> None:
    batch = _create_batch(db, ready_project, count=1, max_new_clips=1)
    fake_render.before = lambda spec, cb: _cancel(batch.id)  # no progress report before it finishes
    pipeline.run_batch(batch.id)
    (video,) = _videos(db, batch)
    assert video.status == "cancelled" and video.asset_id is None
    assert _reload(db, batch).status == "cancelled"


def test_stale_render_after_cancel_and_retry_neither_renders_twice_nor_cancels_the_retry(
    db: Session, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any
) -> None:
    batch = _create_batch(db, ready_project, count=1, max_new_clips=1)

    def cancel_and_retry(spec: Any, cb: Any) -> None:
        if len(fake_render.specs) == 1:
            _cancel(batch.id)
            with SessionLocal() as other:
                pipeline.prepare_retry(other, other.scalars(select(Video).where(Video.batch_id == batch.id)).one())

    fake_render.before = cancel_and_retry
    pipeline.run_batch(batch.id)  # the first render finishes after the cancel + retry: dropped
    (video,) = _videos(db, batch)
    assert video.status == "queued" and video.asset_id is None and video.error is None
    pipeline.render_video(video.id)  # the retry's own task
    pipeline.render_video(video.id)  # a duplicate delivery: nothing to do
    (video,) = _videos(db, batch)
    assert video.status == "done" and video.attempts == 2 and len(fake_render.specs) == 2
    assert _reload(db, batch).status == "completed"


def test_duplicate_render_tasks_render_once(
    db: Session, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any, monkeypatch: Any
) -> None:
    batch = _create_batch(db, ready_project, count=1, max_new_clips=1)
    pipeline.run_batch(batch.id)
    (video,) = _videos(db, batch)
    monkeypatch.setattr(worker, "enqueue", lambda *a: None)
    pipeline.apply_video_edit(db, db.get(Video, video.id), VideoEdit(lyric_style="fade"))
    renders = len(fake_render.specs)
    gate = threading.Barrier(2, timeout=10)
    real_ensure = pipeline._ensure_video_clips

    def both_past_the_first_check(video_id: str, batch_id: str) -> None:
        gate.wait()
        real_ensure(video_id, batch_id)

    monkeypatch.setattr(pipeline, "_ensure_video_clips", both_past_the_first_check)
    threads = [threading.Thread(target=pipeline.render_video, args=(video.id,)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    video = _reload(db, video)
    assert video.status == "done" and len(fake_render.specs) == renders + 1 and video.attempts == 2


def test_cancel_then_retry_stops_clip_generation_of_the_cancelled_run(
    db: Session, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(pipeline, "CLIP_CONCURRENCY", 1)
    monkeypatch.setattr(pipeline, "CANCEL_POLL_SEC", 0.0)
    batch = _create_batch(db, ready_project, count=2, max_new_clips=3)

    def cancel_then_retry(number: int, req: Any, cancel_check: Any) -> None:
        if number == 0:
            _cancel(batch.id)
            with SessionLocal() as other:
                video = other.scalars(select(Video).where(Video.batch_id == batch.id).order_by(Video.index)).first()
                pipeline.prepare_retry(other, video)  # re-opens the batch (rendering) before the run notices

    fake_clips.hook = cancel_then_retry
    monkeypatch.setattr(worker, "enqueue", lambda *a: None)
    pipeline.run_batch(batch.id)
    assert len(fake_clips.calls) == 1  # the cancelled run generated nothing more
    # The clip that was in flight when the batch was cancelled is cancelled too, although a
    # retry had already re-opened the batch before the run looked at it again.
    assert not [c for c in _clips_of(db, batch.id) if c.status in ("ready", "pending", "generating")]
    assert all(v.status != "done" for v in _videos(db, batch))


# --- re-render cancel / failure keeps the previous render ---------------------------------------------------------


def test_failed_rerender_keeps_previous_render(
    db: Session, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any
) -> None:
    batch = _create_batch(db, ready_project, count=1, max_new_clips=1)
    pipeline.run_batch(batch.id)
    (video,) = _videos(db, batch)
    asset, plan = video.asset_id, video.plan
    fake_render.fail = RenderError("動画のレンダリングに失敗しました（FFmpeg）", "tail")
    pipeline.apply_video_edit(db, db.get(Video, video.id), VideoEdit(lyric_style="karaoke"))
    pipeline.render_video(video.id)
    video = _reload(db, video)
    assert video.status == "done" and video.asset_id == asset and video.render_version == 1
    assert video.plan == plan and "前回の動画" in video.error and "FFmpeg" in video.error
    assert _reload(db, batch).status == "completed"


# --- billed attempts ---------------------------------------------------------------------------------------------


def test_timed_out_job_without_remote_cancel_is_booked_and_not_resubmitted(
    db: Session, ready_project: Project, sync_tasks: list, fake_render: Any, set_env: Any, monkeypatch: Any
) -> None:
    from app.services import cost as cost_service
    from app.services.providers.veo import VeoProvider

    set_env(GOOGLE_API_KEY="test-key-not-used")
    submitted: list[str] = []

    def timeout(self: Any, req: Any, out_path: str, **kwargs: Any) -> Any:
        submitted.append(f"op{len(submitted)}")
        raise ProviderTaskError("Veo: 生成が時間内に完了しませんでした", retryable=True,
                                code="provider_timeout", external_job_id=submitted[-1])  # fmt: skip

    monkeypatch.setattr(VeoProvider, "generate", timeout)
    batch = _create_batch(db, ready_project, provider="veo", count=1, max_new_clips=1)
    pipeline.run_batch(batch.id)
    (clip,) = _clips_of(db, batch.id)
    price = cost_service.clip_cost_usd("veo", clip.model, float(clip.params["duration"]))
    assert submitted == ["op0"]  # Veo has no cancel API: the job keeps running, so no second submission
    assert clip.status == "failed" and "再送信しませんでした" in clip.error and clip.external_job_id == "op0"
    records = db.scalars(select(CostRecord).where(CostRecord.clip_id == clip.id)).all()
    assert [r.amount_usd for r in records] == [pytest.approx(price)] and "課金済み" in records[0].note
    assert _reload(db, batch).cost_usd == pytest.approx(price)


def test_remote_cancel_provider_retries_and_reported_cost_is_booked(
    db: Session, ready_project: Project, sync_tasks: list, fake_render: Any, set_env: Any, monkeypatch: Any
) -> None:
    from app.services.providers.runway import RunwayProvider

    set_env(RUNWAY_API_KEY="test-key-not-used")
    monkeypatch.setattr(pipeline, "RETRY_BACKOFF_SEC", (0.0, 0.0))
    calls: list[int] = []

    def flaky(self: Any, req: Any, out_path: str, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) < 3:  # timed out → Runway's task was DELETEd: not billed, safe to resubmit
            raise ProviderTaskError(
                "timeout", retryable=True, code="provider_timeout", external_job_id=f"t{len(calls)}"
            )
        failed = ProviderTaskError("Runway: 生成に失敗しました", retryable=False, external_job_id="t3")
        failed.cost_usd = 0.6  # Runway reports cost.credits for failed tasks
        raise failed

    monkeypatch.setattr(RunwayProvider, "generate", flaky)
    batch = _create_batch(db, ready_project, provider="runway", count=1, max_new_clips=1)
    pipeline.run_batch(batch.id)
    (clip,) = _clips_of(db, batch.id)
    assert len(calls) == 3 and clip.status == "failed" and clip.attempts == 3
    records = db.scalars(select(CostRecord).where(CostRecord.clip_id == clip.id)).all()
    assert [r.amount_usd for r in records] == [pytest.approx(0.6)] and "推定" not in records[0].note
    assert _reload(db, batch).cost_usd == pytest.approx(0.6)  # reservations of the attempts were released


# --- recovery & resume -------------------------------------------------------------------------------------------


def test_submitted_job_id_is_persisted_and_resumed_after_a_crash(
    db: Session, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any, monkeypatch: Any
) -> None:
    from app.services.providers.mock import MockProvider

    batch = _create_batch(db, ready_project, count=1, max_new_clips=1)
    _plan_only(db, batch)
    (clip,) = _clips_of(db, batch.id)
    seen: dict[str, Any] = {}

    def reporting_generate(self: Any, req: Any, out_path: str, *, timeout: float = 900, on_progress: Any = None,
                           cancel_check: Any = None, on_submitted: Any = None) -> Any:  # fmt: skip
        on_submitted("job-1")
        with SessionLocal() as other:
            row = other.get(Clip, clip.id)
            seen.update(job=row.external_job_id, open=row.params.get("open_job"), status=row.status)
        raise KeyboardInterrupt  # the worker dies mid-generation

    monkeypatch.setattr(MockProvider, "generate", reporting_generate)
    with pytest.raises(KeyboardInterrupt):
        pipeline.generate_clip(clip.id)
    assert seen == {"job": "job-1", "open": "job-1", "status": "generating"}

    resumed: list[str] = []

    def resume(self: Any, job_id: str, req: Any, out_path: str, **kwargs: Any) -> Any:
        resumed.append(job_id)
        return fake_clips(self, req, out_path)

    monkeypatch.setattr(MockProvider, "generate", lambda *a, **k: pytest.fail("must not submit again"))
    monkeypatch.setattr(MockProvider, "resume", resume, raising=False)
    # Restart: the orphaned clip goes back to pending with its job id, and the resumed
    # run_batch (run synchronously here) polls that job instead of submitting a new one.
    assert pipeline.recover_interrupted_jobs()["batches"] == 1
    assert resumed == ["job-1"] and _reload(db, clip).status == "ready"
    assert "open_job" not in _reload(db, clip).params
    assert _reload(db, batch).status == "completed"


def _orphan_generating_clip(db: Session, batch: Batch, **params: Any) -> tuple[Clip, float]:
    """A worker claimed the batch's clip, reserved its budget (and maybe submitted a job), then died."""
    (clip,) = _clips_of(db, batch.id)
    estimate = pipeline._clip_cost_estimate(clip)
    with SessionLocal() as other:
        assert pipeline._claim_clip(other, clip.id)
        assert pipeline._reserve_budget(other, batch.id, estimate)
        row = other.get(Clip, clip.id)
        row.params = {**row.params, "reserved_usd": estimate, **params}
        other.commit()
    return clip, estimate


def test_restart_books_an_abandoned_paid_job_before_resubmitting(
    db: Session, ready_project: Project, set_env: Any, enqueued: list, monkeypatch: Any
) -> None:
    """A provider that neither reports submissions nor resumes jobs: the dead attempt is presumed billed."""
    from app.services.providers.base import ApiVideoProvider

    def generate_without_hooks(self: Any, req: Any, out_path: str, *, timeout: float = 900, on_progress: Any = None,
                               cancel_check: Any = None) -> Any:  # fmt: skip
        raise AssertionError("not called")

    monkeypatch.setattr(ApiVideoProvider, "generate", generate_without_hooks)
    monkeypatch.delattr(ApiVideoProvider, "resume")
    set_env(GOOGLE_API_KEY="test-key-not-used")
    batch = _create_batch(db, ready_project, provider="veo", count=1, max_new_clips=1)
    _plan_only(db, batch)
    clip, estimate = _orphan_generating_clip(db, batch)

    pipeline.recover_interrupted_jobs()
    clip = _reload(db, clip)
    assert clip.status == "pending" and "reserved_usd" not in clip.params
    (record,) = db.scalars(select(CostRecord).where(CostRecord.clip_id == clip.id)).all()
    assert record.amount_usd == pytest.approx(estimate) and "中断" in record.note
    assert _reload(db, batch).cost_usd == pytest.approx(estimate)
    assert ("run_batch", batch.id) in enqueued


def test_restart_keeps_a_reported_job_open_and_releases_an_unsubmitted_reservation(
    db: Session, ready_project: Project, set_env: Any, enqueued: list
) -> None:
    """Real API providers report submissions (``on_submitted``) and can ``resume``: nothing is presumed billed."""
    set_env(GOOGLE_API_KEY="test-key-not-used")
    submitted = _create_batch(db, ready_project, provider="veo", count=1, max_new_clips=1)
    _plan_only(db, submitted)
    clip, _ = _orphan_generating_clip(db, submitted, open_job="models/veo-3.1/operations/op-1")
    pipeline.recover_interrupted_jobs()
    clip = _reload(db, clip)
    assert clip.status == "pending" and clip.params["open_job"] == "models/veo-3.1/operations/op-1"
    assert db.scalars(select(CostRecord).where(CostRecord.clip_id == clip.id)).all() == []
    assert _reload(db, submitted).cost_usd == pytest.approx(0.0)  # reservation released; resume reserves again

    # Died before anything was submitted (no job reported): the reservation is simply released.
    pipeline.cancel_batch(db, _reload(db, submitted))
    unsubmitted = _create_batch(db, ready_project, provider="veo", count=1, max_new_clips=1, seed=8)
    _plan_only(db, unsubmitted)
    clip2, _ = _orphan_generating_clip(db, unsubmitted)
    pipeline.recover_interrupted_jobs()
    assert _reload(db, clip2).status == "pending"
    assert db.scalars(select(CostRecord).where(CostRecord.clip_id == clip2.id)).all() == []
    assert _reload(db, unsubmitted).cost_usd == pytest.approx(0.0)


def test_veo_job_reported_before_a_crash_is_resumed_not_resubmitted(
    db: Session, ready_project: Project, sync_tasks: list, fake_render: Any, set_env: Any, demo_dir: Any,
    monkeypatch: Any,
) -> None:  # fmt: skip
    """End to end over HTTP mocks: the worker dies after submitting; on restart the operation is polled again."""
    import httpx
    import respx

    from app.services.providers.base import PollConfig
    from app.services.providers.veo import GEMINI_API_BASE, VeoProvider

    set_env(GOOGLE_API_KEY="test-key-not-used")
    monkeypatch.setattr(VeoProvider, "poll_config", PollConfig(initial=0.0, maximum=0.0, factor=1.0, jitter=0.0,
                                                               minimum=0.0))  # fmt: skip
    op = "models/veo-3.1-lite-generate-preview/operations/op-42"
    uri = f"{GEMINI_API_BASE}/files/abc:download?alt=media"
    batch = _create_batch(db, ready_project, provider="veo", count=1, max_new_clips=1)
    _plan_only(db, batch)
    (clip,) = _clips_of(db, batch.id)
    with respx.mock(assert_all_called=False) as router:
        create = router.post(url__regex=r".*:predictLongRunning$").mock(
            return_value=httpx.Response(200, json={"name": op})
        )
        def die(request: Any) -> Any:
            raise KeyboardInterrupt  # the worker dies while polling

        poll = router.get(f"{GEMINI_API_BASE}/{op}").mock(side_effect=die)
        with pytest.raises(KeyboardInterrupt):
            pipeline.generate_clip(clip.id)
        assert create.call_count == 1
        clip = _reload(db, clip)
        assert clip.status == "generating" and clip.external_job_id == op and clip.params["open_job"] == op

        # Restart: the clip goes back to pending with its job; the resumed batch polls that operation.
        poll.mock(side_effect=None, return_value=httpx.Response(200, json={
            "name": op, "done": True,
            "response": {"generateVideoResponse": {"generatedSamples": [{"video": {"uri": uri}}]}},
        }))  # fmt: skip
        router.get(uri).mock(return_value=httpx.Response(200, content=(demo_dir / "clip_01.mp4").read_bytes()))
        assert pipeline.recover_interrupted_jobs()["batches"] == 1

    assert create.call_count == 1  # never submitted again
    clip = _reload(db, clip)
    assert clip.status == "ready" and "open_job" not in clip.params and clip.external_job_id == op
    paid = db.scalars(select(CostRecord).where(CostRecord.clip_id == clip.id, CostRecord.cached.is_(False))).all()
    assert [round(r.amount_usd, 4) for r in paid] == [round(clip.cost_usd, 4)] and clip.cost_usd > 0
    assert _reload(db, batch).cost_usd == pytest.approx(clip.cost_usd)
    assert _reload(db, batch).status == "completed"


def test_known_zero_cost_of_a_failed_task_is_not_presumed_billed(
    db: Session, ready_project: Project, sync_tasks: list, fake_render: Any, fake_clips: Any, monkeypatch: Any,
    set_env: Any,
) -> None:  # fmt: skip
    """A remote job that *ended with an error* is retried; a reported cost (even 0) is authoritative."""
    from app.services.providers.veo import VeoProvider

    set_env(GOOGLE_API_KEY="test-key-not-used")
    calls: list[int] = []

    def flaky(self: Any, req: Any, out_path: str, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise ProviderTaskError("internal", retryable=True, code="provider_failed", external_job_id="op-1")
        if len(calls) == 2:
            raise ProviderTaskError("internal", retryable=True, code="provider_failed", external_job_id="op-2",
                                    cost_usd=0.0)  # fmt: skip
        return fake_clips(self, req, out_path)

    monkeypatch.setattr(VeoProvider, "generate", flaky)
    monkeypatch.setattr(pipeline, "RETRY_BACKOFF_SEC", (0, 0))
    batch = _create_batch(db, ready_project, provider="veo", count=1, max_new_clips=1)
    pipeline.run_batch(batch.id)
    (clip,) = _clips_of(db, batch.id)
    assert len(calls) == 3 and clip.status == "ready"
    notes = [r.note for r in db.scalars(select(CostRecord).where(CostRecord.clip_id == clip.id))]
    assert not any(n and "課金済み・生成失敗" in n for n in notes)


def test_paid_result_that_cannot_be_stored_is_booked(
    db: Session, ready_project: Project, sync_tasks: list, fake_render: Any, fake_clips: Any, monkeypatch: Any,
    set_env: Any,
) -> None:  # fmt: skip
    from app.services.providers.veo import VeoProvider

    set_env(GOOGLE_API_KEY="test-key-not-used")

    def paid(self: Any, req: Any, out_path: str, **kwargs: Any) -> Any:
        result = fake_clips(self, req, out_path)
        result.cost_usd = 0.3
        result.external_job_id = "op-9"
        return result

    def broken_store(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(VeoProvider, "generate", paid)
    monkeypatch.setattr(pipeline, "_store_clip_result", broken_store)
    batch = _create_batch(db, ready_project, provider="veo", count=1, max_new_clips=1)
    pipeline.run_batch(batch.id)
    (clip,) = _clips_of(db, batch.id)
    assert clip.status == "failed" and clip.cost_usd == pytest.approx(0.3) and clip.external_job_id == "op-9"
    assert "課金済み" in (clip.error or "")
    (record,) = db.scalars(select(CostRecord).where(CostRecord.clip_id == clip.id, CostRecord.cached.is_(False)))
    assert record.amount_usd == pytest.approx(0.3) and "推定" not in (record.note or "")
    assert _reload(db, batch).cost_usd == pytest.approx(0.3)


def test_celery_worker_start_sweeps_stale_work(
    db: Session, ready_project: Project, set_env: Any, enqueued: list
) -> None:
    set_env(TASK_MODE="celery")
    old = utcnow() - timedelta(hours=1)
    rendering = Batch(project_id=ready_project.id, status="rendering", settings={}, plan={})
    stuck_upstream = Batch(project_id=ready_project.id, status="generating_clips", settings={}, plan={},
                           created_at=old, started_at=old)  # fmt: skip
    live_upstream = Batch(project_id=ready_project.id, status="generating_clips", settings={}, plan={})
    db.add_all([rendering, stuck_upstream, live_upstream])
    db.flush()
    stale_video = Video(project_id=ready_project.id, batch_id=rendering.id, index=0, status="rendering")
    live_video = Video(project_id=ready_project.id, batch_id=rendering.id, index=1, status="rendering")
    stale_clip = Clip(project_id=ready_project.id, source="mock", provider="mock", model="mock-v1",
                      status="generating", params={"batch_id": stuck_upstream.id})  # fmt: skip
    db.add_all([stale_video, live_video, stale_clip])
    db.commit()
    for row in (stale_video, stale_clip):
        row.updated_at = old
    db.commit()

    worker._recover_after_worker_start()
    assert ("render_video", stale_video.id) in enqueued and ("render_video", live_video.id) not in enqueued
    assert ("run_batch", stuck_upstream.id) in enqueued and ("run_batch", live_upstream.id) not in enqueued
    assert _reload(db, stale_video).status == "queued" and _reload(db, live_video).status == "rendering"
    assert _reload(db, stale_clip).status == "pending"


# --- regenerate variety --------------------------------------------------------------------------------------------


def test_regenerate_keeps_the_videos_own_start_and_lyric_style(
    db: Session, ready_project: Project, sync_tasks: list, fake_render: Any, add_upload_clips: Any
) -> None:
    add_upload_clips(ready_project)
    batch = _create_batch(db, ready_project, provider="library", count=4, start_strategy="varied",
                          lyric_style="auto", color_grade="auto")  # fmt: skip
    pipeline.run_batch(batch.id)
    before = {v.index: v.plan for v in _videos(db, batch)}
    assert len({p["start"] for p in before.values()}) > 1 and len({p["lyric_style"] for p in before.values()}) > 1
    for index in (1, 2, 3):
        video = next(v for v in _videos(db, batch) if v.index == index)
        pipeline.regenerate_video_plan(db, db.get(Video, video.id), new_clips=False)
        pipeline.render_video(video.id)
        after = _reload(db, video).plan
        assert after["start"] == pytest.approx(before[index]["start"], abs=0.05)
        assert after["lyric_style"] == before[index]["lyric_style"]
        assert after["seed"] != before[index]["seed"]


# --- Claude storyboard reuse -------------------------------------------------------------------------------------


def test_claude_storyboard_is_reused_by_later_batches(
    db: Session, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any, set_env: Any,
    monkeypatch: Any,
) -> None:  # fmt: skip
    set_env(ANTHROPIC_API_KEY="test-key-not-used")
    real = pipeline.generate_storyboard
    claude_calls: list[int] = []

    def fake_claude(**kwargs: Any) -> dict[str, Any]:
        if not kwargs.get("use_claude"):
            return real(**kwargs)  # template (estimates)
        kwargs["use_claude"] = False
        storyboard = real(**kwargs)
        claude_calls.append(1)
        # Claude writes a different storyboard every time.
        for scene in storyboard["scenes"]:
            scene["prompt_en"] = f"take {len(claude_calls)}: {scene['prompt_en']}"
        storyboard.update(source="claude", model="claude-opus-5-5", cost_usd=0.05,
                          usage={"input_tokens": 1000, "output_tokens": 2000})  # fmt: skip
        return storyboard

    monkeypatch.setattr(pipeline, "generate_storyboard", fake_claude)
    first = _create_batch(db, ready_project, count=1, max_new_clips=2)
    pipeline.run_batch(first.id)
    generated = len(fake_clips.calls)

    second = _create_batch(db, ready_project, count=1, max_new_clips=2)
    assert second.estimate["llm_usd"] == 0 and second.estimate["cached_clips"] >= 1
    pipeline.run_batch(second.id)
    first, second = _reload(db, first), _reload(db, second)
    assert claude_calls == [1]
    assert second.storyboard["scenes"] == first.storyboard["scenes"]
    assert len(fake_clips.calls) == generated  # cache hits only
    records = db.scalars(select(CostRecord).where(CostRecord.batch_id == second.id)).all()
    assert records and all(r.cached and r.kind == "video_generation" for r in records)

    third = _create_batch(db, ready_project, count=1, max_new_clips=2, new_storyboard=True)
    pipeline.run_batch(third.id)
    assert claude_calls == [1, 1]


def test_regenerate_keeps_manual_lyric_timing_and_text_edits(
    db: Session, ready_project: Project, sync_tasks: list, fake_render: Any, add_upload_clips: Any
) -> None:
    add_upload_clips(ready_project)
    batch = _create_batch(db, ready_project, provider="library", count=1)
    pipeline.run_batch(batch.id)
    (video,) = _videos(db, batch)
    line = ready_project.lyric_lines[5]
    offsets = {line["id"]: {"start": line["start"] + 0.5, "end": line["end"] + 0.5}}
    pipeline.apply_video_edit(db, db.get(Video, video.id), VideoEdit(
        text_style={"font_size": 80, "position": "top"}, lyric_offsets=offsets, color_grade="warm_film"))  # fmt: skip
    pipeline.render_video(video.id)
    edited = _reload(db, video).plan

    pipeline.regenerate_video_plan(db, db.get(Video, video.id), new_clips=False)
    pipeline.render_video(video.id)
    after = _reload(db, video).plan
    assert after["seed"] != edited["seed"] and after["regenerated"] is True
    assert after["lyric_offsets"] == edited["lyric_offsets"]
    assert after["text_style"] == {"font_size": 80, "position": "top"}
    assert after["color_grade"] == "warm_film" and after["start"] == pytest.approx(edited["start"], abs=0.05)
