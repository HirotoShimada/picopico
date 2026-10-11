"""Pipeline orchestration (ARCHITECTURE.md §7): analysis, batches, clip generation, rendering, recovery.

Most tests replace the slow parts with fakes (``fake_clips`` copies a demo clip,
``fake_render`` copies a canned 5 s MP4) and run enqueued tasks synchronously
(``sync_tasks``). ``test_run_batch_real_mock_provider_and_renderer`` exercises the
real mock provider, FFmpeg renderer and quality validator end to end.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import pipeline
from app.models import Asset, Batch, Clip, CostRecord, Project, Video
from app.schemas import GenerationSettings, VideoEdit
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


def _batch_clips(db: Session, batch: Batch) -> list[Clip]:
    db.expire_all()
    ids = pipeline._batch_clip_ids(db.get(Batch, batch.id))
    return list(db.scalars(select(Clip).where(Clip.id.in_(ids))))


# --- analysis -----------------------------------------------------------------------------------------------------


def _project_with_audio(db: Session, make_project: Any, storage: Any, source: Path, name: str) -> Project:
    from app.models import new_id
    from app.storage import make_key

    project = make_project()
    asset = Asset(
        id=new_id(),
        project_id=project.id,
        owner_id=project.owner_id,
        kind="audio",
        filename=name,
        content_type="audio/mpeg",
    )
    asset.storage_key = make_key(project.id, "audio", asset.id, Path(name).suffix)
    asset.size_bytes = storage.put_file(asset.storage_key, source)
    db.add(asset)
    project.audio_asset = asset
    project.analysis_status = "queued"
    db.commit()
    return project


def test_analyze_project_audio_stores_analysis_and_default_segment(
    db: Session, make_project: Any, storage: Any, demo_dir: Path
) -> None:
    project = _project_with_audio(db, make_project, storage, demo_dir / "demo_song.mp3", "demo_song.mp3")
    pipeline.analyze_project_audio(project.id)
    project = _reload(db, project)
    assert project.analysis_status == "done" and project.analysis_error is None
    assert 115 <= project.analysis["bpm"] <= 125
    # Best 15 s suggestion = chorus start on a downbeat (~24.03 s).
    assert project.segment_start == pytest.approx(24.03, abs=0.1)
    assert project.segment_end - project.segment_start == pytest.approx(15.0, abs=0.01)


def test_analyze_keeps_user_segment_and_reports_failures(
    db: Session, make_project: Any, storage: Any, tmp_path: Path
) -> None:
    bogus = tmp_path / "bogus.mp3"
    bogus.write_bytes(b"not audio at all" * 100)
    project = _project_with_audio(db, make_project, storage, bogus, "bogus.mp3")
    pipeline.analyze_project_audio(project.id)
    project = _reload(db, project)
    assert project.analysis_status == "failed"
    assert project.analysis_error and any("぀" <= ch <= "ヿ" for ch in project.analysis_error)
    assert project.analysis is None


def test_analyze_discards_result_when_audio_was_replaced(
    db: Session, make_project: Any, storage: Any, demo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.services.audio_analysis as audio_analysis
    from app.db import SessionLocal

    project = _project_with_audio(db, make_project, storage, demo_dir / "demo_song.mp3", "demo_song.mp3")

    def replaced_meanwhile(path: Any) -> dict[str, Any]:
        with SessionLocal() as other:
            p = other.get(Project, project.id)
            p.audio_asset = None
            p.analysis_status = "queued"
            other.commit()
        return {"duration": 60.0}

    monkeypatch.setattr(audio_analysis, "analyze_audio", replaced_meanwhile)
    pipeline.analyze_project_audio(project.id)
    project = _reload(db, project)
    assert project.analysis is None and project.analysis_status == "queued"


# --- batches: real end-to-end -------------------------------------------------------------------------------------


def test_run_batch_real_mock_provider_and_renderer(
    db: Session, ready_project: Project, sync_tasks: list, storage: Any
) -> None:
    batch = _create_batch(db, ready_project, count=2, duration_sec=5, lyric_style="auto", max_new_clips=2)
    assert batch.status == "queued" and batch.estimate["within_budget"]
    pipeline.run_batch(batch.id)

    batch = _reload(db, batch)
    assert batch.status == "completed", batch.error
    assert batch.started_at and batch.finished_at
    assert ("render_video", _videos(db, batch)[0].id) in sync_tasks
    clips = _batch_clips(db, batch)
    assert clips and all(c.status == "ready" and c.asset_id and c.thumbnail_asset_id for c in clips)
    assert all(c.source == "mock" and c.model == "mock-v1" and c.cache_key for c in clips)
    for video in _videos(db, batch):
        assert video.status == "done" and video.progress == 1.0 and video.error is None
        assert video.quality["passed"], video.quality
        assert {c["name"] for c in video.quality["checks"]} >= {"resolution", "fps", "duration", "audio_codec"}
        assert video.asset.kind == "render" and storage.exists(video.asset.storage_key)
        assert video.asset.filename == f"デモアーティスト_夜明けのうた_{video.index + 1:02d}.mp4"
        assert video.thumbnail_asset is not None and storage.exists(video.thumbnail_asset.storage_key)
        assert all(shot["clip_id"] in {c.id for c in clips} for shot in video.plan["shots"])
        assert video.attempts == 1 and video.rendered_at is not None
    # lyric_style "auto" rotates the preset's recommended styles.
    assert len({v.plan["lyric_style"] for v in _videos(db, batch)}) == 2
    records = db.scalars(select(CostRecord).where(CostRecord.batch_id == batch.id)).all()
    assert len(records) == len(clips) and not any(r.cached for r in records)


# --- batches: logic with fakes ------------------------------------------------------------------------------------


def test_second_batch_reuses_cached_clips(
    db: Session, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any
) -> None:
    first = _create_batch(db, ready_project, max_new_clips=3)
    pipeline.run_batch(first.id)
    assert _reload(db, first).status == "completed"
    generated = len(fake_clips.calls)
    assert generated >= 1

    second = _create_batch(db, ready_project, seed=99, max_new_clips=0)
    assert second.estimate["new_clips"] == 0 and second.estimate["cached_clips"] >= 1
    pipeline.run_batch(second.id)
    second = _reload(db, second)
    assert second.status == "completed"
    assert len(fake_clips.calls) == generated  # nothing new was generated
    cached = db.scalars(select(CostRecord).where(CostRecord.batch_id == second.id)).all()
    assert cached and all(r.cached and r.amount_usd == 0 for r in cached)
    first_clip_ids = {c.id for c in _batch_clips(db, first)}
    for video in _videos(db, second):
        assert {s["clip_id"] for s in video.plan["shots"]} <= first_clip_ids


def test_budget_check_before_generation(
    db: Session, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any, priced_mock: Any
) -> None:
    priced_mock(0.10)  # $0.50 per 5 s clip
    with pytest.raises(BudgetExceeded) as excinfo:
        _create_batch(db, ready_project, budget_limit_usd=0.2)
    assert excinfo.value.status_code == 422 and excinfo.value.limit_usd == 0.2

    # Created within budget, but the run-time plan no longer fits (e.g. prices changed).
    batch = _create_batch(db, ready_project, budget_limit_usd=100)
    batch.estimate = {**batch.estimate, "budget_limit_usd": 0.2}
    db.commit()
    pipeline.run_batch(batch.id)
    batch = _reload(db, batch)
    assert batch.status == "budget_exceeded" and "予算" in batch.error
    assert fake_clips.calls == []
    assert all(v.status == "cancelled" for v in _videos(db, batch))
    assert all(c.status == "cancelled" for c in _batch_clips(db, batch))


def test_budget_exceeded_mid_generation_stops_paid_clips(
    db: Session,
    ready_project: Project,
    sync_tasks: list,
    fake_clips: Any,
    fake_render: Any,
    priced_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    priced_mock(0.10)
    monkeypatch.setattr(pipeline, "CLIP_CONCURRENCY", 1)
    batch = _create_batch(db, ready_project, reuse="max_variety", max_new_clips=3, budget_limit_usd=100)
    assert batch.estimate["new_clips"] == 3
    # The real spend turns out higher than estimated: every clip costs $2.
    from app.services.providers.mock import MockProvider

    original = MockProvider.generate

    def expensive(self: Any, req: Any, out_path: str, **kwargs: Any) -> Any:
        result = original(self, req, out_path, **kwargs)
        result.cost_usd = 2.0
        return result

    monkeypatch.setattr(MockProvider, "generate", expensive)
    batch.estimate = {**batch.estimate, "budget_limit_usd": 3.0}
    db.commit()
    pipeline.run_batch(batch.id)

    batch = _reload(db, batch)
    assert batch.status == "budget_exceeded"
    clips = _batch_clips(db, batch)
    statuses = sorted(c.status for c in clips)
    assert statuses.count("ready") == 2 and statuses.count("cancelled") == 1
    assert batch.cost_usd == pytest.approx(4.0)  # two clips started within the limit, the third was refused
    paid = db.scalars(select(CostRecord).where(CostRecord.batch_id == batch.id, CostRecord.cached.is_(False))).all()
    assert sum(r.amount_usd for r in paid) == pytest.approx(4.0)
    assert all(v.status == "cancelled" for v in _videos(db, batch))


def test_retryable_provider_errors_are_retried(
    db: Session,
    ready_project: Project,
    sync_tasks: list,
    fake_clips: Any,
    fake_render: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline, "CLIP_CONCURRENCY", 1)
    waits: list[float] = []
    monkeypatch.setattr(pipeline, "_sleep", waits.append)
    monkeypatch.setattr(pipeline, "RETRY_BACKOFF_SEC", (0.01, 0.02))
    fake_clips.script = {
        0: ProviderError("混雑しています", retryable=True),
        1: ProviderTaskError("タイムアウト", retryable=True, external_job_id="job-1", failure_code="timeout"),
    }
    batch = _create_batch(db, ready_project, max_new_clips=1)
    pipeline.run_batch(batch.id)
    batch = _reload(db, batch)
    assert batch.status == "completed"
    (clip,) = _batch_clips(db, batch)
    assert clip.status == "ready" and clip.attempts == 3 and clip.error is None
    assert len(fake_clips.calls) == 3 and waits  # backoff happened between attempts


def test_non_retryable_failure_uses_substitute_clip(
    db: Session,
    ready_project: Project,
    sync_tasks: list,
    fake_clips: Any,
    fake_render: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline, "CLIP_CONCURRENCY", 1)
    fake_clips.script = {0: ProviderError("安全フィルタにより拒否されました", retryable=False)}
    batch = _create_batch(db, ready_project, max_new_clips=3)
    pipeline.run_batch(batch.id)
    batch = _reload(db, batch)
    assert batch.status == "completed"
    clips = _batch_clips(db, batch)
    failed = [c for c in clips if c.status == "failed"]
    assert len(failed) == 1 and failed[0].attempts == 1 and "安全フィルタ" in failed[0].error
    ready_ids = {c.id for c in clips if c.status == "ready"}
    for video in _videos(db, batch):
        assert {s["clip_id"] for s in video.plan["shots"]} <= ready_ids
    assert any(v.plan.get("substitutions") for v in _videos(db, batch))


def test_all_clips_failing_fails_videos_and_batch(
    db: Session, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any
) -> None:
    fake_clips.script = {i: ProviderError("生成失敗", retryable=False) for i in range(10)}
    batch = _create_batch(db, ready_project, max_new_clips=2)
    pipeline.run_batch(batch.id)
    batch = _reload(db, batch)
    assert batch.status == "failed" and batch.error
    assert all(v.status == "failed" and v.error for v in _videos(db, batch))
    assert fake_render.specs == []


def test_cancel_during_clip_generation(
    db: Session,
    ready_project: Project,
    sync_tasks: list,
    fake_clips: Any,
    fake_render: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.db import SessionLocal

    monkeypatch.setattr(pipeline, "CLIP_CONCURRENCY", 1)
    monkeypatch.setattr(pipeline, "CANCEL_POLL_SEC", 0.0)
    batch = _create_batch(db, ready_project, max_new_clips=3)

    def cancel_on_first(number: int, req: Any, cancel_check: Any) -> None:
        if number == 0:
            with SessionLocal() as other:
                pipeline.cancel_batch(other, other.get(Batch, batch.id))

    fake_clips.hook = cancel_on_first
    pipeline.run_batch(batch.id)
    batch = _reload(db, batch)
    assert batch.status == "cancelled" and batch.finished_at
    assert all(v.status == "cancelled" for v in _videos(db, batch))
    assert all(c.status == "cancelled" for c in _batch_clips(db, batch))
    assert len(fake_clips.calls) == 1 and fake_render.specs == []
    assert not [c for c in sync_tasks if c[0] == "render_video"]
    with pytest.raises(ServiceError) as excinfo:  # a finished batch cannot be cancelled again
        batch.status = "completed"
        db.commit()
        pipeline.cancel_batch(db, batch)
    assert excinfo.value.status_code == 409


def test_cancel_during_render_and_retry(
    db: Session,
    ready_project: Project,
    sync_tasks: list,
    fake_clips: Any,
    fake_render: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.db import SessionLocal

    monkeypatch.setattr(pipeline, "PROGRESS_WRITE_SEC", 0.0)
    batch = _create_batch(db, ready_project, count=1, max_new_clips=1)

    def cancel_then_report(spec: Any, progress_cb: Any) -> None:
        with SessionLocal() as other:
            pipeline.cancel_batch(other, other.get(Batch, batch.id))
        progress_cb(0.3, "encode")  # raises JobCancelled

    fake_render.before = cancel_then_report
    pipeline.run_batch(batch.id)
    (video,) = _videos(db, batch)
    assert video.status == "cancelled" and video.asset_id is None
    assert _reload(db, batch).status == "cancelled"

    # Retrying a cancelled video re-opens the batch and renders the same plan.
    fake_render.before = None
    video = db.get(Video, video.id)
    shots_before = video.plan["shots"]
    pipeline.prepare_retry(db, video)
    pipeline.render_video(video.id)
    (video,) = _videos(db, batch)
    assert video.status == "done" and video.plan["shots"] == shots_before
    assert video.attempts == 2
    assert _reload(db, batch).status == "completed"


def test_render_failure_then_retry(
    db: Session, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any
) -> None:
    fake_render.fail = RenderError("動画のレンダリングに失敗しました（FFmpeg）", "stderr tail")
    batch = _create_batch(db, ready_project, max_new_clips=2)
    pipeline.run_batch(batch.id)
    batch = _reload(db, batch)
    assert batch.status == "failed"
    videos = _videos(db, batch)
    assert all(v.status == "failed" and "FFmpeg" in v.error for v in videos)

    fake_render.fail = None
    pipeline.prepare_retry(db, db.get(Video, videos[0].id))
    assert _reload(db, batch).status == "rendering"
    pipeline.render_video(videos[0].id)
    assert _reload(db, batch).status == "partial"
    pipeline.prepare_retry(db, db.get(Video, videos[1].id))
    pipeline.render_video(videos[1].id)
    batch = _reload(db, batch)
    assert batch.status == "completed" and batch.error is None
    with pytest.raises(ServiceError) as excinfo:
        pipeline.prepare_retry(db, db.get(Video, videos[1].id))
    assert excinfo.value.status_code == 409


def test_quality_failure_marks_video_failed_and_keeps_report(
    db: Session, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any
) -> None:
    batch = _create_batch(db, ready_project, count=1, duration_sec=6, max_new_clips=1)  # canned MP4 is 5 s
    pipeline.run_batch(batch.id)
    (video,) = _videos(db, batch)
    assert video.status == "failed" and "品質チェック" in video.error
    assert video.quality and not video.quality["passed"]
    assert any(c["name"] == "duration" and not c["passed"] for c in video.quality["checks"])
    assert video.asset_id is None


def test_missing_clip_is_substituted_at_render_time(
    db: Session, ready_project: Project, sync_tasks: list, fake_render: Any, add_upload_clips: Any, storage: Any
) -> None:
    clips = add_upload_clips(ready_project)
    batch = _create_batch(db, ready_project, provider="library", count=1)
    pipeline.run_batch(batch.id)
    (video,) = _videos(db, batch)
    assert video.status == "done"
    used = video.plan["shots"][0]["clip_id"]
    # The user deletes a clip the finished video used, then re-renders it.
    gone = db.get(Clip, used)
    storage.delete(gone.asset.storage_key)
    db.delete(gone)
    db.commit()
    video = db.get(Video, video.id)
    pipeline.apply_video_edit(db, video, VideoEdit(lyric_style="karaoke"))
    pipeline.render_video(video.id)
    video = _reload(db, video)
    assert video.status == "done" and video.render_version == 2
    remaining = {c.id for c in clips} - {used}
    assert {s["clip_id"] for s in video.plan["shots"]} <= remaining
    assert video.plan["substitutions"] >= 1


# --- edits & regenerate -------------------------------------------------------------------------------------------


def test_apply_video_edit_overrides_and_rerender(
    db: Session, ready_project: Project, sync_tasks: list, fake_render: Any, add_upload_clips: Any
) -> None:
    clips = add_upload_clips(ready_project)
    batch = _create_batch(db, ready_project, provider="library", count=1, lyric_style="simple")
    pipeline.run_batch(batch.id)
    (video,) = _videos(db, batch)
    old_asset = video.asset_id
    line = next(line for line in ready_project.lyric_lines if line["start"] >= 24)
    swap_to = next(c for c in clips if c.id != video.plan["shots"][0]["clip_id"])
    edit = VideoEdit.model_validate(
        {
            "lyric_style": "karaoke",
            "color_grade": "mono_noir",
            "text_style": {"font_size": 72, "position": "center"},
            "lyric_offsets": {line["id"]: {"start": line["start"] + 0.5, "end": line["end"] + 0.5}},
            "shots": [{"index": 0, "clip_id": swap_to.id, "src_in": 0.5}],
        }
    )
    pipeline.apply_video_edit(db, db.get(Video, video.id), edit)
    queued = _reload(db, video)
    assert queued.status == "queued" and queued.render_version == 2
    assert _reload(db, batch).status == "rendering"
    pipeline.render_video(video.id)

    video = _reload(db, video)
    assert video.status == "done" and video.asset_id != old_asset
    assert db.get(Asset, old_asset) is None  # replaced render removed
    spec = fake_render.specs[-1]
    assert spec.lyric_style == "karaoke" and spec.color_grade == "mono_noir"
    assert spec.text_style == {"font_size": 72, "position": "center"}
    moved = next(x for x in spec.lyric_lines if x["id"] == line["id"])
    assert moved["start"] == pytest.approx(line["start"] + 0.5) and moved["source"] == "manual"
    assert video.plan["shots"][0]["clip_id"] == swap_to.id and video.plan["shots"][0]["src_in"] == 0.5
    assert spec.shots[0].clip_path.endswith(swap_to.asset.storage_key) and spec.shots[0].src_in == 0.5
    assert _reload(db, batch).status == "completed"

    for bad in (
        {"color_grade": "nope"},
        {"lyric_offsets": {"l999": {"start": 1, "end": 2}}},
        {"shots": [{"index": 99, "clip_id": swap_to.id}]},
        {"start": 59.0},
    ):
        with pytest.raises(ServiceError) as excinfo:
            pipeline.apply_video_edit(db, db.get(Video, video.id), VideoEdit.model_validate(bad))
        assert excinfo.value.status_code == 422, bad


def test_regenerate_with_new_clips_generates_in_render_task(
    db: Session, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any
) -> None:
    batch = _create_batch(db, ready_project, count=1, max_new_clips=1)
    pipeline.run_batch(batch.id)
    (video,) = _videos(db, batch)
    before = len(fake_clips.calls)
    old_shots = video.plan["shots"]

    pipeline.regenerate_video_plan(db, db.get(Video, video.id), new_clips=True)
    video = _reload(db, video)
    assert video.status == "waiting_clips" and video.render_version == 2 and video.plan["regenerated"]
    new_clip_ids = {s["clip_id"] for s in video.plan["shots"]} - {s["clip_id"] for s in old_shots}
    assert new_clip_ids and all(db.get(Clip, cid).status == "pending" for cid in new_clip_ids)
    pipeline.render_video(video.id)
    video = _reload(db, video)
    assert video.status == "done"
    assert len(fake_clips.calls) > before
    assert all(db.get(Clip, cid).status == "ready" for cid in new_clip_ids)

    # Without new clips only existing footage is used (no spend).
    calls = len(fake_clips.calls)
    pipeline.regenerate_video_plan(db, db.get(Video, video.id), new_clips=False)
    assert _reload(db, video).status == "queued"
    pipeline.render_video(video.id)
    assert _reload(db, video).status == "done" and len(fake_clips.calls) == calls


def test_regenerate_over_budget_is_refused(
    db: Session, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any, priced_mock: Any
) -> None:
    priced_mock(0.10)
    batch = _create_batch(db, ready_project, count=1, max_new_clips=1, budget_limit_usd=0.6)
    pipeline.run_batch(batch.id)
    (video,) = _videos(db, batch)
    assert video.status == "done" and _reload(db, batch).cost_usd == pytest.approx(0.5)
    with pytest.raises(BudgetExceeded):
        pipeline.regenerate_video_plan(db, db.get(Video, video.id), new_clips=True)
    video = _reload(db, video)
    assert video.status == "done" and video.render_version == 1


# --- status rules, lyrics helpers, recovery -----------------------------------------------------------------------


def test_recompute_batch_status_rules(db: Session, make_project: Any) -> None:
    project = make_project()
    batch = Batch(project_id=project.id, status="rendering", settings={})
    db.add(batch)
    db.flush()
    videos = [Video(project_id=project.id, batch_id=batch.id, index=i, status="rendering") for i in range(3)]
    db.add_all(videos)
    db.commit()

    def status_with(*statuses: str) -> str | None:
        for video, status in zip(videos, statuses, strict=True):
            video.status = status
        db.commit()
        return pipeline.recompute_batch_status(db, batch.id)

    assert status_with("done", "rendering", "failed") == "rendering"
    assert status_with("done", "done", "done") == "completed"
    assert _reload(db, batch).finished_at is not None
    assert status_with("done", "failed", "cancelled") == "partial"
    assert status_with("failed", "failed", "cancelled") == "failed"
    assert _reload(db, batch).error
    assert status_with("done", "done", "done") == "completed" and _reload(db, batch).error is None
    batch = _reload(db, batch)
    batch.status = "cancelled"
    db.commit()
    assert status_with("done", "done", "done") == "cancelled"  # user cancel is final


def test_retime_line_rescales_words() -> None:
    line = {
        "id": "l001",
        "text": "叫べ 今",
        "start": 10.0,
        "end": 12.0,
        "source": "lrc",
        "confidence": None,
        "words": [{"text": "叫べ", "start": 10.0, "end": 11.0}, {"text": "今", "start": 11.0, "end": 12.0}],
    }
    moved = pipeline.retime_line(line, 20.0, 24.0)
    assert moved["start"] == 20.0 and moved["end"] == 24.0 and moved["source"] == "manual"
    assert moved["words"] == [{"text": "叫べ", "start": 20.0, "end": 22.0}, {"text": "今", "start": 22.0, "end": 24.0}]
    assert line["start"] == 10.0  # input untouched
    lines = pipeline.effective_lyric_lines([line], {"l001": {"start": 5, "end": 6}, "zzz": {"start": 1, "end": 2}})
    assert lines[0]["start"] == 5.0 and lines[0]["words"][1]["end"] == 6.0


def test_download_filename_is_sanitized(make_project: Any) -> None:
    project = make_project(title='夜明け/のうた:"live"', artist_name="アーティスト")
    video = Video(index=2)
    name = pipeline.download_filename(project, video)
    assert name.endswith("_03.mp4") and "/" not in name and ":" not in name and '"' not in name


def test_recover_interrupted_jobs_requeues_work(
    db: Session, make_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import worker
    from app.models import new_id

    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(worker, "enqueue", lambda name, *args: calls.append((name, *args)))
    project = make_project()
    asset = Asset(
        id=new_id(),
        project_id=project.id,
        owner_id=project.owner_id,
        kind="audio",
        filename="a.mp3",
        storage_key=f"projects/{project.id}/audio/x.mp3",
    )
    db.add(asset)
    project.audio_asset = asset
    project.analysis_status = "running"
    planning = Batch(project_id=project.id, status="generating_clips", settings={})
    rendering = Batch(project_id=project.id, status="rendering", settings={})
    done = Batch(project_id=project.id, status="completed", settings={})
    db.add_all([planning, rendering, done])
    db.flush()
    v1 = Video(project_id=project.id, batch_id=rendering.id, index=0, status="rendering")
    v2 = Video(project_id=project.id, batch_id=rendering.id, index=1, status="done")
    db.add_all([v1, v2])
    db.commit()

    counts = pipeline.recover_interrupted_jobs()
    assert counts == {"analyses": 1, "batches": 1, "videos": 1}
    assert ("analyze", project.id) in calls and ("run_batch", planning.id) in calls and ("render_video", v1.id) in calls


def test_clean_stale_work_dirs_keeps_recent_ones() -> None:
    import os
    import time

    from app.config import get_settings

    work = get_settings().WORK_DIR
    old = work / "renders" / "dead-process-render"
    fresh = work / "renders" / "running-render"
    other = work / "uploads" / "old-upload-dir"
    for path in (old, fresh, other):
        path.mkdir(parents=True, exist_ok=True)
        (path / "f.bin").write_bytes(b"x")
    past = time.time() - pipeline.STALE_WORK_SEC - 60
    os.utime(old, (past, past))
    os.utime(other, (past, past))
    try:
        assert pipeline.clean_stale_work_dirs() == 1
        assert not old.exists() and fresh.exists() and other.exists()
    finally:
        for path in (fresh, other):  # other tests check that uploads leave nothing behind
            shutil.rmtree(path, ignore_errors=True)


def test_celery_dispatch_publishes_named_tasks(set_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from app import worker

    set_env(TASK_MODE="celery")
    sent: list[tuple[str, list[Any]]] = []
    for task_name in worker.CELERY_TASK_NAMES.values():
        task = worker.celery_app.tasks[task_name]
        monkeypatch.setattr(task, "apply_async", lambda args, _n=task_name, **kw: sent.append((_n, args)))
    worker.enqueue("analyze", "p1")
    worker.enqueue("run_batch", "b1")
    worker.enqueue("render_video", "v1")
    assert sent == [("mvf.analyze", ["p1"]), ("mvf.run_batch", ["b1"]), ("mvf.render_video", ["v1"])]
    conf = worker.celery_app.conf
    assert worker.celery_app.main == "mvf"
    assert conf.task_acks_late is True and conf.worker_prefetch_multiplier == 1


def test_celery_task_runs_registered_body(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import worker

    seen: list[str] = []
    monkeypatch.setitem(worker._registry, "render_video", seen.append)
    worker.celery_app.tasks["mvf.render_video"].apply(args=["v42"]).get()
    assert seen == ["v42"]


def test_celery_broker_down_raises_queue_unavailable(set_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from app import worker

    set_env(TASK_MODE="celery")

    def down(*args: Any, **kwargs: Any) -> None:
        raise ConnectionRefusedError("redis down")

    monkeypatch.setattr(worker.celery_app.tasks["mvf.analyze"], "apply_async", down)
    with pytest.raises(worker.QueueUnavailable) as excinfo:
        worker.enqueue("analyze", "p1")
    assert excinfo.value.status_code == 503


def test_celery_worker_module_imports_cleanly() -> None:
    """``celery -A app.worker.celery_app`` must find the three tasks without importing the API."""
    code = (
        "from app.worker import celery_app;"
        "celery_app.loader.import_default_modules();"
        "print(sorted(n for n in celery_app.tasks if n.startswith('mvf.')))"
    )
    out = subprocess.run(
        [str(Path(__file__).resolve().parents[1] / ".venv" / "bin" / "python"), "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        cwd=Path(__file__).resolve().parents[1],
    ).stdout
    assert "['mvf.analyze', 'mvf.render_video', 'mvf.run_batch']" in out
