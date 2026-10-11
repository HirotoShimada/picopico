"""Job orchestration over the database (ARCHITECTURE.md §7).

Three background tasks, registered with :mod:`app.worker` at import time:

* ``analyze`` → :func:`analyze_project_audio` — librosa analysis of the song,
  default 15 s segment.
* ``run_batch`` → :func:`run_batch` — storyboard → batch plan → Clip/Video
  rows → clip generation (bounded concurrency, budget check before every paid
  clip, retries with backoff, cancellation) → enqueue one ``render_video`` per
  video.
* ``render_video`` → :func:`render_video` — fetch inputs from storage, render
  with FFmpeg, validate, upload MP4 + thumbnail, recompute the batch status.

Every task takes IDs, opens its own sessions and is safe to run again (a
re-delivered Celery message or a restart resumes instead of duplicating work).
State changes that race with the API (cancel) or with sibling tasks use
conditional ``UPDATE … WHERE status IN (…)`` statements, so a cancelled batch
or video is never silently resurrected.

The module also holds the planning helpers shared by the estimate / batch /
regenerate / edit endpoints, so routers stay thin.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import math
import random
import shutil
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session

from app import worker
from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    ACTIVE_BATCH_STATUSES,
    ACTIVE_VIDEO_STATUSES,
    AnalysisStatus,
    Asset,
    AssetKind,
    Batch,
    BatchStatus,
    Clip,
    ClipSource,
    ClipStatus,
    CostKind,
    CostRecord,
    CostUnit,
    Project,
    Video,
    VideoStatus,
    new_id,
    utcnow,
)
from app.schemas import GenerationSettings, VideoEdit
from app.serializers import all_rights_confirmed
from app.services import cost as cost_service
from app.services import quality as quality_service
from app.services import render as render_service
from app.services.errors import (
    BudgetExceeded,
    ProviderError,
    ProviderNotConfigured,
    RenderError,
    ServiceError,
)
from app.services.media import make_thumbnail, sanitize_filename
from app.services.presets import COLOR_GRADES
from app.services.providers import ClipRequest, ProviderCancelled, ProviderTaskError, VideoProvider, get_provider
from app.services.storyboard import DEFAULT_SCENES, claude_cost_usd, generate_storyboard
from app.services.variation import plan_batch
from app.storage import Storage, StorageNotFound, get_storage, make_key

logger = logging.getLogger(__name__)

__all__ = [
    "CLIP_CONCURRENCY",
    "MAX_CLIP_ATTEMPTS",
    "BatchPreview",
    "analyze_project_audio",
    "apply_video_edit",
    "cancel_batch",
    "clean_stale_work_dirs",
    "create_batch",
    "download_filename",
    "effective_lyric_lines",
    "generate_clip",
    "preview_batch",
    "recompute_batch_status",
    "recover_interrupted_jobs",
    "regenerate_video_plan",
    "render_video",
    "reopen_batch",
    "retime_line",
    "run_batch",
]

# --- tuning -------------------------------------------------------------------------------------------------------

CLIP_CONCURRENCY = 2  # parallel clip generations per batch
MAX_CLIP_ATTEMPTS = 3  # first try + 2 retries for retryable provider errors
RETRY_BACKOFF_SEC: tuple[float, ...] = (5.0, 20.0)  # wait before attempt 2, 3
CANCEL_POLL_SEC = 1.0  # DB polls for cancellation are throttled to this interval
PROGRESS_WRITE_SEC = 1.0  # min interval between video.progress writes
DEFAULT_SEGMENT_SEC = 15.0
CLIP_TIMEOUT_SEC = 900.0
STALE_WORK_SEC = 2 * 3600.0  # scratch dirs older than this belong to a dead process (renders time out at 30 min)
WORK_SUBDIRS = ("analysis", "clips", "renders", "whisper")
# Rough Claude storyboard size, used only for the estimate before the real call.
LLM_ESTIMATE_INPUT_TOKENS = 6000
LLM_ESTIMATE_OUTPUT_TOKENS_PER_SCENE = 700
LLM_ESTIMATE_OUTPUT_TOKENS_BASE = 1500
ANTHROPIC_PROVIDER = "anthropic"

_TERMINAL_REOPENABLE = frozenset(
    {BatchStatus.COMPLETED, BatchStatus.PARTIAL, BatchStatus.FAILED, BatchStatus.CANCELLED, BatchStatus.BUDGET_EXCEEDED}
)
_RECOMPUTABLE = frozenset({BatchStatus.RENDERING, BatchStatus.COMPLETED, BatchStatus.PARTIAL, BatchStatus.FAILED})
_UPSTREAM = frozenset({BatchStatus.QUEUED, BatchStatus.PLANNING, BatchStatus.GENERATING_CLIPS})

# Swappable for tests (keeps retry backoff instant).
_sleep: Callable[[float], None] = time.sleep


class JobCancelled(Exception):
    """Raised inside a running task when its batch/video was cancelled."""


# --- small helpers ------------------------------------------------------------------------------------------------


def stable_seed(*parts: object) -> int:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _work_dir(kind: str, name: str) -> Path:
    path = get_settings().WORK_DIR / kind / f"{name}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def local_input(storage: Storage, key: str, dest: Path) -> Path:
    """A local path for a stored object: the file itself (local backend) or a copy in ``dest``."""
    path = storage.local_path(key)
    if path is not None:
        if not path.is_file():
            raise StorageNotFound(key)
        return path
    return storage.fetch_to(key, dest)


def song_duration(project: Project) -> float | None:
    analysis = project.analysis or {}
    duration = _num(analysis.get("duration"))
    if duration is None and project.audio_asset is not None:
        duration = project.audio_asset.duration
    return duration


def resolve_budget_limit(settings: Mapping[str, Any], project: Project) -> float | None:
    """Batch budget: settings → project → ``DEFAULT_BUDGET_USD``."""
    for value in (settings.get("budget_limit_usd"), project.budget_limit_usd):
        if value is not None:
            return float(value)
    return float(get_settings().DEFAULT_BUDGET_USD)


def download_filename(project: Project, video: Video) -> str:
    """``{artist}_{title}_{NN}.mp4`` (NN = 1-based video number), sanitized."""
    name = f"{project.artist_name}_{project.title}_{video.index + 1:02d}.mp4"
    return sanitize_filename(name, default=f"video_{video.index + 1:02d}.mp4")


# --- lyrics -------------------------------------------------------------------------------------------------------


def retime_line(line: Mapping[str, Any], start: float, end: float) -> dict[str, Any]:
    """Copy of ``line`` moved to ``[start, end]``; word timings are rescaled linearly."""
    out = copy.deepcopy(dict(line))
    old_start, old_end = _num(line.get("start")), _num(line.get("end"))
    words = line.get("words")
    if words and old_start is not None and old_end is not None and old_end > old_start:
        scale = (end - start) / (old_end - old_start)
        new_words: list[dict[str, Any]] | None = []
        for word in words:
            ws, we = _num(word.get("start")), _num(word.get("end"))
            if ws is None or new_words is None:
                new_words = None
                break
            we = ws if we is None else we
            new_words.append(
                {
                    "text": word.get("text", ""),
                    "start": round(start + (ws - old_start) * scale, 3),
                    "end": round(start + (we - old_start) * scale, 3),
                }
            )
        out["words"] = new_words
    elif words:
        out["words"] = None
    out["start"] = round(float(start), 3)
    out["end"] = round(float(end), 3)
    out["source"] = "manual"
    return out


def effective_lyric_lines(
    lines: Sequence[Mapping[str, Any]] | None, overrides: Mapping[str, Mapping[str, Any]] | None
) -> list[dict[str, Any]]:
    """Project lyric lines with a video's per-line timing overrides applied."""
    result: list[dict[str, Any]] = []
    overrides = overrides or {}
    for line in lines or []:
        override = overrides.get(str(line.get("id")))
        start = _num((override or {}).get("start"))
        end = _num((override or {}).get("end"))
        if override and start is not None and end is not None and end > start:
            result.append(retime_line(line, start, end))
        else:
            result.append(copy.deepcopy(dict(line)))
    return result


# --- planning (shared by estimate / batch creation / regenerate) --------------------------------------------------


@dataclass
class BatchPreview:
    settings: dict[str, Any]
    storyboard: dict[str, Any] | None
    plan: dict[str, Any]
    estimate: dict[str, Any]
    budget_limit_usd: float | None


def resolve_generation_settings(
    project: Project, settings: GenerationSettings | Mapping[str, Any]
) -> tuple[dict[str, Any], VideoProvider]:
    """GenerationSettings as a dict with ``model`` resolved (cache keys need the real model id).

    ``preset`` / ``custom_world`` fall back to the project's world when the
    request did not set them.
    """
    if isinstance(settings, GenerationSettings):
        data = settings.model_dump(mode="json")
        if "preset" not in settings.model_fields_set and project.world_preset:
            data["preset"] = project.world_preset
        if "custom_world" not in settings.model_fields_set:
            data["custom_world"] = project.custom_world
    else:
        data = dict(settings)
    provider = get_provider(str(data.get("provider") or "mock"))
    try:
        data["model"] = provider.get_model(data.get("model")).id
    except ProviderError as exc:
        raise ServiceError(exc.message, status_code=422, code="provider_unknown_model") from exc
    return data, provider


def existing_clip_refs(db: Session, project_id: str) -> list[dict[str, Any]]:
    """Ready clips of a project in the planner's ``existing_clips`` shape."""
    clips = db.scalars(
        select(Clip)
        .where(Clip.project_id == project_id, Clip.status == ClipStatus.READY, Clip.asset_id.is_not(None))
        .order_by(Clip.created_at, Clip.id)
    ).all()
    return [
        {
            "id": c.id,
            "scene_id": c.scene_id,
            "cache_key": c.cache_key,
            "duration": c.duration,
            "source": c.source,
            "status": c.status,
        }
        for c in clips
    ]


def build_storyboard(project: Project, settings: Mapping[str, Any], *, use_claude: bool) -> dict[str, Any] | None:
    """Storyboard for a batch (``None`` in library mode: uploads need no prompts)."""
    if settings.get("provider") == "library":
        return None
    return generate_storyboard(
        title=project.title,
        artist_name=project.artist_name,
        mood=project.mood,
        purpose=project.purpose,
        lyrics=list(project.lyric_lines or []),
        analysis=project.analysis,
        preset_id=str(settings.get("preset") or project.world_preset or "cinematic"),
        custom_world=settings.get("custom_world") or project.custom_world,
        n_scenes=None,
        seed=None,  # stable per song/preset, so later batches hit the clip cache
        use_claude=use_claude,
    )


def llm_estimate_usd(settings: Mapping[str, Any], n_scenes: int = DEFAULT_SCENES) -> float:
    """Expected cost of the Claude storyboard call (0 without a key or in library mode)."""
    app_settings = get_settings()
    if not app_settings.anthropic_configured or settings.get("provider") == "library":
        return 0.0
    usage = {
        "input_tokens": LLM_ESTIMATE_INPUT_TOKENS,
        "output_tokens": LLM_ESTIMATE_OUTPUT_TOKENS_BASE + LLM_ESTIMATE_OUTPUT_TOKENS_PER_SCENE * max(1, n_scenes),
    }
    return claude_cost_usd(app_settings.ANTHROPIC_MODEL, usage)


def _plan(
    project: Project,
    settings: Mapping[str, Any],
    storyboard: Mapping[str, Any] | None,
    provider: VideoProvider,
    existing: Sequence[Mapping[str, Any]],
    seed: int,
) -> dict[str, Any]:
    segment = None
    if project.segment_start is not None and project.segment_end is not None:
        segment = (float(project.segment_start), float(project.segment_end))
    return plan_batch(
        analysis=project.analysis or {},
        lyric_lines=list(project.lyric_lines or []),
        settings=settings,
        storyboard=storyboard or {"scenes": []},
        segment=segment,
        provider_clip_duration=provider.clip_duration_for(str(settings["model"]), cost_service.TARGET_CLIP_SEC),
        existing_clips=existing,
        seed=seed,
    )


def _require_analysis(project: Project) -> None:
    if project.audio_asset_id is None:
        raise ServiceError("先に楽曲をアップロードしてください。", status_code=422, code="audio_missing")
    if project.analysis_status != AnalysisStatus.DONE or not project.analysis:
        raise ServiceError(
            "楽曲解析が完了していません。解析の完了後に再度お試しください。", status_code=422, code="analysis_missing"
        )


def preview_batch(db: Session, project: Project, settings: GenerationSettings | Mapping[str, Any]) -> BatchPreview:
    """Plan a batch without side effects and price it (``POST /estimate`` and batch creation).

    The storyboard is always the deterministic template here (no paid Claude
    call); the expected Claude cost is added as ``llm_usd`` when a key is set.
    """
    _require_analysis(project)
    data, provider = resolve_generation_settings(project, settings)
    storyboard = build_storyboard(project, data, use_claude=False)
    seed = int(data["seed"]) if data.get("seed") is not None else 0
    plan = _plan(project, data, storyboard, provider, existing_clip_refs(db, project.id), seed)
    stats = plan["stats"]
    n_scenes = len((storyboard or {}).get("scenes") or []) or DEFAULT_SCENES
    limit = resolve_budget_limit(data, project)
    estimate = cost_service.estimate(
        data,
        stats,
        provider.id,
        str(data["model"]),
        cached_clips=0 if provider.kind == "library" else int(stats.get("reused_clips") or 0),
        llm_estimate_usd=llm_estimate_usd(data, n_scenes),
        budget_limit_usd=limit,
    )
    estimate["notes"] = list(dict.fromkeys([*plan.get("warnings", []), *estimate.get("notes", [])]))
    return BatchPreview(settings=data, storyboard=storyboard, plan=plan, estimate=estimate, budget_limit_usd=limit)


def create_batch(db: Session, project: Project, settings: GenerationSettings) -> Batch:
    """Validate the request (§5.6 guards), store a ``queued`` Batch and return it (not yet enqueued)."""
    _require_analysis(project)
    if not all_rights_confirmed(project.rights):
        raise ServiceError(
            "権利確認チェックリストのすべての項目を確認してから生成してください。",
            status_code=422,
            code="rights_unconfirmed",
        )
    data, provider = resolve_generation_settings(project, settings)
    ok, reason = provider.check_model(str(data["model"]))
    if not ok:
        raise ProviderNotConfigured(reason or f"{provider.name} は使用できません。", code="provider_not_configured")
    if provider.kind == "library" and not any(
        c["source"] == ClipSource.UPLOAD for c in existing_clip_refs(db, project.id)
    ):
        raise ServiceError(
            "「アップロード素材」で生成するには、先に動画素材をアップロードしてください。",
            status_code=422,
            code="library_empty",
        )
    if settings.seed is None:
        # Fix the seed now so the stored estimate describes exactly the plan the batch will run.
        settings = settings.model_copy(update={"seed": random.SystemRandom().randrange(1, 2**31 - 1)})
    preview = preview_batch(db, project, settings)
    estimate = preview.estimate
    if not estimate.get("within_budget", True):
        limit = preview.budget_limit_usd or 0.0
        total = float(estimate.get("total_usd") or 0.0)
        raise BudgetExceeded(
            f"見積もり {_money(total)} が予算上限 {_money(limit)} を超えています。"
            "本数・新規生成数・再利用設定を見直すか、予算上限を引き上げてください。",
            estimate_usd=total,
            limit_usd=limit,
        )
    batch = Batch(
        project_id=project.id,
        settings=preview.settings,
        estimate=estimate,
        status=BatchStatus.QUEUED,
    )
    db.add(batch)
    project.updated_at = utcnow()
    db.commit()
    return batch


# --- state transitions --------------------------------------------------------------------------------------------


def _transition_batch(db: Session, batch_id: str, allowed: Iterable[str], status: str, **values: Any) -> bool:
    result = db.execute(
        update(Batch)
        .where(Batch.id == batch_id, Batch.status.in_(list(allowed)))
        .values(status=status, **values)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return isinstance(result, CursorResult) and result.rowcount > 0


def _transition_video(db: Session, video_id: str, allowed: Iterable[str], status: str, **values: Any) -> bool:
    result = db.execute(
        update(Video)
        .where(Video.id == video_id, Video.status.in_(list(allowed)))
        .values(status=status, updated_at=utcnow(), **values)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return isinstance(result, CursorResult) and result.rowcount > 0


def _add_batch_cost(db: Session, batch_id: str | None, amount: float) -> None:
    if batch_id and amount:
        db.execute(
            update(Batch)
            .where(Batch.id == batch_id)
            .values(cost_usd=Batch.cost_usd + float(amount))
            .execution_options(synchronize_session=False)
        )


def _batch_clip_ids(batch: Batch) -> list[str]:
    """Clip rows this batch created (not cache hits)."""
    return [
        str(spec["clip_id"])
        for spec in (batch.plan or {}).get("clip_specs") or []
        if isinstance(spec, dict) and spec.get("clip_id") and not spec.get("existing_clip_id")
    ]


def _stop_batch(db: Session, batch_id: str, status: str, error: str, *, video_error: str) -> bool:
    """Move an active batch to a terminal status; cancel its waiting videos and pending clips."""
    if not _transition_batch(db, batch_id, ACTIVE_BATCH_STATUSES, status, error=error, finished_at=utcnow()):
        return False
    batch = db.get(Batch, batch_id)
    if batch is not None:
        db.refresh(batch)
    db.execute(
        update(Video)
        .where(Video.batch_id == batch_id, Video.status.in_([VideoStatus.QUEUED, VideoStatus.WAITING_CLIPS]))
        .values(status=VideoStatus.CANCELLED, error=video_error, updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    clip_ids = _batch_clip_ids(batch) if batch is not None else []
    if clip_ids:
        db.execute(
            update(Clip)
            .where(Clip.id.in_(clip_ids), Clip.status == ClipStatus.PENDING)
            .values(status=ClipStatus.CANCELLED, updated_at=utcnow())
            .execution_options(synchronize_session=False)
        )
    db.commit()
    return True


def cancel_batch(db: Session, batch: Batch) -> None:
    """User cancel (§7): pending/waiting videos → cancelled; running tasks stop at their next check."""
    if batch.status == BatchStatus.CANCELLED:
        return
    if batch.status not in ACTIVE_BATCH_STATUSES:
        raise ServiceError(
            "このバッチはすでに終了しているためキャンセルできません。", status_code=409, code="batch_finished"
        )
    _stop_batch(
        db,
        batch.id,
        BatchStatus.CANCELLED,
        "ユーザーによってキャンセルされました。",
        video_error="バッチがキャンセルされました。",
    )
    db.refresh(batch)


def reopen_batch(db: Session, batch: Batch) -> None:
    """A finished batch goes back to ``rendering`` while one of its videos is re-rendered."""
    if batch.status in _TERMINAL_REOPENABLE:
        batch.status = BatchStatus.RENDERING
        batch.finished_at = None
        batch.error = None


def recompute_batch_status(db: Session, batch_id: str) -> str | None:
    """Batch status from its videos once rendering has started (§7).

    ``completed`` = all done, ``partial`` = some done, ``failed`` = none done;
    ``rendering`` while any video is still active. The batch row is locked so
    concurrent render tasks cannot overwrite each other's result.
    """
    batch = db.execute(
        select(Batch).where(Batch.id == batch_id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if batch is None:
        db.commit()
        return None
    if batch.status not in _RECOMPUTABLE:
        db.commit()
        return batch.status
    statuses = list(db.scalars(select(Video.status).where(Video.batch_id == batch_id)))
    if any(s in ACTIVE_VIDEO_STATUSES for s in statuses):
        new_status = BatchStatus.RENDERING
    else:
        done = sum(1 for s in statuses if s == VideoStatus.DONE)
        if statuses and done == len(statuses):
            new_status = BatchStatus.COMPLETED
        elif done:
            new_status = BatchStatus.PARTIAL
        else:
            new_status = BatchStatus.FAILED
    if new_status != batch.status or (new_status != BatchStatus.RENDERING and batch.finished_at is None):
        batch.status = new_status
        if new_status == BatchStatus.RENDERING:
            batch.finished_at = None
        else:
            batch.finished_at = utcnow()
            if new_status == BatchStatus.FAILED and not batch.error:
                batch.error = "すべての動画の生成に失敗しました。各動画のエラーを確認してください。"
            elif new_status != BatchStatus.FAILED:
                batch.error = None
    db.commit()
    return new_status


# --- budget & cancellation guards ---------------------------------------------------------------------------------


class BudgetGuard:
    """Thread-safe spend tracker: committed spend + reservations of in-flight clips ≤ limit."""

    def __init__(self, limit_usd: float | None, spent_usd: float):
        self.limit = limit_usd
        self.spent = float(spent_usd or 0.0)
        self.reserved = 0.0
        self.exceeded = False
        self._lock = threading.Lock()

    def reserve(self, amount: float) -> bool:
        with self._lock:
            if self.exceeded:
                return False
            if self.limit is not None and amount > 0 and self.spent + self.reserved + amount > self.limit + 1e-9:
                self.exceeded = True
                return False
            self.reserved += amount
            return True

    def settle(self, reserved: float, actual: float) -> None:
        with self._lock:
            self.reserved = max(0.0, self.reserved - reserved)
            self.spent += float(actual or 0.0)


class CancelWatcher:
    """``cancel_check`` for providers/renders: is the batch (or video) cancelled? Throttled DB reads."""

    def __init__(self, batch_id: str | None, video_id: str | None = None):
        self.batch_id = batch_id
        self.video_id = video_id
        self._last = 0.0
        self._cancelled = False
        self._lock = threading.Lock()

    def __call__(self) -> bool:
        with self._lock:
            if self._cancelled:
                return True
            now = time.monotonic()
            if now - self._last < CANCEL_POLL_SEC:
                return False
            self._last = now
        cancelled = self._check()
        if cancelled:
            with self._lock:
                self._cancelled = True
        return cancelled

    def _check(self) -> bool:
        with SessionLocal() as db:
            if self.batch_id is not None:
                status = db.scalar(select(Batch.status).where(Batch.id == self.batch_id))
                if status is None or status == BatchStatus.CANCELLED:
                    return True
            if self.video_id is not None:
                status = db.scalar(select(Video.status).where(Video.id == self.video_id))
                if status is None or status == VideoStatus.CANCELLED:
                    return True
        return False


# --- task: audio analysis -----------------------------------------------------------------------------------------


def _default_segment(analysis: Mapping[str, Any]) -> tuple[float, float]:
    duration = _num(analysis.get("duration")) or DEFAULT_SEGMENT_SEC
    segments = [s for s in analysis.get("suggested_segments") or [] if isinstance(s, Mapping)]
    best = min(
        segments,
        key=lambda s: (abs((_num(s.get("duration")) or 0) - DEFAULT_SEGMENT_SEC), -(_num(s.get("score")) or 0)),
        default=None,
    )
    if best is not None and _num(best.get("start")) is not None and _num(best.get("end")) is not None:
        return float(best["start"]), float(best["end"])
    return 0.0, round(min(duration, DEFAULT_SEGMENT_SEC), 3)


def analyze_project_audio(project_id: str) -> None:
    """Analyse the project's song; sets ``analysis_status`` to ``done`` or ``failed``."""
    from app.services.audio_analysis import analyze_audio  # heavy import (librosa), only in workers

    with SessionLocal() as db:
        project = db.get(Project, project_id)
        if project is None:
            logger.info("analyze: project %s no longer exists", project_id)
            return
        asset = project.audio_asset
        if asset is None:
            project.analysis_status = AnalysisStatus.NONE
            db.commit()
            return
        asset_id, key, filename = asset.id, asset.storage_key, asset.filename
        project.analysis_status = AnalysisStatus.RUNNING
        project.analysis_error = None
        db.commit()

    work = _work_dir("analysis", project_id)
    analysis: dict[str, Any] | None = None
    error: str | None = None
    started = time.monotonic()
    try:
        path = local_input(get_storage(), key, work / f"audio{Path(filename).suffix.lower() or '.bin'}")
        analysis = analyze_audio(path)
    except ServiceError as exc:
        error = exc.message or "楽曲解析に失敗しました。"
    except StorageNotFound:
        error = "音源ファイルが見つかりません。もう一度アップロードしてください。"
    except Exception:
        logger.exception("analyze: unexpected failure for project %s", project_id)
        error = "楽曲解析中に予期しないエラーが発生しました。再解析を実行してください。"
    finally:
        shutil.rmtree(work, ignore_errors=True)

    with SessionLocal() as db:
        project = db.get(Project, project_id)
        if project is None or project.audio_asset_id != asset_id:
            logger.info("analyze: audio of project %s changed during analysis; result discarded", project_id)
            return
        if error is not None or analysis is None:
            project.analysis_status = AnalysisStatus.FAILED
            project.analysis_error = error or "楽曲解析に失敗しました。"
        else:
            project.analysis = analysis
            project.analysis_status = AnalysisStatus.DONE
            project.analysis_error = None
            if project.segment_start is None or project.segment_end is None:
                project.segment_start, project.segment_end = _default_segment(analysis)
        db.commit()
    logger.info(
        "analyze: project %s %s in %.1fs", project_id, "failed" if error else "done", time.monotonic() - started
    )


# --- task: batch --------------------------------------------------------------------------------------------------


def llm_cost_record(project_id: str, batch_id: str | None, storyboard: Mapping[str, Any]) -> CostRecord | None:
    amount = float(storyboard.get("cost_usd") or 0.0)
    if amount <= 0:
        return None
    usage = storyboard.get("usage") or {}
    tokens = sum(int(v or 0) for v in usage.values()) if isinstance(usage, Mapping) else 0
    return CostRecord(
        project_id=project_id,
        batch_id=batch_id,
        kind=CostKind.LLM,
        provider=ANTHROPIC_PROVIDER,
        model=storyboard.get("model"),
        units=float(tokens),
        unit=CostUnit.TOKEN,
        unit_price_usd=round(amount / tokens, 10) if tokens else 0.0,
        amount_usd=round(amount, 6),
        cached=False,
        note="絵コンテ生成（Claude）"
        + ("" if storyboard.get("source") == "claude" else "・テンプレートにフォールバック"),
    )


def _create_plan_rows(
    db: Session,
    *,
    project: Project,
    batch_id: str,
    provider: VideoProvider,
    model: str,
    plan: dict[str, Any],
    videos: Sequence[dict[str, Any]],
    record_cache_hits: bool = True,
) -> list[str]:
    """Clip rows for new clip specs (+ cache-hit cost records); resolves shot ``clip_id``s in ``videos``.

    Returns the ids of the newly created (pending) clips.
    """
    pps = cost_service.get_pricing(provider.id, model).price_per_second_usd if provider.kind != "library" else 0.0
    key_to_id: dict[str, str] = {}
    new_ids: list[str] = []
    for spec in plan.get("clip_specs") or []:
        existing = spec.get("existing_clip_id")
        if existing:
            spec["clip_id"] = existing
            if not record_cache_hits:
                key_to_id[str(spec.get("key"))] = str(existing)
                continue
            db.add(
                CostRecord(
                    project_id=project.id,
                    batch_id=batch_id,
                    clip_id=existing,
                    kind=CostKind.VIDEO_GENERATION,
                    provider=provider.id,
                    model=model,
                    units=float(spec.get("duration") or 0.0),
                    unit=CostUnit.SECOND,
                    unit_price_usd=pps,
                    amount_usd=0.0,
                    cached=True,
                    note="生成済み素材を再利用（キャッシュ）",
                )
            )
        else:
            clip = Clip(
                id=new_id(),
                project_id=project.id,
                source=provider.id,
                provider=provider.id,
                model=model,
                scene_id=spec.get("scene_id"),
                prompt=spec.get("prompt"),
                negative_prompt=spec.get("negative_prompt"),
                params={
                    "batch_id": batch_id,
                    "key": spec.get("key"),
                    "seed": spec.get("seed"),
                    "duration": spec.get("duration"),
                    "variant": spec.get("variant"),
                    "section": spec.get("section"),
                    "hints": spec.get("hints"),
                    "aspect_ratio": "9:16",
                },
                cache_key=spec.get("cache_key"),
                status=ClipStatus.PENDING,
                attempts=0,
            )
            db.add(clip)
            spec["clip_id"] = clip.id
            new_ids.append(clip.id)
        key_to_id[str(spec.get("key"))] = str(spec["clip_id"])
    for video_plan in videos:
        for shot in video_plan.get("shots") or []:
            if not shot.get("clip_id") and shot.get("clip_key"):
                shot["clip_id"] = key_to_id.get(str(shot["clip_key"]))
    return new_ids


def _plan_batch_rows(db: Session, batch: Batch) -> None:
    """Planning step: storyboard → plan → Clip/Video rows (one transaction)."""
    project = batch.project
    data, provider = resolve_generation_settings(project, dict(batch.settings or {}))
    storyboard = build_storyboard(project, data, use_claude=get_settings().anthropic_configured)
    spent = 0.0
    if storyboard is not None:
        record = llm_cost_record(project.id, batch.id, storyboard)
        if record is not None:
            db.add(record)
            spent += record.amount_usd
    seed = int(data["seed"]) if data.get("seed") is not None else stable_seed("batch", batch.id)
    plan = _plan(project, data, storyboard, provider, existing_clip_refs(db, project.id), seed)
    plan["seed"] = seed
    videos = plan.get("videos") or []
    _create_plan_rows(
        db, project=project, batch_id=batch.id, provider=provider, model=str(data["model"]), plan=plan, videos=videos
    )
    for video_plan in videos:
        db.add(
            Video(
                project_id=project.id,
                batch_id=batch.id,
                index=int(video_plan["index"]),
                title=str(video_plan.get("title") or f"#{int(video_plan['index']) + 1}"),
                status=VideoStatus.WAITING_CLIPS,
                progress=0.0,
                plan=video_plan,
            )
        )
    batch.settings = data
    batch.storyboard = storyboard
    batch.plan = plan
    batch.cost_usd = float(batch.cost_usd or 0.0) + spent
    db.commit()


def _clip_cost_estimate(clip: Clip) -> float:
    try:
        duration = float((clip.params or {}).get("duration") or cost_service.TARGET_CLIP_SEC)
        return cost_service.clip_cost_usd(clip.provider, clip.model, duration)
    except ServiceError:
        return 0.0


def _store_clip_result(db: Session, clip: Clip, result: Any, out_dir: Path) -> None:
    storage = get_storage()
    thumb = out_dir / "thumb.jpg"
    make_thumbnail(result.path, thumb, at=min(1.0, max(0.0, float(result.duration) / 2)))
    video_asset = Asset(
        id=new_id(),
        project_id=clip.project_id,
        owner_id=clip.project.owner_id,
        kind=AssetKind.CLIP,
        filename=f"{clip.provider}_{clip.scene_id or 'clip'}_{clip.id[:8]}.mp4",
        content_type="video/mp4",
        duration=float(result.duration),
        width=int(result.width),
        height=int(result.height),
        meta={"provider": clip.provider, "model": result.model, "external_job_id": result.external_job_id},
    )
    video_asset.storage_key = make_key(clip.project_id, AssetKind.CLIP, video_asset.id, ".mp4")
    thumb_asset = Asset(
        id=new_id(),
        project_id=clip.project_id,
        owner_id=clip.project.owner_id,
        kind=AssetKind.THUMBNAIL,
        filename=f"{clip.id[:8]}.jpg",
        content_type="image/jpeg",
    )
    thumb_asset.storage_key = make_key(clip.project_id, AssetKind.THUMBNAIL, thumb_asset.id, ".jpg")
    video_asset.size_bytes = storage.put_file(video_asset.storage_key, result.path, content_type="video/mp4", move=True)
    try:
        thumb_asset.size_bytes = storage.put_file(thumb_asset.storage_key, thumb, content_type="image/jpeg", move=True)
    except Exception:
        storage.delete(video_asset.storage_key)
        raise
    db.add_all([video_asset, thumb_asset])
    clip.asset = video_asset
    clip.thumbnail_asset = thumb_asset


def generate_clip(clip_id: str, *, cancel_check: Callable[[], bool] | None = None) -> str:
    """Generate one pending clip with retries (max :data:`MAX_CLIP_ATTEMPTS`); returns the final status.

    Retryable :class:`ProviderError`s are retried with backoff; cancellation
    (``cancel_check``) → ``cancelled``; anything else → ``failed`` with a
    Japanese error. Successful clips are stored, get a CostRecord and add
    their cost to the batch.
    """
    with SessionLocal() as db:
        clip = db.get(Clip, clip_id)
        if clip is None:
            return ClipStatus.FAILED
        if clip.status in (ClipStatus.READY, ClipStatus.CANCELLED):
            return clip.status
        params = dict(clip.params or {})
        batch_id = params.get("batch_id")
        provider = get_provider(clip.provider)
        request = ClipRequest(
            prompt=clip.prompt or "",
            negative_prompt=clip.negative_prompt or "",
            duration=float(params.get("duration") or cost_service.TARGET_CLIP_SEC),
            model=clip.model or provider.default_model(),
            aspect_ratio=str(params.get("aspect_ratio") or "9:16"),
            seed=params.get("seed"),
            scene=params.get("hints"),
        )
        pps = cost_service.get_pricing(provider.id, request.model).price_per_second_usd

        for attempt in range(1, MAX_CLIP_ATTEMPTS + 1):
            if cancel_check is not None and cancel_check():
                clip.status = ClipStatus.CANCELLED
                db.commit()
                return clip.status
            clip.status = ClipStatus.GENERATING
            clip.attempts = int(clip.attempts or 0) + 1
            clip.error = None
            db.commit()
            out_dir = _work_dir("clips", clip.id)
            try:
                result = provider.generate(
                    request, str(out_dir / "clip.mp4"), timeout=CLIP_TIMEOUT_SEC, cancel_check=cancel_check
                )
                _store_clip_result(db, clip, result, out_dir)
                amount = float(result.cost_usd or 0.0)
                units = float((result.raw or {}).get("requested_duration") or result.duration)
                clip.status = ClipStatus.READY
                clip.duration = float(result.duration)
                clip.width = int(result.width)
                clip.height = int(result.height)
                clip.cost_usd = amount
                clip.external_job_id = result.external_job_id
                clip.error = None
                db.add(
                    CostRecord(
                        project_id=clip.project_id,
                        batch_id=batch_id,
                        clip_id=clip.id,
                        kind=CostKind.VIDEO_GENERATION,
                        provider=clip.provider,
                        model=result.model,
                        units=units,
                        unit=CostUnit.SECOND,
                        unit_price_usd=pps,
                        amount_usd=round(amount, 6),
                        cached=False,
                        note=None,
                    )
                )
                _add_batch_cost(db, batch_id, amount)
                db.commit()
                return clip.status
            except ProviderCancelled:
                db.rollback()
                clip.status = ClipStatus.CANCELLED
                clip.error = "生成がキャンセルされました。"
                db.commit()
                return clip.status
            except ProviderNotConfigured as exc:
                db.rollback()
                clip.status = ClipStatus.FAILED
                clip.error = exc.message
                db.commit()
                return clip.status
            except ProviderError as exc:
                db.rollback()
                if isinstance(exc, ProviderTaskError):
                    clip.external_job_id = exc.external_job_id or clip.external_job_id
                    clip.params = {**dict(clip.params or {}), "failure_code": exc.failure_code}
                clip.error = exc.message or "動画素材の生成に失敗しました。"
                if exc.retryable and attempt < MAX_CLIP_ATTEMPTS:
                    clip.status = ClipStatus.PENDING
                    db.commit()
                    delay = RETRY_BACKOFF_SEC[min(attempt - 1, len(RETRY_BACKOFF_SEC) - 1)]
                    logger.warning("clip %s attempt %d failed (%s); retrying in %.0fs", clip.id, attempt, exc, delay)
                    if _wait_or_cancel(delay, cancel_check):
                        clip.status = ClipStatus.CANCELLED
                        db.commit()
                        return clip.status
                    continue
                clip.status = ClipStatus.FAILED
                db.commit()
                return clip.status
            except Exception:
                logger.exception("clip %s: unexpected failure", clip.id)
                db.rollback()
                clip.status = ClipStatus.FAILED
                clip.error = "動画素材の生成中に予期しないエラーが発生しました。"
                db.commit()
                return clip.status
            finally:
                shutil.rmtree(out_dir, ignore_errors=True)
        return clip.status


def _wait_or_cancel(seconds: float, cancel_check: Callable[[], bool] | None) -> bool:
    """Sleep ``seconds`` in small steps; True when cancelled meanwhile."""
    deadline = time.monotonic() + seconds
    while True:
        if cancel_check is not None and cancel_check():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        _sleep(min(1.0, remaining))


def _generate_with_budget(clip_id: str, guard: BudgetGuard, cancel: Callable[[], bool]) -> str:
    """Budget check right before a (possibly paid) clip starts, then generate it."""
    with SessionLocal() as db:
        clip = db.get(Clip, clip_id)
        if clip is None or clip.status not in (ClipStatus.PENDING, ClipStatus.GENERATING):
            return clip.status if clip is not None else ClipStatus.FAILED
        estimate = _clip_cost_estimate(clip)
        if cancel():
            clip.status = ClipStatus.CANCELLED
            db.commit()
            return clip.status
        if not guard.reserve(estimate):
            clip.status = ClipStatus.CANCELLED
            clip.error = "予算上限に達したため生成しませんでした。"
            db.commit()
            return clip.status
    status: str = ClipStatus.FAILED
    actual = 0.0
    try:
        status = generate_clip(clip_id, cancel_check=cancel)
        if status == ClipStatus.READY:
            with SessionLocal() as db:
                actual = float(db.scalar(select(Clip.cost_usd).where(Clip.id == clip_id)) or 0.0)
    finally:
        guard.settle(estimate, actual)
    return status


def _generate_clips(clip_ids: Sequence[str], guard: BudgetGuard, cancel: Callable[[], bool]) -> dict[str, str]:
    if not clip_ids:
        return {}
    results: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=CLIP_CONCURRENCY, thread_name_prefix="mvf-clip") as pool:
        futures = {pool.submit(_generate_with_budget, cid, guard, cancel): cid for cid in clip_ids}
        for future, cid in futures.items():
            try:
                results[cid] = future.result()
            except Exception:
                logger.exception("clip %s: generation task crashed", cid)
                results[cid] = ClipStatus.FAILED
    return results


def _ready_clips(db: Session, clip_ids: Iterable[str]) -> dict[str, Clip]:
    ids = list(dict.fromkeys(i for i in clip_ids if i))
    if not ids:
        return {}
    clips = db.scalars(select(Clip).where(Clip.id.in_(ids))).all()
    return {c.id: c for c in clips if c.status == ClipStatus.READY and c.asset_id is not None}


def resolve_plan_clips(
    db: Session, plan: dict[str, Any], project_id: str, *, preferred: Sequence[str] = ()
) -> tuple[bool, int]:
    """Make every shot point at a ready clip, substituting failed/missing ones.

    Substitutes come from ``preferred`` (the batch's own clips) first, then any
    ready clip of the project; same storyboard scene first, least used next,
    never the neighbouring shot's clip when avoidable. ``src_in`` is clamped
    into the new clip. Returns ``(ok, substitutions)``; ``ok`` is False when a
    shot cannot be filled at all.
    """
    shots = plan.get("shots") or []
    if not shots:
        return False, 0
    referenced = [str(s.get("clip_id") or "") for s in shots]
    ready = _ready_clips(db, referenced)
    if all(cid in ready for cid in referenced):
        return True, 0
    candidates = _ready_clips(db, preferred)
    for clip in db.scalars(
        select(Clip)
        .where(Clip.project_id == project_id, Clip.status == ClipStatus.READY, Clip.asset_id.is_not(None))
        .order_by(Clip.created_at, Clip.id)
    ):
        candidates.setdefault(clip.id, clip)
    if not candidates:
        return False, 0
    usage = Counter(cid for cid in referenced if cid in ready)
    order = list(candidates)
    substitutions = 0
    for index, shot in enumerate(shots):
        cid = str(shot.get("clip_id") or "")
        if cid in ready:
            continue
        neighbours = {str(shots[j].get("clip_id") or "") for j in (index - 1, index + 1) if 0 <= j < len(shots)}

        def rank(candidate: str, *, shot: Mapping[str, Any] = shot, neighbours: set[str] = neighbours) -> tuple:
            clip = candidates[candidate]
            return (
                candidate in neighbours and len(candidates) > 1,
                clip.scene_id != shot.get("scene_id"),
                usage[candidate],
                order.index(candidate),
            )

        chosen = min(candidates, key=rank)
        clip = candidates[chosen]
        need = float(shot.get("duration") or 0.0) * float(shot.get("speed") or 1.0)
        room = max(0.0, float(clip.duration or 0.0) - need - 0.05)
        shot["substituted_from"] = cid or None
        shot["clip_id"] = chosen
        shot["clip_key"] = None
        shot["src_in"] = round(min(float(shot.get("src_in") or 0.0), room), 3)
        usage[chosen] += 1
        substitutions += 1
    return True, substitutions


def _start_rendering(batch_id: str) -> None:
    """After clip generation: resolve shots, queue renderable videos, move the batch to ``rendering``."""
    with SessionLocal() as db:
        batch = db.get(Batch, batch_id)
        if batch is None or batch.status != BatchStatus.GENERATING_CLIPS:
            return
        pool = [
            str(spec.get("clip_id"))
            for spec in (batch.plan or {}).get("clip_specs") or []
            if isinstance(spec, dict) and spec.get("clip_id")
        ]
        videos = db.scalars(
            select(Video)
            .where(Video.batch_id == batch_id, Video.status == VideoStatus.WAITING_CLIPS)
            .order_by(Video.index)
        ).all()
        queued: list[str] = []
        for video in videos:
            plan = copy.deepcopy(dict(video.plan or {}))
            ok, substitutions = resolve_plan_clips(db, plan, batch.project_id, preferred=pool)
            if substitutions:
                plan["substitutions"] = int(plan.get("substitutions") or 0) + substitutions
            if ok:
                if _transition_video(db, video.id, [VideoStatus.WAITING_CLIPS], VideoStatus.QUEUED, plan=plan):
                    queued.append(video.id)
            else:
                _transition_video(
                    db,
                    video.id,
                    [VideoStatus.WAITING_CLIPS],
                    VideoStatus.FAILED,
                    error="使用できる動画素材がないためレンダリングできませんでした（素材の生成に失敗しました）。",
                )
        if not _transition_batch(db, batch_id, [BatchStatus.GENERATING_CLIPS], BatchStatus.RENDERING):
            return
    for video_id in queued:
        worker.enqueue("render_video", video_id)
    with SessionLocal() as db:
        recompute_batch_status(db, batch_id)


def run_batch(batch_id: str) -> None:
    """Batch task (§7): plan → generate clips → enqueue renders. Resumable."""
    with SessionLocal() as db:
        batch = db.get(Batch, batch_id)
        if batch is None:
            logger.info("run_batch: batch %s no longer exists", batch_id)
            return
        if batch.status not in _UPSTREAM:
            logger.info("run_batch: batch %s is %s; nothing to do", batch_id, batch.status)
            return
        try:
            if batch.plan is None:
                if not _transition_batch(
                    db, batch_id, [BatchStatus.QUEUED, BatchStatus.PLANNING], BatchStatus.PLANNING, started_at=utcnow()
                ):
                    return
                db.refresh(batch)
                _plan_batch_rows(db, batch)
            if not _transition_batch(
                db, batch_id, [BatchStatus.PLANNING, BatchStatus.GENERATING_CLIPS], BatchStatus.GENERATING_CLIPS
            ):
                return
            db.refresh(batch)
            limit = _num((batch.estimate or {}).get("budget_limit_usd"))
            todo = [
                c.id
                for c in db.scalars(select(Clip).where(Clip.id.in_(_batch_clip_ids(batch)))).all()
                if c.status in (ClipStatus.PENDING, ClipStatus.GENERATING)
            ]
            todo.sort(key=_batch_clip_ids(batch).index)
            spent = float(batch.cost_usd or 0.0)
            needed = sum(_clip_cost_estimate(c) for c in db.scalars(select(Clip).where(Clip.id.in_(todo))).all())
            if limit is not None and needed > 0 and spent + needed > limit + 1e-9:
                _stop_batch(
                    db,
                    batch_id,
                    BatchStatus.BUDGET_EXCEEDED,
                    f"予定している新規生成 {_money(needed)}（使用済み {_money(spent)}）が"
                    f"予算上限 {_money(limit)} を超えるため、生成を開始しませんでした。",
                    video_error="予算上限に達したため中止しました。",
                )
                return
        except ServiceError as exc:
            logger.warning("run_batch %s: %s", batch_id, exc)
            db.rollback()
            _stop_batch(
                db,
                batch_id,
                BatchStatus.FAILED,
                exc.message or "バッチの準備に失敗しました。",
                video_error="バッチの準備に失敗しました。",
            )
            return
        except Exception:
            logger.exception("run_batch %s: planning failed", batch_id)
            db.rollback()
            _stop_batch(
                db,
                batch_id,
                BatchStatus.FAILED,
                "バッチの準備中に予期しないエラーが発生しました。",
                video_error="バッチの準備に失敗しました。",
            )
            return

    guard = BudgetGuard(limit, spent)
    cancel = CancelWatcher(batch_id)
    started = time.monotonic()
    try:
        results = _generate_clips(todo, guard, cancel)
    except Exception:
        logger.exception("run_batch %s: clip generation crashed", batch_id)
        with SessionLocal() as db:
            _stop_batch(
                db,
                batch_id,
                BatchStatus.FAILED,
                "素材の生成中に予期しないエラーが発生しました。",
                video_error="素材の生成に失敗しました。",
            )
        return
    logger.info(
        "run_batch %s: %d clips in %.1fs (%s)",
        batch_id,
        len(todo),
        time.monotonic() - started,
        dict(Counter(results.values())),
    )
    if guard.exceeded:
        with SessionLocal() as db:
            _stop_batch(
                db,
                batch_id,
                BatchStatus.BUDGET_EXCEEDED,
                f"予算上限 {_money(guard.limit or 0.0)} に達したため、残りの素材の生成を中止しました"
                f"（使用済み {_money(guard.spent)}）。",
                video_error="予算上限に達したため中止しました。",
            )
        return
    _start_rendering(batch_id)


# --- task: render -------------------------------------------------------------------------------------------------


class _RenderProgress:
    """``progress_cb`` for the renderer: throttled progress writes + cancellation."""

    def __init__(self, video_id: str, batch_id: str):
        self.video_id = video_id
        self.batch_id = batch_id
        self._last = 0.0

    def __call__(self, fraction: float, stage: str) -> None:
        now = time.monotonic()
        if now - self._last < PROGRESS_WRITE_SEC and fraction < 1.0:
            return
        self._last = now
        with SessionLocal() as db:
            row = db.execute(
                select(Video.status, Batch.status)
                .join(Batch, Batch.id == Video.batch_id)
                .where(Video.id == self.video_id)
            ).one_or_none()
            if row is None or row[0] != VideoStatus.RENDERING or row[1] == BatchStatus.CANCELLED:
                raise JobCancelled(self.video_id)
            db.execute(
                update(Video)
                .where(Video.id == self.video_id, Video.status == VideoStatus.RENDERING)
                .values(progress=round(0.02 + 0.93 * max(0.0, min(1.0, fraction)), 3))
                .execution_options(synchronize_session=False)
            )
            db.commit()


def _generate_pending_for_video(db: Session, video: Video, batch: Batch) -> None:
    """Clips a regenerated plan still needs (status ``pending``) are generated here, within the batch budget."""
    ids = [str(s.get("clip_id")) for s in (video.plan or {}).get("shots") or [] if s.get("clip_id")]
    pending = [
        c.id
        for c in db.scalars(select(Clip).where(Clip.id.in_(list(dict.fromkeys(ids))))).all()
        if c.status in (ClipStatus.PENDING, ClipStatus.GENERATING)
    ]
    if not pending:
        return
    guard = BudgetGuard(_num((batch.estimate or {}).get("budget_limit_usd")), float(batch.cost_usd or 0.0))
    _generate_clips(pending, guard, CancelWatcher(batch.id, video.id))


def _fail_video(video_id: str, message: str, **values: Any) -> None:
    with SessionLocal() as db:
        _transition_video(
            db,
            video_id,
            [VideoStatus.QUEUED, VideoStatus.WAITING_CLIPS, VideoStatus.RENDERING],
            VideoStatus.FAILED,
            error=message,
            **values,
        )


def _cancel_video(video_id: str) -> None:
    with SessionLocal() as db:
        _transition_video(
            db,
            video_id,
            [VideoStatus.QUEUED, VideoStatus.WAITING_CLIPS, VideoStatus.RENDERING],
            VideoStatus.CANCELLED,
            error="バッチがキャンセルされました。",
        )


def _quality_error(report: Mapping[str, Any]) -> str:
    failed = [c for c in report.get("checks") or [] if not c.get("passed")]
    detail = ", ".join(f"{c.get('name')}={c.get('value')}（期待値 {c.get('expected')}）" for c in failed)
    return (
        f"出力ファイルの品質チェックに失敗しました: {detail}"
        if detail
        else "出力ファイルの品質チェックに失敗しました。"
    )


def render_video(video_id: str) -> None:
    """Render task (§7): inputs → FFmpeg → validate → upload → ``done``; recomputes the batch status."""
    with SessionLocal() as db:
        video = db.get(Video, video_id)
        if video is None:
            logger.info("render_video: video %s no longer exists", video_id)
            return
        batch_id = video.batch_id
        batch = video.batch
        if batch.status == BatchStatus.CANCELLED:
            _cancel_video(video_id)
            recompute_batch_status(db, batch_id)
            return
        if video.status not in (VideoStatus.QUEUED, VideoStatus.WAITING_CLIPS, VideoStatus.RENDERING):
            logger.info("render_video: video %s is %s; nothing to do", video_id, video.status)
            return

    work = _work_dir("renders", video_id)
    try:
        _render_once(video_id, batch_id, work)
    except JobCancelled:
        _cancel_video(video_id)
    except RenderError as exc:
        logger.error("render_video %s failed: %s\n%s", video_id, exc.message, exc.stderr_tail)
        _fail_video(video_id, exc.message or "動画のレンダリングに失敗しました。")
    except ServiceError as exc:
        logger.warning("render_video %s failed: %s", video_id, exc)
        _fail_video(video_id, exc.message or "動画のレンダリングに失敗しました。")
    except StorageNotFound as exc:
        logger.warning("render_video %s: missing input %s", video_id, exc)
        _fail_video(video_id, "入力ファイル（音源または素材）が見つかりません。")
    except Exception:
        logger.exception("render_video %s: unexpected failure", video_id)
        _fail_video(video_id, "レンダリング中に予期しないエラーが発生しました。再試行してください。")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        with SessionLocal() as db:
            recompute_batch_status(db, batch_id)


def _render_once(video_id: str, batch_id: str, work: Path) -> None:
    storage = get_storage()
    with SessionLocal() as db:
        video = db.get(Video, video_id)
        batch = db.get(Batch, batch_id)
        if video is None or batch is None:
            raise JobCancelled(video_id)
        _generate_pending_for_video(db, video, batch)
        db.refresh(video)
        db.refresh(batch)
        if batch.status == BatchStatus.CANCELLED or video.status == VideoStatus.CANCELLED:
            raise JobCancelled(video_id)

        plan = copy.deepcopy(dict(video.plan or {}))
        preferred = [str(s.get("clip_id")) for s in (batch.plan or {}).get("clip_specs") or [] if s.get("clip_id")]
        ok, substitutions = resolve_plan_clips(db, plan, video.project_id, preferred=preferred)
        if not ok:
            raise ServiceError("使用できる動画素材がないためレンダリングできませんでした。", code="no_clips")
        if substitutions:
            plan["substitutions"] = int(plan.get("substitutions") or 0) + substitutions
        if not _transition_video(
            db,
            video_id,
            [VideoStatus.QUEUED, VideoStatus.WAITING_CLIPS, VideoStatus.RENDERING],
            VideoStatus.RENDERING,
            plan=plan,
            progress=0.01,
            error=None,
            attempts=Video.attempts + 1,
        ):
            raise JobCancelled(video_id)

        project = video.project
        if project.audio_asset is None:
            raise ServiceError("音源がありません。楽曲をアップロードしてください。", code="audio_missing")
        audio_key = project.audio_asset.storage_key
        audio_ext = Path(project.audio_asset.filename).suffix.lower() or ".bin"
        analysis = dict(project.analysis or {})
        lines = effective_lyric_lines(project.lyric_lines, plan.get("lyric_offsets"))
        clips = _ready_clips(db, [str(s["clip_id"]) for s in plan["shots"]])
        clip_keys = {cid: clip.asset.storage_key for cid, clip in clips.items() if clip.asset is not None}
        owner_id = project.owner_id
        project_id = project.id
        filename = download_filename(project, video)
        old_asset_ids = [a for a in (video.asset_id, video.thumbnail_asset_id) if a]
        render_version = video.render_version

    inputs = work / "inputs"
    audio_path = local_input(storage, audio_key, inputs / f"audio{audio_ext}")
    clip_paths: dict[str, Path] = {}
    shots: list[render_service.RenderShot] = []
    for shot in plan["shots"]:
        cid = str(shot["clip_id"])
        if cid not in clip_paths:
            if cid not in clip_keys:
                raise ServiceError("動画素材が見つかりません。", code="clip_missing")
            clip_paths[cid] = local_input(storage, clip_keys[cid], inputs / f"{cid}.mp4")
        shots.append(
            render_service.RenderShot(
                clip_path=str(clip_paths[cid]),
                src_in=float(shot.get("src_in") or 0.0),
                duration=float(shot["duration"]),
                hflip=bool(shot.get("hflip")),
                zoom_from=float(shot.get("zoom_from") or 1.0),
                zoom_to=float(shot.get("zoom_to") or 1.0),
                pan=str(shot.get("pan") or "center"),
                speed=float(shot.get("speed") or 1.0),
            )
        )
    duration = float(plan["duration"])
    spec = render_service.RenderSpec(
        shots=shots,
        audio_path=str(audio_path),
        audio_start=float(plan.get("start") or 0.0),
        duration=duration,
        lyric_lines=lines,
        lyric_style=str(plan.get("lyric_style") or "simple"),
        text_style=dict(plan.get("text_style") or {}),
        color_grade=str(plan.get("color_grade") or "natural"),
        output_path=str(work / "out" / "video.mp4"),
        work_dir=str(work / "render"),
        beats=[float(b) for b in analysis.get("beats") or []] or None,
        transition=str(plan.get("transition") or "cut"),
    )
    result = render_service.render_video(spec, progress_cb=_RenderProgress(video_id, batch_id))
    report = quality_service.validate_output(result.output_path, expected_duration=duration)
    if not report.get("passed"):
        _fail_video(video_id, _quality_error(report), quality=report, progress=0.0)
        return

    video_asset = Asset(
        id=new_id(),
        project_id=project_id,
        owner_id=owner_id,
        kind=AssetKind.RENDER,
        filename=filename,
        content_type="video/mp4",
        duration=float(result.duration),
        width=spec.width,
        height=spec.height,
        meta={"render_version": render_version, "timings": dict(result.timings or {})},
    )
    video_asset.storage_key = make_key(project_id, AssetKind.RENDER, video_asset.id, ".mp4")
    thumb_asset = Asset(
        id=new_id(),
        project_id=project_id,
        owner_id=owner_id,
        kind=AssetKind.THUMBNAIL,
        filename=f"{Path(filename).stem}.jpg",
        content_type="image/jpeg",
    )
    thumb_asset.storage_key = make_key(project_id, AssetKind.THUMBNAIL, thumb_asset.id, ".jpg")
    video_asset.size_bytes = storage.put_file(
        video_asset.storage_key, result.output_path, content_type="video/mp4", move=True
    )
    thumb_asset.size_bytes = storage.put_file(
        thumb_asset.storage_key, result.thumbnail_path, content_type="image/jpeg", move=True
    )

    with SessionLocal() as db:
        db.add_all([video_asset, thumb_asset])
        db.flush()
        finished = _transition_video(
            db,
            video_id,
            [VideoStatus.RENDERING],
            VideoStatus.DONE,
            asset_id=video_asset.id,
            thumbnail_asset_id=thumb_asset.id,
            quality=report,
            progress=1.0,
            error=None,
            rendered_at=utcnow(),
        )
        if not finished:
            # Cancelled while finishing: drop the new files.
            db.execute(
                Asset.__table__.delete().where(Asset.id.in_([video_asset.id, thumb_asset.id]))  # type: ignore[attr-defined]
            )
            db.commit()
            for key in (video_asset.storage_key, thumb_asset.storage_key):
                storage.delete(key)
            raise JobCancelled(video_id)
        old_keys = [a.storage_key for a in db.scalars(select(Asset).where(Asset.id.in_(old_asset_ids)))]
        if old_asset_ids:
            db.execute(Asset.__table__.delete().where(Asset.id.in_(old_asset_ids)))  # type: ignore[attr-defined]
            db.commit()
    for key in old_keys:
        try:
            storage.delete(key)
        except Exception:
            logger.exception("could not delete replaced render file %s", key)
    logger.info("render_video %s done (%.1fs, %d bytes)", video_id, result.timings.get("total", 0.0), result.size_bytes)


# --- video edits --------------------------------------------------------------------------------------------------


def _require_idle(video: Video) -> None:
    if video.status in ACTIVE_VIDEO_STATUSES:
        raise ServiceError("この動画は生成中です。完了してから操作してください。", status_code=409, code="video_busy")


def prepare_retry(db: Session, video: Video) -> None:
    """``POST /videos/{id}/retry``: failed/cancelled videos are queued again with the same plan."""
    if video.status not in (VideoStatus.FAILED, VideoStatus.CANCELLED):
        raise ServiceError(
            "失敗またはキャンセルされた動画のみ再試行できます。", status_code=409, code="video_not_retryable"
        )
    video.status = VideoStatus.QUEUED
    video.error = None
    video.progress = 0.0
    reopen_batch(db, video.batch)
    db.commit()


def apply_video_edit(db: Session, video: Video, edit: VideoEdit) -> None:
    """Merge :class:`VideoEdit` overrides into the plan, bump ``render_version`` and queue a re-render."""
    _require_idle(video)
    project = video.project
    plan = copy.deepcopy(dict(video.plan or {}))
    duration = float(plan.get("duration") or 0.0)
    song = song_duration(project)
    if edit.start is not None:
        latest = (song - duration) if song is not None else None
        if latest is not None and edit.start > latest + 0.01:
            raise ServiceError(
                f"開始位置は 0〜{max(0.0, latest):.2f} 秒の範囲で指定してください。",
                status_code=422,
                code="start_out_of_range",
            )
        plan["start"] = round(min(float(edit.start), max(0.0, latest)) if latest is not None else float(edit.start), 3)
    if edit.lyric_style is not None:
        plan["lyric_style"] = edit.lyric_style
    if edit.text_style is not None:
        plan["text_style"] = edit.text_style.model_dump(exclude_none=True)
    if edit.color_grade is not None:
        if edit.color_grade not in COLOR_GRADES:
            raise ServiceError(
                f"不明なカラーグレードです: {edit.color_grade}", status_code=422, code="color_grade_unknown"
            )
        plan["color_grade"] = edit.color_grade
    if edit.lyric_offsets:
        known = {str(line.get("id")) for line in project.lyric_lines or []}
        unknown = sorted(set(edit.lyric_offsets) - known)
        if unknown:
            raise ServiceError(f"不明な歌詞行です: {', '.join(unknown)}", status_code=422, code="lyric_line_unknown")
        offsets = dict(plan.get("lyric_offsets") or {})
        for line_id, times in edit.lyric_offsets.items():
            offsets[line_id] = {"start": round(times.start, 3), "end": round(times.end, 3)}
        plan["lyric_offsets"] = offsets
    if edit.shots:
        shots = plan.get("shots") or []
        for change in edit.shots:
            if change.index >= len(shots):
                raise ServiceError(
                    f"ショット番号 {change.index} は存在しません。", status_code=422, code="shot_unknown"
                )
            clip = db.get(Clip, change.clip_id)
            if clip is None or clip.project_id != project.id:
                raise ServiceError("指定された素材が見つかりません。", status_code=422, code="clip_unknown")
            if clip.status != ClipStatus.READY or clip.asset_id is None:
                raise ServiceError("使用できる状態の素材ではありません。", status_code=422, code="clip_not_ready")
            shot = shots[change.index]
            need = float(shot.get("duration") or 0.0) * float(shot.get("speed") or 1.0)
            room = max(0.0, float(clip.duration or 0.0) - need - 0.05)
            wanted = change.src_in if change.src_in is not None else float(shot.get("src_in") or 0.0)
            shot.update(
                {"clip_id": clip.id, "clip_key": None, "scene_id": clip.scene_id, "src_in": round(min(wanted, room), 3)}
            )
            shot.pop("substituted_from", None)
        plan["shots"] = shots
    plan["edited"] = True
    video.plan = plan
    video.render_version = int(video.render_version or 1) + 1
    video.status = VideoStatus.QUEUED
    video.error = None
    video.progress = 0.0
    reopen_batch(db, video.batch)
    db.commit()


def regenerate_video_plan(db: Session, video: Video, *, new_clips: bool) -> None:
    """``POST /videos/{id}/regenerate``: a new plan variant (new seed) for one video, then re-render.

    Without ``new_clips`` only ready clips are used (no spend). With it, fresh
    clips are planned (pending Clip rows, generated by the render task within
    the batch budget).
    """
    _require_idle(video)
    batch = video.batch
    project = video.project
    _require_analysis(project)
    data, provider = resolve_generation_settings(project, dict(batch.settings or {}))
    data["count"] = 1
    if new_clips:
        if provider.kind == "library":
            raise ServiceError(
                "アップロード素材モードでは新しい素材を生成できません。", status_code=422, code="library_no_generation"
            )
        ok, reason = provider.check_model(str(data["model"]))
        if not ok:
            raise ProviderNotConfigured(reason or f"{provider.name} は使用できません。", code="provider_not_configured")
        data["fresh_clips"] = True
    else:
        data["max_new_clips"] = 0
    seed = random.SystemRandom().randrange(1, 2**31 - 1)
    storyboard = batch.storyboard
    if provider.kind != "library" and not (storyboard or {}).get("scenes"):
        storyboard = build_storyboard(project, data, use_claude=False)
    plan = _plan(project, data, storyboard, provider, existing_clip_refs(db, project.id), seed)
    video_plan = plan["videos"][0]
    new_ids = _create_plan_rows(
        db,
        project=project,
        batch_id=batch.id,
        provider=provider,
        model=str(data["model"]),
        plan=plan,
        videos=[video_plan],
        record_cache_hits=False,
    )
    if new_ids:
        db.flush()  # SessionLocal has autoflush off; the pending clips must be visible to the query
        needed = sum(_clip_cost_estimate(c) for c in db.scalars(select(Clip).where(Clip.id.in_(new_ids))).all())
        limit = _num((batch.estimate or {}).get("budget_limit_usd"))
        spent = float(batch.cost_usd or 0.0)
        if limit is not None and needed > 0 and spent + needed > limit + 1e-9:
            db.rollback()
            raise BudgetExceeded(
                f"新しい素材の生成 {_money(needed)} を加えると予算上限 {_money(limit)} を超えます"
                f"（使用済み {_money(spent)}）。",
                estimate_usd=spent + needed,
                limit_usd=limit,
            )
    video_plan.update({"index": video.index, "title": video.title, "seed": seed, "regenerated": True})
    video.plan = video_plan
    video.render_version = int(video.render_version or 1) + 1
    video.status = VideoStatus.WAITING_CLIPS if new_ids else VideoStatus.QUEUED
    video.error = None
    video.progress = 0.0
    reopen_batch(db, batch)
    db.commit()


# --- recovery (inline mode) ---------------------------------------------------------------------------------------


def clean_stale_work_dirs(max_age_sec: float = STALE_WORK_SEC) -> int:
    """Remove task scratch directories left behind by a killed process; returns how many."""
    root = get_settings().WORK_DIR
    cutoff = time.time() - max_age_sec
    removed = 0
    for sub in WORK_SUBDIRS:
        base = root / sub
        if not base.is_dir():
            continue
        for entry in base.iterdir():
            try:
                if entry.is_dir() and not entry.is_symlink() and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
    return removed


def recover_interrupted_jobs() -> dict[str, int]:
    """Re-enqueue work a previous API process left unfinished (inline mode only).

    Inline tasks live in the API process, so a restart drops them; Celery
    re-delivers its own (``acks_late``). Analyses, active batches and
    queued/rendering videos are resumed — every task is idempotent. Old
    scratch directories of the dead process are removed as well.
    """
    removed = clean_stale_work_dirs()
    if removed:
        logger.info("removed %d stale work directories", removed)
    counts = {"analyses": 0, "batches": 0, "videos": 0}
    with SessionLocal() as db:
        projects = db.scalars(
            select(Project.id).where(
                Project.analysis_status.in_([AnalysisStatus.QUEUED, AnalysisStatus.RUNNING]),
                Project.audio_asset_id.is_not(None),
            )
        ).all()
        batches = db.execute(select(Batch.id, Batch.status).where(Batch.status.in_(ACTIVE_BATCH_STATUSES))).all()
        for batch_id, status in batches:
            if status == BatchStatus.RENDERING:
                recompute_batch_status(db, batch_id)
        videos = db.scalars(
            select(Video.id)
            .join(Batch, Batch.id == Video.batch_id)
            .where(
                Batch.status == BatchStatus.RENDERING,
                Video.status.in_([VideoStatus.QUEUED, VideoStatus.WAITING_CLIPS, VideoStatus.RENDERING]),
            )
            .order_by(Video.created_at)
        ).all()
    for project_id in projects:
        worker.enqueue("analyze", project_id)
        counts["analyses"] += 1
    for batch_id, status in batches:
        if status in _UPSTREAM:
            worker.enqueue("run_batch", batch_id)
            counts["batches"] += 1
    for video_id in videos:
        worker.enqueue("render_video", video_id)
        counts["videos"] += 1
    if any(counts.values()):
        logger.warning("resumed interrupted jobs: %s", counts)
    return counts


worker.register_task("analyze", analyze_project_audio)
worker.register_task("run_batch", run_batch)
worker.register_task("render_video", render_video)
