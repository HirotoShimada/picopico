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
import difflib
import hashlib
import inspect
import json
import logging
import math
import random
import re
import shutil
import threading
import time
import unicodedata
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from app.serializers import all_rights_confirmed, video_clip_ids
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
from app.services.llm import configured_model as llm_configured_model
from app.services.media import make_thumbnail, sanitize_filename
from app.services.presets import COLOR_GRADES
from app.services.providers import ClipRequest, ProviderCancelled, ProviderTaskError, VideoProvider, get_provider
from app.services.providers.base import ApiVideoProvider
from app.services.storyboard import DEFAULT_SCENES, claude_cost_usd, generate_storyboard
from app.services.variation import plan_batch
from app.storage import Storage, StorageNotFound, get_storage, make_key

logger = logging.getLogger(__name__)

__all__ = [
    "CLIP_CONCURRENCY",
    "MAX_CLIP_ATTEMPTS",
    "BatchPreview",
    "abandon_video",
    "analyze_project_audio",
    "apply_video_edit",
    "cancel_batch",
    "carry_over_lyric_lines",
    "clean_stale_work_dirs",
    "create_batch",
    "download_filename",
    "effective_lyric_lines",
    "generate_clip",
    "lyric_ids_in_use",
    "preview_batch",
    "recompute_batch_status",
    "recover_interrupted_jobs",
    "regenerate_video_plan",
    "render_video",
    "reopen_batch",
    "require_rights",
    "retime_line",
    "revoke_right",
    "run_batch",
    "stored_storyboard",
    "update_batch_budget",
]

# --- tuning -------------------------------------------------------------------------------------------------------

CLIP_CONCURRENCY = 2  # parallel clip generations per batch
MAX_CLIP_ATTEMPTS = 3  # first try + 2 retries for retryable provider errors
RETRY_BACKOFF_SEC: tuple[float, ...] = (5.0, 20.0)  # wait before attempt 2, 3
CANCEL_POLL_SEC = 1.0  # DB polls for cancellation are throttled to this interval
PROGRESS_WRITE_SEC = 1.0  # min interval between video.progress writes
DEFAULT_SEGMENT_SEC = 15.0
CLIP_TIMEOUT_SEC = 900.0
CLIP_HEARTBEAT_SEC = 30.0  # a generating clip's updated_at is refreshed this often (liveness for other tasks)
STALE_CLIP_SEC = 600.0  # a generating clip without heartbeat for this long was abandoned by a dead task
WAIT_POLL_SEC = 2.0  # poll interval while waiting for clips another task generates
STALE_TASK_SEC = 600.0  # Celery recovery sweep: work idle this long is considered orphaned
STALE_WORK_SEC = 2 * 3600.0  # scratch dirs older than this belong to a dead process (renders time out at 30 min)
WORK_SUBDIRS = ("analysis", "clips", "renders", "whisper")
# Rough Claude storyboard size, used only for the estimate before the real call.
LLM_ESTIMATE_INPUT_TOKENS = 6000
LLM_ESTIMATE_OUTPUT_TOKENS_PER_SCENE = 700
LLM_ESTIMATE_OUTPUT_TOKENS_BASE = 1500
ANTHROPIC_PROVIDER = "anthropic"
OPENROUTER_PROVIDER = "openrouter"  # LLM cost records of the OpenRouter Claude route (and its video provider id)
RIGHTS_UNCONFIRMED_MESSAGE = "権利確認チェックリストのすべての項目を確認してから生成してください。"
BUDGET_REFUSED_MESSAGE = "予算上限に達したため生成しませんでした。"
RESUME_FAILED_NOTE = "。中断前に作成した生成ジョブを再開できなかったため、推定額を課金済みとして計上しました。"

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


# An edited line (not identical text) keeps its id when it is at least this similar to the old line.
LINE_MATCH_MIN_RATIO = 0.6
# Larger edited blocks are paired by position instead of the O(m·n) similarity alignment.
_MAX_LINE_PAIRING_CELLS = 10_000
_LINE_ID_RE = re.compile(r"^l(\d+)$")


def _line_key(text: Any) -> str:
    """Line text for alignment: NFKC, case-folded, whitespace removed."""
    return "".join(unicodedata.normalize("NFKC", str(text or "")).casefold().split())


def _line_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() if a or b else 1.0


def _pair_edited_lines(old_keys: Sequence[str], new_keys: Sequence[str]) -> list[tuple[int, int]]:
    """Order-preserving pairs (old index, new index) of edited lines, maximising the summed similarity."""
    m, n = len(old_keys), len(new_keys)
    if m * n > _MAX_LINE_PAIRING_CELLS:
        return [
            (i, i) for i in range(min(m, n)) if _line_similarity(old_keys[i], new_keys[i]) >= LINE_MATCH_MIN_RATIO
        ]
    sim = [[_line_similarity(a, b) for b in new_keys] for a in old_keys]
    best = [[0.0] * (n + 1) for _ in range(m + 1)]
    for i in range(m - 1, -1, -1):
        for j in range(n - 1, -1, -1):
            value = max(best[i + 1][j], best[i][j + 1])
            if sim[i][j] >= LINE_MATCH_MIN_RATIO:
                value = max(value, sim[i][j] + best[i + 1][j + 1])
            best[i][j] = value
    pairs: list[tuple[int, int]] = []
    i = j = 0
    while i < m and j < n:
        if sim[i][j] >= LINE_MATCH_MIN_RATIO and best[i][j] == sim[i][j] + best[i + 1][j + 1]:
            pairs.append((i, j))
            i, j = i + 1, j + 1
        elif best[i][j] == best[i + 1][j]:
            i += 1
        else:
            j += 1
    return pairs


def carry_over_lyric_lines(
    old_lines: Sequence[Mapping[str, Any]] | None,
    new_lines: Sequence[Mapping[str, Any]],
    *,
    reserved_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Freshly parsed lyric lines with the ids (and timing) of the lines they replace.

    Re-saving the lyrics must not renumber lines: per-video ``lyric_offsets``
    are keyed by line id. Old and new lines are aligned on their normalised
    text (``difflib`` over the line sequence, so inserted/removed lines and
    repeated choruses do not shift the others); edited lines in a changed
    block are paired by similarity (≥ :data:`LINE_MATCH_MIN_RATIO`).

    - A matched line keeps its id. When its text is identical and the new text
      carries no timing (TXT), it also keeps its timing, words, source and confidence;
      timed input (LRC/SRT) is authoritative.
    - Other lines get fresh ids ``lNNN`` numbered after the highest id of the old
      lines and ``reserved_ids`` (ids still referenced, e.g. by videos' overrides
      of deleted lines), so a new line never inherits another line's overrides.
    """
    old = [dict(line) for line in old_lines or []]
    new = [copy.deepcopy(dict(line)) for line in new_lines]
    old_keys = [_line_key(line.get("text")) for line in old]
    new_keys = [_line_key(line.get("text")) for line in new]
    match: dict[int, int] = {}  # new index -> old index
    matcher = difflib.SequenceMatcher(None, old_keys, new_keys, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            match.update({j1 + k: i1 + k for k in range(i2 - i1)})
        elif tag == "replace":
            match.update({j1 + j: i1 + i for i, j in _pair_edited_lines(old_keys[i1:i2], new_keys[j1:j2])})
    used = {str(line.get("id")) for line in old if line.get("id") is not None} | {str(i) for i in reserved_ids}
    numbers = [int(found.group(1)) for found in map(_LINE_ID_RE.match, used) if found]
    next_number = max(numbers, default=0) + 1
    for j, line in enumerate(new):
        i = match.get(j)
        if i is not None and old[i].get("id") is not None:
            previous = old[i]
            line["id"] = str(previous["id"])
            same_text = line.get("text") == previous.get("text")
            if same_text and line.get("start") is None and previous.get("start") is not None:
                for field in ("start", "end", "words", "source", "confidence"):
                    line[field] = copy.deepcopy(previous.get(field))
            continue
        while f"l{next_number:03d}" in used:
            next_number += 1
        line["id"] = f"l{next_number:03d}"
        used.add(line["id"])
    return new


def lyric_ids_in_use(db: Session, project_id: str) -> set[str]:
    """Line ids the project's videos still reference (``lyric_offsets``, also of the fallback render's plan)."""
    ids: set[str] = set()
    for plan in db.scalars(select(Video.plan).where(Video.project_id == project_id)):
        plans = [plan or {}, ((plan or {}).get("previous") or {}).get("plan") or {}]
        for item in plans:
            offsets = item.get("lyric_offsets") if isinstance(item, Mapping) else None
            if isinstance(offsets, Mapping):
                ids.update(str(key) for key in offsets)
    return ids


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
    grade = data.get("color_grade")
    if grade not in (None, "", "auto") and grade not in COLOR_GRADES:
        raise ServiceError(f"不明なカラーグレードです: {grade}", status_code=422, code="color_grade_unknown")
    provider = get_provider(str(data.get("provider") or "mock"))
    try:
        data["model"] = provider.get_model(data.get("model")).id
    except ProviderError as exc:
        raise ServiceError(exc.message, status_code=422, code="provider_unknown_model") from exc
    return data, provider


def existing_clip_refs(db: Session, project_id: str, *, include_in_flight: bool = False) -> list[dict[str, Any]]:
    """Ready clips of a project in the planner's ``existing_clips`` shape.

    With ``include_in_flight`` the project's ``pending``/``generating`` clips
    (with a cache key) follow, flagged ``in_flight`` and presented to the
    planner as ready with their requested duration: a batch planned while
    another one still generates the same clip reuses that clip (and waits for
    it) instead of generating and paying for it twice.
    """
    clips = db.scalars(
        select(Clip)
        .where(Clip.project_id == project_id, Clip.status == ClipStatus.READY, Clip.asset_id.is_not(None))
        .order_by(Clip.created_at, Clip.id)
    ).all()
    refs = [
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
    if include_in_flight:
        in_flight = db.scalars(
            select(Clip)
            .where(
                Clip.project_id == project_id,
                Clip.status.in_([ClipStatus.PENDING, ClipStatus.GENERATING]),
                Clip.cache_key.is_not(None),
            )
            .order_by(Clip.created_at, Clip.id)
        ).all()
        owners = {b for b in ((c.params or {}).get("batch_id") for c in in_flight) if b}
        running = (
            set(db.scalars(select(Batch.id).where(Batch.id.in_(owners), Batch.status.in_(list(ACTIVE_BATCH_STATUSES)))))
            if owners
            else set()
        )
        for c in in_flight:
            if (c.params or {}).get("batch_id") not in running:
                continue  # nobody will generate it
            refs.append(
                {
                    "id": c.id,
                    "scene_id": c.scene_id,
                    "cache_key": c.cache_key,
                    "duration": _num((c.params or {}).get("duration")) or cost_service.TARGET_CLIP_SEC,
                    "source": c.source,
                    "status": ClipStatus.READY,  # usable for planning; the batch waits for it
                    "in_flight": True,
                }
            )
    return refs


def storyboard_world_key(project: Project, settings: Mapping[str, Any]) -> str:
    """Identity of what a storyboard is written from: world (preset, custom world) + song + lyrics."""
    payload = {
        "preset": str(settings.get("preset") or project.world_preset or "cinematic"),
        "custom_world": settings.get("custom_world") or project.custom_world or "",
        "title": project.title or "",
        "artist": project.artist_name or "",
        "mood": project.mood or "",
        "purpose": project.purpose or "",
        "audio": project.audio_asset_id or "",
        "lyrics": [str(line.get("text") or "") for line in project.lyric_lines or []],
    }
    if project.artist_aliases:  # only when set, so keys of projects without aliases stay unchanged
        payload["aliases"] = sorted(str(alias) for alias in project.artist_aliases)
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def stored_storyboard(db: Session, project: Project, settings: Mapping[str, Any]) -> dict[str, Any] | None:
    """The project's latest Claude storyboard for the same world, reused by later batches.

    Claude writes a different storyboard on every call; reusing it keeps the
    protagonist/world consistent across batches, lets later batches hit the
    clip cache and makes the estimate describe the real run.
    ``settings["new_storyboard"]`` asks for a fresh one.
    """
    if settings.get("provider") == "library" or settings.get("new_storyboard"):
        return None
    key = storyboard_world_key(project, settings)
    for storyboard in db.scalars(
        select(Batch.storyboard).where(Batch.project_id == project.id).order_by(Batch.created_at.desc(), Batch.id)
    ):
        if (
            isinstance(storyboard, Mapping)
            and storyboard.get("source") == "claude"
            and storyboard.get("world_key") == key
            and storyboard.get("scenes")
        ):
            return copy.deepcopy(dict(storyboard))
    return None


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
        artist_aliases=list(project.artist_aliases or []),
        end_user=project.owner_id,
    )


def llm_estimate_usd(settings: Mapping[str, Any], n_scenes: int = DEFAULT_SCENES) -> float:
    """Expected cost of the Claude storyboard call (0 without a Claude route or in library mode).

    Priced at the Anthropic list price of the route's model on both routes (OpenRouter's
    ``usage.cost`` replaces it once the call is made).
    """
    app_settings = get_settings()
    if not app_settings.llm_configured or settings.get("provider") == "library":
        return 0.0
    usage = {
        "input_tokens": LLM_ESTIMATE_INPUT_TOKENS,
        "output_tokens": LLM_ESTIMATE_OUTPUT_TOKENS_BASE + LLM_ESTIMATE_OUTPUT_TOKENS_PER_SCENE * max(1, n_scenes),
    }
    return claude_cost_usd(llm_configured_model(), usage)


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


def require_rights(project: Project) -> None:
    """422 ``rights_unconfirmed`` unless every §9 checklist item is confirmed.

    Checked for every generation and re-render (batch, regenerate, edit,
    retry) and again by the tasks, so withdrawing consent stops queued work.
    """
    if not all_rights_confirmed(project.rights):
        raise ServiceError(RIGHTS_UNCONFIRMED_MESSAGE, status_code=422, code="rights_unconfirmed")


AUDIO_RIGHTS_RESET_MESSAGE = (
    "音源が変わったため、権利確認の「音源の権利」を未確認に戻しました。"
    "新しい音源の権利を確認するまで、生成と再レンダリングはできません。"
)
LYRICS_RIGHTS_RESET_MESSAGE = (
    "歌詞の本文が変わったため、権利確認の「歌詞の権利」を未確認に戻しました。"
    "新しい歌詞の権利を確認するまで、生成と再レンダリングはできません。"
)


def revoke_right(project: Project, key: str) -> bool:
    """Un-confirm one checklist item (content it vouched for was replaced); True when it was confirmed.

    ``rights_confirmed_at`` is cleared with it, so every generation guard stops until the user confirms again.
    """
    rights = dict(project.rights or {})
    if not rights.get(key):
        return False
    rights[key] = False
    project.rights = rights
    project.rights_confirmed_at = None
    return True


def preview_batch(db: Session, project: Project, settings: GenerationSettings | Mapping[str, Any]) -> BatchPreview:
    """Plan a batch without side effects and price it (``POST /estimate`` and batch creation).

    The storyboard is the project's stored Claude storyboard for this world
    when one exists (the batch will reuse it), else the deterministic template
    (no paid Claude call); the expected Claude cost is then added as
    ``llm_usd`` when a key is set. Clips other batches are still generating
    count as cached (the batch will reuse them).
    """
    _require_analysis(project)
    data, provider = resolve_generation_settings(project, settings)
    stored = stored_storyboard(db, project, data)
    storyboard = stored or build_storyboard(project, data, use_claude=False)
    seed = int(data["seed"]) if data.get("seed") is not None else 0
    plan = _plan(project, data, storyboard, provider, existing_clip_refs(db, project.id, include_in_flight=True), seed)
    stats = plan["stats"]
    n_scenes = len((storyboard or {}).get("scenes") or []) or DEFAULT_SCENES
    limit = resolve_budget_limit(data, project)
    estimate = cost_service.estimate(
        data,
        stats,
        provider.id,
        str(data["model"]),
        cached_clips=0 if provider.kind == "library" else int(stats.get("reused_clips") or 0),
        llm_estimate_usd=0.0 if stored is not None else llm_estimate_usd(data, n_scenes),
        budget_limit_usd=limit,
    )
    reuse_note = ["前回 Claude で作成した絵コンテを再利用します（同じ世界観・新たな LLM 費用なし）。"] if stored else []
    estimate["notes"] = list(dict.fromkeys([*plan.get("warnings", []), *reuse_note, *estimate.get("notes", [])]))
    return BatchPreview(settings=data, storyboard=storyboard, plan=plan, estimate=estimate, budget_limit_usd=limit)


def create_batch(db: Session, project: Project, settings: GenerationSettings) -> Batch:
    """Validate the request (§5.6 guards), store a ``queued`` Batch and return it (not yet enqueued)."""
    _require_analysis(project)
    require_rights(project)
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
    """Clip rows this batch created at planning time (not cache hits)."""
    return [
        str(spec["clip_id"])
        for spec in (batch.plan or {}).get("clip_specs") or []
        if isinstance(spec, dict) and spec.get("clip_id") and not spec.get("existing_clip_id")
    ]


def _clip_batch_id(clip: Clip) -> str | None:
    batch_id = (clip.params or {}).get("batch_id")
    return str(batch_id) if batch_id else None


def _batch_clips(db: Session, batch_id: str, project_id: str, statuses: Iterable[str]) -> list[Clip]:
    """Clips created for ``batch_id`` (``params.batch_id``: planning, regenerate and retry) in ``statuses``.

    Filtered in Python (the project's clips in those states are few), which
    keeps the query portable between PostgreSQL JSONB and SQLite JSON.
    """
    rows = db.scalars(
        select(Clip).where(Clip.project_id == project_id, Clip.status.in_(list(statuses))).order_by(Clip.created_at)
    ).all()
    return [clip for clip in rows if _clip_batch_id(clip) == batch_id]


def _pending_budget_usd(db: Session, batch: Batch) -> float:
    """Estimated cost of the batch's planned clips that hold no budget reservation yet (``pending``)."""
    return sum(_clip_cost_estimate(c) for c in _batch_clips(db, batch.id, batch.project_id, [ClipStatus.PENDING]))


def _cancel_pending_clips(db: Session, batch_id: str, project_id: str, *, keep: Iterable[str] = ()) -> int:
    """``pending`` clips of the batch → ``cancelled`` (no commit). Clips in ``keep`` stay."""
    kept = set(keep)
    ids = [c.id for c in _batch_clips(db, batch_id, project_id, [ClipStatus.PENDING]) if c.id not in kept]
    if ids:
        db.execute(
            update(Clip)
            .where(Clip.id.in_(ids), Clip.status == ClipStatus.PENDING)
            .values(status=ClipStatus.CANCELLED, updated_at=utcnow())
            .execution_options(synchronize_session=False)
        )
    return len(ids)


def _release_orphan_pending_clips(db: Session, batch_id: str) -> None:
    """Cancel ``pending`` clips of a rendering/finished batch that no active video needs any more (no commit).

    While a batch is upstream its ``run_batch`` task owns the pending clips.
    """
    row = db.execute(select(Batch.status, Batch.project_id).where(Batch.id == batch_id)).one_or_none()
    if row is None or row[0] in _UPSTREAM:
        return
    needed: set[str] = set()
    for plan in db.scalars(
        select(Video.plan).where(Video.batch_id == batch_id, Video.status.in_(list(ACTIVE_VIDEO_STATUSES)))
    ):
        needed |= video_clip_ids(plan)
    _cancel_pending_clips(db, batch_id, row[1], keep=needed)


def _previous_render(video: Video) -> dict[str, Any] | None:
    """State a re-render of a finished video falls back to when it is cancelled or fails."""
    if video.status != VideoStatus.DONE or video.asset_id is None:
        return None
    plan = {key: value for key, value in dict(video.plan or {}).items() if key != "previous"}
    return {
        "plan": copy.deepcopy(plan),
        "render_version": int(video.render_version or 1),
        "quality": copy.deepcopy(video.quality),
    }


def _end_video(
    db: Session,
    video_id: str,
    status: str,
    error: str,
    *,
    allowed: Iterable[str],
    attempts: int | None = None,
    **values: Any,
) -> bool:
    """Move an active video to ``failed``/``cancelled`` (only from ``allowed``; with ``attempts``, only
    while that render attempt still owns it). Commits.

    A re-render of a finished video (``plan.previous`` and its MP4 still
    stored) returns to ``done`` with the previous plan instead, so the last
    good render stays playable and downloadable; ``error`` says why.
    """
    video = db.execute(
        select(Video).where(Video.id == video_id).execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if video is None:
        db.commit()
        return False
    previous = (video.plan or {}).get("previous")
    if isinstance(previous, Mapping) and isinstance(previous.get("plan"), Mapping) and video.asset_id is not None:
        reason = (
            "再レンダリングを中止しました。"
            if status == VideoStatus.CANCELLED
            else f"再レンダリングに失敗しました（{error.rstrip('。')}）。"
        )
        values = {
            "status": VideoStatus.DONE,
            "plan": copy.deepcopy(dict(previous["plan"])),
            "render_version": int(previous.get("render_version") or video.render_version or 1),
            "quality": previous.get("quality"),
            "progress": 1.0,
            "error": reason + "前回の動画を表示しています。",
        }
    else:
        values = {"status": status, "error": error, **values}
    stmt = update(Video).where(Video.id == video_id, Video.status.in_(list(allowed)))
    if attempts is not None:
        stmt = stmt.where(Video.attempts == attempts)
    result = db.execute(stmt.values(updated_at=utcnow(), **values).execution_options(synchronize_session=False))
    ended = isinstance(result, CursorResult) and result.rowcount > 0
    if ended:
        _release_orphan_pending_clips(db, video.batch_id)
    db.commit()
    return ended


def _settle_videos_and_clips(db: Session, batch_id: str, video_error: str) -> int:
    """Active videos → cancelled (or back to their previous render), pending clips → cancelled."""
    project_id = db.scalar(select(Batch.project_id).where(Batch.id == batch_id))
    if project_id is None:
        db.commit()
        return 0
    stopped = 0
    for video_id in db.scalars(
        select(Video.id).where(Video.batch_id == batch_id, Video.status.in_(list(ACTIVE_VIDEO_STATUSES)))
    ).all():
        stopped += _end_video(db, video_id, VideoStatus.CANCELLED, video_error, allowed=ACTIVE_VIDEO_STATUSES)
    _cancel_pending_clips(db, batch_id, project_id)
    db.commit()
    return stopped


def _stop_batch(db: Session, batch_id: str, status: str, error: str, *, video_error: str) -> bool:
    """Move an active batch to a terminal status; cancel its active videos and pending clips.

    Rendering videos are cancelled too (their task notices at its next
    progress check and its final ``done`` transition fails), and every
    ``pending`` clip created for the batch is cancelled, including clips a
    regenerate/retry planned outside ``plan.clip_specs``.
    """
    if not _transition_batch(db, batch_id, ACTIVE_BATCH_STATUSES, status, error=error, finished_at=utcnow()):
        return False
    _settle_videos_and_clips(db, batch_id, video_error)
    return True


def _cleanup_terminal_batch(db: Session, batch_id: str) -> int:
    """Settle leftovers of a batch that is already terminal (e.g. rows a planner committed after a cancel).

    Returns the number of videos stopped; does nothing for an active batch.
    """
    status = db.scalar(select(Batch.status).where(Batch.id == batch_id))
    if status is None or status in ACTIVE_BATCH_STATUSES:
        db.commit()
        return 0
    message = (
        "バッチがキャンセルされました。" if status == BatchStatus.CANCELLED else "バッチが終了したため中止しました。"
    )
    return _settle_videos_and_clips(db, batch_id, message)


def cancel_batch(db: Session, batch: Batch) -> None:
    """User cancel (§7): active videos → cancelled; running tasks stop at their next check.

    Cancelling the re-render of a finished batch (re-opened by retry / edit /
    regenerate) restores the finished renders and the batch's earlier status.
    """
    if batch.status == BatchStatus.CANCELLED:
        return
    if batch.status not in ACTIVE_BATCH_STATUSES:
        raise ServiceError(
            "このバッチはすでに終了しているためキャンセルできません。", status_code=409, code="batch_finished"
        )
    reopened_from = (batch.plan or {}).get("reopened_from")
    stopped = _stop_batch(
        db,
        batch.id,
        BatchStatus.CANCELLED,
        "ユーザーによってキャンセルされました。",
        video_error="バッチがキャンセルされました。",
    )
    if stopped and reopened_from:
        _restore_reopened_batch(db, batch.id, str(reopened_from))
    db.refresh(batch)


def _clear_reopened(batch: Batch) -> None:
    if batch.plan is not None and "reopened_from" in batch.plan:
        batch.plan = {key: value for key, value in batch.plan.items() if key != "reopened_from"}


def _restore_reopened_batch(db: Session, batch_id: str, reopened_from: str) -> None:
    """After cancelling a re-opened batch: back to the status it had before the re-render."""
    batch = db.execute(
        select(Batch).where(Batch.id == batch_id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if batch is None or batch.status != BatchStatus.CANCELLED:
        db.commit()
        return
    previous_error = (batch.plan or {}).get("previous_error")
    if reopened_from in (BatchStatus.COMPLETED, BatchStatus.PARTIAL, BatchStatus.FAILED):
        # Its videos are terminal again (re-renders fell back to their previous render): recompute.
        batch.status = BatchStatus.RENDERING
        batch.finished_at = None
        batch.error = previous_error if reopened_from == BatchStatus.FAILED else None
        db.commit()
        recompute_batch_status(db, batch_id)
        return
    if reopened_from == BatchStatus.BUDGET_EXCEEDED:
        batch.status = BatchStatus.BUDGET_EXCEEDED
        batch.error = previous_error or batch.error
    _clear_reopened(batch)
    db.commit()


def update_batch_budget(db: Session, batch: Batch, limit_usd: float) -> None:
    """New budget cap of a batch (``estimate.budget_limit_usd``), used by every later clip reservation.

    Lets a ``budget_exceeded`` batch be resumed: its cancelled videos can then be retried.
    """
    estimate = dict(batch.estimate or {})
    estimate["budget_limit_usd"] = round(float(limit_usd), 4)
    total = _num(estimate.get("total_usd"))
    if total is not None:
        estimate["within_budget"] = total <= float(limit_usd) + 1e-9
    batch.estimate = estimate
    db.commit()


def reopen_batch(db: Session, batch: Batch) -> None:
    """A finished batch goes back to ``rendering`` while one of its videos is re-rendered.

    The status it came from and its terminal reason are kept in
    ``plan.reopened_from`` / ``plan.previous_error`` (a budget or cancel
    reason is not lost; cancelling the re-render restores them).
    """
    if batch.status in _TERMINAL_REOPENABLE:
        if batch.plan is not None:
            plan = dict(batch.plan)
            plan["reopened_from"] = batch.status
            if batch.error:
                plan["previous_error"] = batch.error
            batch.plan = plan
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
            _clear_reopened(batch)
            if new_status == BatchStatus.FAILED and not batch.error:
                batch.error = "すべての動画の生成に失敗しました。各動画のエラーを確認してください。"
            elif new_status != BatchStatus.FAILED:
                batch.error = None
    db.commit()
    return new_status


# --- budget & cancellation guards ---------------------------------------------------------------------------------


def _batch_budget_limit(db: Session, batch_id: str) -> float | None:
    estimate = db.scalar(select(Batch.estimate).where(Batch.id == batch_id))
    return _num((estimate or {}).get("budget_limit_usd"))


def _reserve_budget(db: Session, batch_id: str | None, amount: float) -> bool:
    """Atomically add ``amount`` to the batch's spend unless that exceeds its budget (no commit).

    Reservations live in ``batches.cost_usd`` itself, so every task and
    process sees the same headroom (two renders can no longer both spend it).
    Each attempt settles its reservation to the real (or presumed billed) cost.
    """
    if not batch_id or amount <= 0:
        return True
    limit = _batch_budget_limit(db, batch_id)
    stmt = update(Batch).where(Batch.id == batch_id)
    if limit is not None:
        stmt = stmt.where(Batch.cost_usd + float(amount) <= limit + 1e-9)
    result = db.execute(
        stmt.values(cost_usd=Batch.cost_usd + float(amount)).execution_options(synchronize_session=False)
    )
    return isinstance(result, CursorResult) and result.rowcount > 0


class CancelWatcher:
    """``cancel_check`` for providers/renders: should the work stop? Throttled DB reads.

    True when the batch (or video) is gone or cancelled, or — with
    ``batch_alive`` / ``video_alive`` — no longer in one of those statuses
    (e.g. ``run_batch`` stops when its batch leaves ``generating_clips``, even
    if a retry has already re-opened it).
    """

    def __init__(
        self,
        batch_id: str | None,
        video_id: str | None = None,
        *,
        batch_alive: Iterable[str] | None = None,
        video_alive: Iterable[str] | None = None,
    ):
        self.batch_id = batch_id
        self.video_id = video_id
        self.batch_alive = frozenset(batch_alive) if batch_alive is not None else None
        self.video_alive = frozenset(video_alive) if video_alive is not None else None
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
                if self.batch_alive is not None and status not in self.batch_alive:
                    return True
            if self.video_id is not None:
                status = db.scalar(select(Video.status).where(Video.id == self.video_id))
                if status is None or status == VideoStatus.CANCELLED:
                    return True
                if self.video_alive is not None and status not in self.video_alive:
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
    """LLM CostRecord of a storyboard's Claude call (``None`` when it cost nothing).

    ``provider`` is the route the call took: ``"anthropic"`` (direct) or ``"openrouter"``
    (amount = OpenRouter's reported ``usage.cost`` when present).
    """
    amount = float(storyboard.get("cost_usd") or 0.0)
    if amount <= 0:
        return None
    usage = storyboard.get("usage") or {}
    tokens = sum(int(v or 0) for v in usage.values()) if isinstance(usage, Mapping) else 0
    via_openrouter = storyboard.get("route") == OPENROUTER_PROVIDER
    return CostRecord(
        project_id=project_id,
        batch_id=batch_id,
        kind=CostKind.LLM,
        provider=OPENROUTER_PROVIDER if via_openrouter else ANTHROPIC_PROVIDER,
        model=storyboard.get("model"),
        units=float(tokens),
        unit=CostUnit.TOKEN,
        unit_price_usd=round(amount / tokens, 10) if tokens else 0.0,
        amount_usd=round(amount, 6),
        cached=False,
        note=llm_cost_label(storyboard, "絵コンテ生成")
        + ("" if storyboard.get("source") == "claude" else "・テンプレートにフォールバック"),
    )


def llm_cost_label(storyboard: Mapping[str, Any], what: str) -> str:
    """``絵コンテ生成（Claude）`` / ``…（Claude・OpenRouter 経由）`` for cost-record notes."""
    via = "・OpenRouter 経由" if storyboard.get("route") == OPENROUTER_PROVIDER else ""
    return f"{what}（Claude{via}）"


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


def _plan_batch_rows(db: Session, batch: Batch, token: datetime) -> bool:
    """Planning step: storyboard → plan → Clip/Video rows, committed in one transaction.

    The rows and ``batch.plan`` are only committed while the batch is still
    ``planning`` and this task still owns the planning (``started_at ==
    token``): a batch cancelled during the (possibly long) Claude call gets no
    rows. Returns False in that case. A Claude call that was made is booked
    either way (it is billed).
    """
    project = batch.project
    data, provider = resolve_generation_settings(project, dict(batch.settings or {}))
    stored = stored_storyboard(db, project, data)
    storyboard = stored or build_storyboard(project, data, use_claude=get_settings().llm_configured)
    if storyboard is not None and stored is None:
        storyboard["world_key"] = storyboard_world_key(project, data)
    llm_record = llm_cost_record(project.id, batch.id, storyboard) if storyboard and stored is None else None
    spent = llm_record.amount_usd if llm_record is not None else 0.0
    seed = int(data["seed"]) if data.get("seed") is not None else stable_seed("batch", batch.id)
    plan = _plan(project, data, storyboard, provider, existing_clip_refs(db, project.id, include_in_flight=True), seed)
    plan["seed"] = seed
    if stored is not None:
        plan["storyboard_reused"] = True
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
    if llm_record is not None:
        db.add(llm_record)
    result = db.execute(
        update(Batch)
        .where(Batch.id == batch.id, Batch.status == BatchStatus.PLANNING, Batch.started_at == token)
        .values(settings=data, storyboard=storyboard, plan=plan, cost_usd=Batch.cost_usd + spent)
        .execution_options(synchronize_session=False)
    )
    if not (isinstance(result, CursorResult) and result.rowcount > 0):
        db.rollback()  # cancelled (or planned by another task) meanwhile: no rows
        if llm_record is not None:
            db.add(llm_cost_record(project.id, batch.id, storyboard))
            _add_batch_cost(db, batch.id, spent)
            db.commit()
        return False
    db.commit()
    return True


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


# --- clip generation ----------------------------------------------------------------------------------------------


@dataclass
class ClipRun:
    """Outcome of :func:`_generate_clip`: final status, and whether the batch budget refused it."""

    status: str
    budget_refused: bool = False


def _remote_cancel_supported(provider: VideoProvider) -> bool:
    """Can an abandoned job of this provider be cancelled remotely (Runway) or does it keep running (Veo, Kling)?"""
    flag = getattr(provider, "supports_remote_cancel", None)
    if isinstance(flag, bool):
        return flag
    implementation = getattr(type(provider), "_cancel_remote", None)
    return implementation is not None and implementation is not getattr(ApiVideoProvider, "_cancel_remote", None)


def _accepts_keyword(fn: Callable[..., Any], name: str) -> bool:
    try:
        parameter = inspect.signature(fn).parameters.get(name)
    except (TypeError, ValueError):
        return False
    return parameter is not None and parameter.kind in (
        inspect.Parameter.KEYWORD_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )


# Codes of a remote job that *ended* with an error (Runway FAILED, Veo ``error``, Kling ``failed``, safety
# filters): the provider reports what it charged (``cost_usd``) or nothing is presumed billed.
_ENDED_WITH_ERROR = frozenset({"provider_failed", "provider_content_filtered"})


def _billed_usd(provider: VideoProvider, exc: BaseException, estimate: float, *, resumed: bool = False) -> float:
    """What a failed attempt is known or presumed to have cost (0 when nothing was billed).

    * Known: the provider reports it — ``exc.cost_usd`` is authoritative even
      when it is 0 (e.g. Runway's ``cost.credits`` of a FAILED task, or the
      price of a finished job whose video could not be fetched) — or
      ``exc.billed`` is set (then the estimate is booked), e.g. a submission
      whose outcome is unknown (``ProviderSubmitAmbiguous``: a job may exist,
      so it is not sent again).
    * Not billed: nothing was submitted, or the remote job ended with an
      error of its own (:data:`_ENDED_WITH_ERROR`), so it may be retried.
    * Presumed billed (table price): the job finished but its output was
      unusable (``provider_bad_output``), or it was **abandoned** while it may
      still run — a timeout, lost polling or a cancel — on a provider without a
      remote cancel (Veo, Kling). Runway's abandoned tasks are DELETEd.
    * ``resumed``: the attempt picked up a job submitted before a restart. If it
      failed before the job could be followed (no :class:`ProviderTaskError`
      for the job: e.g. credentials gone, a bad token), the job still runs and
      is billed — presumed billed at ``estimate`` (the submit-time estimate).
    """
    if getattr(provider, "kind", "") != "api":
        return 0.0
    cost = _num(getattr(exc, "cost_usd", None))
    if cost is not None:
        return max(0.0, cost)
    if getattr(exc, "billed", False):
        return estimate
    if not isinstance(exc, ProviderTaskError) or not exc.external_job_id:
        return estimate if resumed else 0.0
    if exc.code == "provider_bad_output":
        return estimate
    if exc.code in _ENDED_WITH_ERROR:
        return 0.0
    if isinstance(exc, ProviderCancelled) or exc.retryable:
        cancelled_remotely = _remote_cancel_supported(provider) and exc.code in (
            "provider_cancelled",
            "provider_timeout",
        )
        return 0.0 if cancelled_remotely else estimate
    return 0.0


class _ClipHeartbeat:
    """``cancel_check`` wrapper that also refreshes the clip's ``updated_at``.

    Other tasks waiting for this clip (and the recovery sweep) treat a
    ``generating`` clip without a heartbeat for :data:`STALE_CLIP_SEC` as
    abandoned by a dead task.
    """

    def __init__(self, clip_id: str, cancel_check: Callable[[], bool] | None):
        self.clip_id = clip_id
        self.cancel_check = cancel_check
        self._last = time.monotonic()

    def __call__(self) -> bool:
        now = time.monotonic()
        if now - self._last >= CLIP_HEARTBEAT_SEC:
            self._last = now
            try:
                with SessionLocal() as db:
                    db.execute(
                        update(Clip)
                        .where(Clip.id == self.clip_id, Clip.status == ClipStatus.GENERATING)
                        .values(updated_at=utcnow())
                        .execution_options(synchronize_session=False)
                    )
                    db.commit()
            except Exception:
                logger.warning("clip %s: heartbeat failed", self.clip_id, exc_info=True)
        return bool(self.cancel_check is not None and self.cancel_check())


def _claim_clip(db: Session, clip_id: str) -> bool:
    """``pending`` → ``generating`` for this task only (two tasks never generate the same row)."""
    result = db.execute(
        update(Clip)
        .where(Clip.id == clip_id, Clip.status == ClipStatus.PENDING)
        .values(status=ClipStatus.GENERATING, error=None, updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return isinstance(result, CursorResult) and result.rowcount > 0


def _without(params: Mapping[str, Any] | None, *keys: str) -> dict[str, Any]:
    return {key: value for key, value in dict(params or {}).items() if key not in keys}


def _refuse_for_budget(db: Session, clip: Clip) -> ClipRun:
    clip.status = ClipStatus.CANCELLED
    clip.error = BUDGET_REFUSED_MESSAGE
    clip.params = {**_without(clip.params, "reserved_usd", "open_job", "submitted_usd"), "budget_refused": True}
    db.commit()
    return ClipRun(ClipStatus.CANCELLED, budget_refused=True)


def _record_submitted(db: Session, clip: Clip, job_id: Any, token: Any = None, *, estimate: float = 0.0) -> None:
    """``on_submitted(job_id, resume_token)`` hook: persist the remote job at once, so a restart can
    resume (or book) it. ``params.open_job`` holds the token :meth:`resume` takes; ``params.submitted_usd``
    the attempt's estimate, which prices the job after a restart even if the provider's catalogue no
    longer lists or prices its model (and is booked when the job cannot be resumed at all)."""
    try:
        clip.external_job_id = str(job_id)[:200]
        open_job = str(token if token else job_id)
        clip.params = {
            **dict(clip.params or {}),
            "open_job": open_job,
            "submitted_at": utcnow().isoformat(),
            "submitted_usd": round(float(estimate), 6),
        }
        db.commit()
    except Exception:
        logger.warning("clip %s: could not record submitted job %s", clip.id, job_id, exc_info=True)
        db.rollback()


def _book_failed_attempt(
    db: Session, clip: Clip, *, batch_id: str | None, reserved: float, billed: float, presumed: bool, units: float
) -> None:
    """Settle an attempt that did not produce a clip: release its reservation, book what it cost (no commit)."""
    _add_batch_cost(db, batch_id, billed - reserved)
    clip.params = _without(clip.params, "reserved_usd", "open_job", "submitted_usd")
    if billed <= 0:
        return
    clip.cost_usd = round(float(clip.cost_usd or 0.0) + billed, 6)
    db.add(
        CostRecord(
            project_id=clip.project_id,
            batch_id=batch_id,
            clip_id=clip.id,
            kind=CostKind.VIDEO_GENERATION,
            provider=clip.provider,
            model=clip.model,
            units=units,
            unit=CostUnit.SECOND,
            unit_price_usd=round(billed / units, 6) if units else 0.0,
            amount_usd=round(billed, 6),
            cached=False,
            note="課金済み・生成失敗" + ("（推定）" if presumed else ""),
        )
    )


def generate_clip(clip_id: str, *, cancel_check: Callable[[], bool] | None = None) -> str:
    """Generate one pending clip with retries (max :data:`MAX_CLIP_ATTEMPTS`); returns the final status.

    See :func:`_generate_clip`. A clip another task is generating is not
    touched (its current status is returned).
    """
    return _generate_clip(clip_id, cancel_check=cancel_check).status


def _generate_clip(clip_id: str, *, cancel_check: Callable[[], bool] | None = None, refuse: bool = False) -> ClipRun:
    """Claim and generate one ``pending`` clip.

    * The clip is claimed (``pending`` → ``generating``) first; another task's clip is left alone.
    * Before **every** attempt the estimated price is reserved atomically against the clip's
      batch budget (:func:`_reserve_budget`); a refusal → ``cancelled`` + ``params.budget_refused``.
      ``refuse=True`` refuses without trying (a sibling was already refused).
    * Retryable :class:`ProviderError`s are retried with backoff — unless the failed attempt is
      known or presumed billed (:func:`_billed_usd`), e.g. a Veo/Kling job that timed out keeps
      running remotely: resubmitting would pay twice, so the clip fails instead.
    * Every billed attempt gets a CostRecord and its cost in ``batch.cost_usd``.
    * Providers whose ``generate`` takes ``on_submitted`` report the job id right after submission
      (persisted as ``external_job_id`` / ``params.open_job``); a clip with an open job and a provider
      with ``resume(job_id, req, out_path, *, timeout, cancel_check)`` resumes polling it instead of
      submitting again. The job is priced with the estimate stored at submission (``params.submitted_usd``,
      passed on as ``resume(..., submitted_usd=)`` when accepted), whatever the provider's catalogue says
      now; when the job cannot be followed at all, that estimate is booked as presumed billed.
    * The rights checklist must still be confirmed; cancellation → ``cancelled``; anything else →
      ``failed`` with a Japanese error.
    """
    with SessionLocal() as db:
        clip = db.get(Clip, clip_id)
        if clip is None:
            return ClipRun(ClipStatus.FAILED)
        if clip.status != ClipStatus.PENDING:
            return ClipRun(clip.status, bool((clip.params or {}).get("budget_refused")))
        if not _claim_clip(db, clip_id):
            db.refresh(clip)
            return ClipRun(clip.status, bool((clip.params or {}).get("budget_refused")))
        db.refresh(clip)
        if refuse:
            return _refuse_for_budget(db, clip)
        if not all_rights_confirmed(clip.project.rights):
            clip.status = ClipStatus.FAILED
            clip.error = RIGHTS_UNCONFIRMED_MESSAGE
            db.commit()
            return ClipRun(clip.status)
        params = dict(clip.params or {})
        batch_id = params.get("batch_id")
        duration = float(params.get("duration") or cost_service.TARGET_CLIP_SEC)
        submitted_usd: float | None = None
        try:
            provider = get_provider(clip.provider)
            model_id = clip.model or provider.default_model()
            resume = getattr(provider, "resume", None)
            resume_job = params.get("open_job") if callable(resume) else None
            if resume_job:
                # The job exists and was priced when it was submitted: the provider's current catalogue
                # (which may have dropped or stopped pricing the model since) must not stop us following it.
                submitted_usd = _num(params.get("submitted_usd"))
            try:
                pricing = cost_service.get_pricing(provider.id, model_id)
            except ServiceError:
                if not resume_job:
                    raise
                pricing = None
        except ServiceError as exc:
            clip.status = ClipStatus.FAILED
            clip.error = exc.message or "動画素材の生成に失敗しました。"
            db.commit()
            return ClipRun(clip.status)
        if submitted_usd is not None:
            estimate = max(0.0, submitted_usd)
            pps = round(estimate / duration, 6) if duration > 0 else 0.0
        else:
            estimate = pricing.clip_cost_usd(duration) if pricing is not None else 0.0
            pps = pricing.price_per_second_usd if pricing is not None else 0.0
        request = ClipRequest(
            prompt=clip.prompt or "",
            negative_prompt=clip.negative_prompt or "",
            duration=duration,
            model=model_id,
            aspect_ratio=str(params.get("aspect_ratio") or "9:16"),
            seed=params.get("seed"),
            scene=params.get("hints"),
        )
        cancel = _ClipHeartbeat(clip_id, cancel_check)
        report_submit = _accepts_keyword(provider.generate, "on_submitted")
        resume_extra: dict[str, Any] = {}
        if resume_job and submitted_usd is not None and _accepts_keyword(resume, "submitted_usd"):
            resume_extra["submitted_usd"] = submitted_usd  # prices the job if the catalogue no longer can

        for attempt in range(1, MAX_CLIP_ATTEMPTS + 1):
            if cancel():
                clip.status = ClipStatus.CANCELLED
                db.commit()
                return ClipRun(clip.status)
            if not _reserve_budget(db, batch_id, estimate):
                return _refuse_for_budget(db, clip)
            clip.attempts = int(clip.attempts or 0) + 1
            clip.error = None
            clip.params = {**dict(clip.params or {}), "reserved_usd": estimate}
            db.commit()  # reservation + bookkeeping in one transaction
            out_dir = _work_dir("clips", clip.id)
            out_path = str(out_dir / "clip.mp4")
            result = None
            resuming = bool(resume_job)
            try:
                if resume_job:
                    logger.info("clip %s: resuming remote job %s", clip.id, resume_job)
                    result = provider.resume(  # type: ignore[attr-defined]
                        resume_job, request, out_path, timeout=CLIP_TIMEOUT_SEC, cancel_check=cancel, **resume_extra
                    )
                else:
                    extra: dict[str, Any] = {}
                    if report_submit:
                        extra["on_submitted"] = lambda job_id, token=None, *_args, **_kw: _record_submitted(
                            db, clip, job_id, token, estimate=estimate
                        )
                    result = provider.generate(
                        request, out_path, timeout=CLIP_TIMEOUT_SEC, cancel_check=cancel, **extra
                    )
                _store_clip_result(db, clip, result, out_dir)
                amount = float(result.cost_usd or 0.0)
                units = float((result.raw or {}).get("requested_duration") or result.duration)
                clip.status = ClipStatus.READY
                clip.duration = float(result.duration)
                clip.width = int(result.width)
                clip.height = int(result.height)
                clip.cost_usd = round(float(clip.cost_usd or 0.0) + amount, 6)
                clip.external_job_id = result.external_job_id
                clip.error = None
                clip.params = _without(clip.params, "reserved_usd", "open_job", "submitted_usd", "budget_refused")
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
                        # e.g. OpenRouter: whether the amount is its reported usage.cost or an estimate
                        note=(result.raw or {}).get("cost_note"),
                    )
                )
                _add_batch_cost(db, batch_id, amount - estimate)
                db.commit()
                return ClipRun(clip.status)
            except ProviderCancelled as exc:
                db.rollback()
                billed = _billed_usd(provider, exc, estimate, resumed=resuming)
                known = _num(getattr(exc, "cost_usd", None)) is not None
                _book_failed_attempt(
                    db, clip, batch_id=batch_id, reserved=estimate, billed=billed, presumed=not known,
                    units=request.duration,
                )  # fmt: skip
                clip.status = ClipStatus.CANCELLED
                clip.error = "生成がキャンセルされました。"
                db.commit()
                return ClipRun(clip.status)
            except ProviderNotConfigured as exc:
                db.rollback()
                billed = _billed_usd(provider, exc, estimate, resumed=resuming)  # a resumed job runs anyway
                _book_failed_attempt(db, clip, batch_id=batch_id, reserved=estimate, billed=billed, presumed=True,
                                     units=request.duration)  # fmt: skip
                clip.status = ClipStatus.FAILED
                clip.error = exc.message.rstrip("。") + RESUME_FAILED_NOTE if billed > 0 else exc.message
                db.commit()
                return ClipRun(clip.status)
            except ProviderError as exc:
                db.rollback()
                if isinstance(exc, ProviderTaskError):
                    clip.external_job_id = exc.external_job_id or clip.external_job_id
                    clip.params = {**dict(clip.params or {}), "failure_code": exc.failure_code}
                billed = _billed_usd(provider, exc, estimate, resumed=resuming)
                known = _num(getattr(exc, "cost_usd", None)) is not None
                _book_failed_attempt(
                    db, clip, batch_id=batch_id, reserved=estimate, billed=billed, presumed=not known,
                    units=request.duration,
                )  # fmt: skip
                clip.error = exc.message or "動画素材の生成に失敗しました。"
                if resuming and billed > 0 and not known and not isinstance(exc, ProviderTaskError):
                    clip.error = clip.error.rstrip("。") + RESUME_FAILED_NOTE
                if exc.retryable and attempt < MAX_CLIP_ATTEMPTS and billed <= 0:
                    db.commit()  # stays ``generating`` (owned by this task) during the backoff
                    delay = RETRY_BACKOFF_SEC[min(attempt - 1, len(RETRY_BACKOFF_SEC) - 1)]
                    logger.warning("clip %s attempt %d failed (%s); retrying in %.0fs", clip.id, attempt, exc, delay)
                    if _wait_or_cancel(delay, cancel):
                        clip.status = ClipStatus.CANCELLED
                        db.commit()
                        return ClipRun(clip.status)
                    continue
                if exc.retryable and billed > 0:
                    clip.error = (
                        f"{clip.error.rstrip('。')}。生成ジョブが課金されている可能性があるため、"
                        "重複課金を避けて再送信しませんでした。"
                    )
                clip.status = ClipStatus.FAILED
                db.commit()
                return ClipRun(clip.status)
            except Exception:
                logger.exception("clip %s: unexpected failure", clip.id)
                db.rollback()
                # A paid result that could not be stored (storage/thumbnail error) is still billed.
                billed = float(result.cost_usd or 0.0) if result is not None and provider.kind == "api" else 0.0
                presumed = False
                if result is None and resuming and provider.kind == "api":
                    billed, presumed = estimate, True  # the resumed job runs (and is billed) anyway
                if result is not None and result.external_job_id:
                    clip.external_job_id = result.external_job_id
                _book_failed_attempt(db, clip, batch_id=batch_id, reserved=estimate, billed=billed, presumed=presumed,
                                     units=request.duration)  # fmt: skip
                clip.status = ClipStatus.FAILED
                clip.error = "動画素材の生成中に予期しないエラーが発生しました。"
                if presumed:
                    clip.error = clip.error.rstrip("。") + RESUME_FAILED_NOTE
                elif billed > 0:
                    clip.error = "生成した動画素材を保存できませんでした（生成は完了・課金済みです）。"
                db.commit()
                return ClipRun(clip.status)
            finally:
                resume_job = None  # an open job is resumed at most once
                shutil.rmtree(out_dir, ignore_errors=True)
        return ClipRun(clip.status)


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


def _generate_clips(
    clip_ids: Sequence[str], cancel: Callable[[], bool], *, stop_on_refusal: bool = True
) -> tuple[dict[str, str], set[str]]:
    """Generate clips with :data:`CLIP_CONCURRENCY` threads → (status per clip, clips refused for budget).

    Once one clip is refused for budget the clips not started yet are refused
    too (``stop_on_refusal``): the batch is out of budget.
    """
    if not clip_ids:
        return {}, set()
    results: dict[str, str] = {}
    refused: set[str] = set()
    lock = threading.Lock()
    stop = threading.Event()

    def run(clip_id: str) -> str:
        outcome = _generate_clip(clip_id, cancel_check=cancel, refuse=stop_on_refusal and stop.is_set())
        if outcome.budget_refused:
            with lock:
                refused.add(clip_id)
            stop.set()
        return outcome.status

    with ThreadPoolExecutor(max_workers=CLIP_CONCURRENCY, thread_name_prefix="mvf-clip") as pool:
        futures = {pool.submit(run, cid): cid for cid in clip_ids}
        for future, cid in futures.items():
            try:
                results[cid] = future.result()
            except Exception:
                logger.exception("clip %s: generation task crashed", cid)
                results[cid] = ClipStatus.FAILED
    return results, refused


def _wait_for_clips(clip_ids: Iterable[str], cancel: Callable[[], bool]) -> bool:
    """Block until none of ``clip_ids`` is being (or about to be) generated by another task.

    A clip is settled when it is ready/failed/cancelled/deleted, ``generating``
    without a heartbeat for :data:`STALE_CLIP_SEC` (its task died; recovery
    resets it) or ``pending`` while its batch no longer runs. Returns False
    when ``cancel`` fired first.
    """
    waiting = list(dict.fromkeys(str(i) for i in clip_ids if i))
    while waiting:
        cutoff = utcnow() - timedelta(seconds=STALE_CLIP_SEC)
        with SessionLocal() as db:
            clips = db.scalars(
                select(Clip).where(Clip.id.in_(waiting), Clip.status.in_([ClipStatus.PENDING, ClipStatus.GENERATING]))
            ).all()
            batch_ids = {b for b in (_clip_batch_id(c) for c in clips) if b}
            running = (
                set(
                    db.scalars(
                        select(Batch.id).where(Batch.id.in_(batch_ids), Batch.status.in_(list(ACTIVE_BATCH_STATUSES)))
                    )
                )
                if batch_ids
                else set()
            )
            waiting = [
                c.id
                for c in clips
                if (c.status == ClipStatus.GENERATING and c.updated_at is not None and c.updated_at > cutoff)
                or (c.status == ClipStatus.PENDING and _clip_batch_id(c) in running)
            ]
        if waiting and _wait_or_cancel(WAIT_POLL_SEC, cancel):
            return False
    return True


def _ready_clips(db: Session, clip_ids: Iterable[str]) -> dict[str, Clip]:
    ids = list(dict.fromkeys(i for i in clip_ids if i))
    if not ids:
        return {}
    clips = db.scalars(select(Clip).where(Clip.id.in_(ids))).all()
    return {c.id: c for c in clips if c.status == ClipStatus.READY and c.asset_id is not None}


def _substitute_allowed(clip: Clip, provider: str | None, model: str | None) -> bool:
    """Same footage family only: uploads for library batches, else the batch's own provider + model."""
    if provider is None:
        return True
    if provider == "library":
        return clip.source == ClipSource.UPLOAD
    return clip.source != ClipSource.UPLOAD and clip.provider == provider and (not model or clip.model == model)


def _own_clip_ids(db: Session, batch: Batch) -> list[str]:
    """The batch's own pool: its plan's clips (generated or cache hits) and clips created for it later."""
    ids = [
        str(spec["clip_id"])
        for spec in (batch.plan or {}).get("clip_specs") or []
        if isinstance(spec, dict) and spec.get("clip_id")
    ]
    ids += [c.id for c in _batch_clips(db, batch.id, batch.project_id, [ClipStatus.READY])]
    return list(dict.fromkeys(ids))


def _batch_footage(batch: Batch) -> tuple[str | None, str | None]:
    settings = batch.settings or {}
    provider = settings.get("provider")
    return (str(provider) if provider else None), (str(settings["model"]) if settings.get("model") else None)


def resolve_plan_clips(
    db: Session,
    plan: dict[str, Any],
    project_id: str,
    *,
    preferred: Sequence[str] = (),
    provider: str | None = None,
    model: str | None = None,
) -> tuple[bool, int]:
    """Make every shot point at a ready clip, substituting failed/missing ones.

    Candidates are ready clips of the same footage family (``provider`` /
    ``model``: never mock clips in an API batch, uploads only in a library
    batch, see :func:`_substitute_allowed`). The batch's own pool
    (``preferred``) always wins over other project clips; then avoiding the
    neighbouring shot's clip, the same storyboard scene, least used.
    ``src_in`` is clamped into the new clip. Returns ``(ok, substitutions)``;
    ``ok`` is False when a shot cannot be filled at all.
    """
    shots = plan.get("shots") or []
    if not shots:
        return False, 0
    referenced = [str(s.get("clip_id") or "") for s in shots]
    ready = _ready_clips(db, referenced)
    if all(cid in ready for cid in referenced):
        return True, 0
    own = list(dict.fromkeys(preferred))
    candidates = _ready_clips(db, own)
    for clip in db.scalars(
        select(Clip)
        .where(Clip.project_id == project_id, Clip.status == ClipStatus.READY, Clip.asset_id.is_not(None))
        .order_by(Clip.created_at, Clip.id)
    ):
        candidates.setdefault(clip.id, clip)
    candidates = {cid: clip for cid, clip in candidates.items() if _substitute_allowed(clip, provider, model)}
    if not candidates:
        return False, 0
    own_set = set(own)
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
                candidate not in own_set,
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
    """After clip generation: batch → ``rendering``, resolve shots, queue renderable videos."""
    with SessionLocal() as db:
        # The batch transition comes first: of two run_batch tasks (re-delivery) only one gets here.
        if not _transition_batch(db, batch_id, [BatchStatus.GENERATING_CLIPS], BatchStatus.RENDERING):
            return
        batch = db.get(Batch, batch_id)
        if batch is None:
            return
        db.refresh(batch)
        provider, model = _batch_footage(batch)
        own = _own_clip_ids(db, batch)
        videos = db.scalars(
            select(Video)
            .where(Video.batch_id == batch_id, Video.status == VideoStatus.WAITING_CLIPS)
            .order_by(Video.index)
        ).all()
        queued: list[str] = []
        for video in videos:
            plan = copy.deepcopy(dict(video.plan or {}))
            ok, substitutions = resolve_plan_clips(
                db, plan, batch.project_id, preferred=own, provider=provider, model=model
            )
            if substitutions:
                plan["substitutions"] = int(plan.get("substitutions") or 0) + substitutions
            if ok:
                if _transition_video(db, video.id, [VideoStatus.WAITING_CLIPS], VideoStatus.QUEUED, plan=plan):
                    queued.append(video.id)
            else:
                _end_video(
                    db,
                    video.id,
                    VideoStatus.FAILED,
                    "使用できる動画素材がないためレンダリングできませんでした（素材の生成に失敗しました）。",
                    allowed=[VideoStatus.WAITING_CLIPS],
                )
    for video_id in queued:
        try:
            worker.enqueue("render_video", video_id)
        except worker.QueueUnavailable as exc:
            with SessionLocal() as db:
                _end_video(db, video_id, VideoStatus.FAILED, exc.message, allowed=[VideoStatus.QUEUED])
    with SessionLocal() as db:
        recompute_batch_status(db, batch_id)


def run_batch(batch_id: str) -> None:
    """Batch task (§7): plan → generate clips → enqueue renders. Resumable and safe to run twice."""
    with SessionLocal() as db:
        batch = db.get(Batch, batch_id)
        if batch is None:
            logger.info("run_batch: batch %s no longer exists", batch_id)
            return
        if batch.status not in _UPSTREAM:
            logger.info("run_batch: batch %s is %s; nothing to do", batch_id, batch.status)
            _cleanup_terminal_batch(db, batch_id)
            return
        try:
            if batch.plan is None:
                token = utcnow()
                if not _transition_batch(
                    db, batch_id, [BatchStatus.QUEUED, BatchStatus.PLANNING], BatchStatus.PLANNING, started_at=token
                ):
                    _cleanup_terminal_batch(db, batch_id)
                    return
                db.refresh(batch)
                if not _plan_batch_rows(db, batch, token):
                    logger.info("run_batch: batch %s was cancelled (or re-planned) during planning", batch_id)
                    _cleanup_terminal_batch(db, batch_id)
                    return
            if not _transition_batch(
                db, batch_id, [BatchStatus.PLANNING, BatchStatus.GENERATING_CLIPS], BatchStatus.GENERATING_CLIPS
            ):
                _cleanup_terminal_batch(db, batch_id)
                return
            db.refresh(batch)
            require_rights(batch.project)
            limit = _num((batch.estimate or {}).get("budget_limit_usd"))
            own_ids = _batch_clip_ids(batch)
            own = {c.id: c for c in db.scalars(select(Clip).where(Clip.id.in_(own_ids))).all()}
            todo = [
                cid for cid in own_ids if cid in own and own[cid].status in (ClipStatus.PENDING, ClipStatus.GENERATING)
            ]
            spent = float(batch.cost_usd or 0.0)  # includes reservations of attempts in flight
            needed = sum(_clip_cost_estimate(own[cid]) for cid in todo if own[cid].status == ClipStatus.PENDING)
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
            # Clips of other batches this plan reuses while they are still being generated are awaited.
            plan_clip_ids = [
                str(spec["clip_id"])
                for spec in (batch.plan or {}).get("clip_specs") or []
                if isinstance(spec, dict) and spec.get("clip_id")
            ]
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

    # Leaving ``generating_clips`` (cancel, or a retry re-opening a cancelled batch) stops the generation.
    cancel = CancelWatcher(batch_id, batch_alive=[BatchStatus.GENERATING_CLIPS])
    started = time.monotonic()
    try:
        results, refused = _generate_clips(todo, cancel)
        settled = _wait_for_clips(plan_clip_ids, cancel) if not refused else True
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
    if refused:
        with SessionLocal() as db:
            spent = float(db.scalar(select(Batch.cost_usd).where(Batch.id == batch_id)) or 0.0)
            _stop_batch(
                db,
                batch_id,
                BatchStatus.BUDGET_EXCEEDED,
                f"予算上限 {_money(limit or 0.0)} に達したため、残りの素材の生成を中止しました"
                f"（使用済み {_money(spent)}）。",
                video_error="予算上限に達したため中止しました。",
            )
        return
    if not settled:
        return  # cancelled: whoever stopped the batch settled its videos and clips
    _start_rendering(batch_id)


# --- task: render -------------------------------------------------------------------------------------------------


class _ClaimLost(Exception):
    """Another task renders this video (a duplicate or stale task exits without side effects)."""


@dataclass
class _RenderAttempt:
    attempts: int | None = None  # ``Video.attempts`` this task claimed the render with


class _RenderProgress:
    """``progress_cb`` for the renderer: throttled progress writes + cancellation.

    The render is cancelled when the video left ``rendering``, another
    attempt took it over, or the batch was cancelled.
    """

    def __init__(self, video_id: str, batch_id: str, attempts: int | None = None):
        self.video_id = video_id
        self.batch_id = batch_id
        self.attempts = attempts
        self._last = 0.0

    def __call__(self, fraction: float, stage: str) -> None:
        now = time.monotonic()
        if now - self._last < PROGRESS_WRITE_SEC and fraction < 1.0:
            return
        self._last = now
        with SessionLocal() as db:
            row = db.execute(
                select(Video.status, Video.attempts, Batch.status)
                .join(Batch, Batch.id == Video.batch_id)
                .where(Video.id == self.video_id)
            ).one_or_none()
            if (
                row is None
                or row[0] != VideoStatus.RENDERING
                or (self.attempts is not None and row[1] != self.attempts)
                or row[2] == BatchStatus.CANCELLED
            ):
                raise JobCancelled(self.video_id)
            stmt = update(Video).where(Video.id == self.video_id, Video.status == VideoStatus.RENDERING)
            if self.attempts is not None:
                stmt = stmt.where(Video.attempts == self.attempts)
            db.execute(
                stmt.values(progress=round(0.02 + 0.93 * max(0.0, min(1.0, fraction)), 3)).execution_options(
                    synchronize_session=False
                )
            )
            db.commit()


def _ensure_video_clips(video_id: str, batch_id: str) -> None:
    """Clips the video's plan needs that are not ready yet: this batch's pending ones are generated
    (within the batch budget), clips other tasks generate are awaited.

    Raises :class:`JobCancelled` when the video/batch was cancelled meanwhile
    and :class:`BudgetExceeded` when the budget refused a clip of the plan (the
    video fails instead of silently getting other footage).
    """
    with SessionLocal() as db:
        plan = db.scalar(select(Video.plan).where(Video.id == video_id)) or {}
        ids = sorted(video_clip_ids(plan))
        clips = db.scalars(select(Clip).where(Clip.id.in_(ids))).all() if ids else []
        own_pending = [c.id for c in clips if c.status == ClipStatus.PENDING and _clip_batch_id(c) == batch_id]
        busy = [c.id for c in clips if c.status in (ClipStatus.PENDING, ClipStatus.GENERATING)]
    if busy:
        cancel = CancelWatcher(
            batch_id, video_id, batch_alive=[BatchStatus.RENDERING], video_alive=ACTIVE_VIDEO_STATUSES
        )
        _generate_clips(own_pending, cancel)
        if not _wait_for_clips(busy, cancel):
            raise JobCancelled(video_id)
    with SessionLocal() as db:
        refused = [
            c
            for c in (db.scalars(select(Clip).where(Clip.id.in_(ids))).all() if ids else [])
            if c.status == ClipStatus.CANCELLED
            and (c.params or {}).get("budget_refused")
            and _clip_batch_id(c) == batch_id  # another batch's refusal: its clip is just substituted
        ]
        if refused:
            limit = _batch_budget_limit(db, batch_id)
            spent = float(db.scalar(select(Batch.cost_usd).where(Batch.id == batch_id)) or 0.0)
            raise BudgetExceeded(
                f"予算上限 {_money(limit or 0.0)} に達したため、この動画の新しい素材を生成できませんでした"
                f"（使用済み {_money(spent)}）。予算を見直すか、既存の素材で再生成してください。",
                estimate_usd=spent,
                limit_usd=limit,
            )


def _fail_video(video_id: str, message: str, *, attempts: int | None = None, **values: Any) -> None:
    """``failed`` (or back to the previous render). Without ``attempts``: only before a render was claimed."""
    with SessionLocal() as db:
        if attempts is None:
            _end_video(db, video_id, VideoStatus.FAILED, message,
                       allowed=[VideoStatus.QUEUED, VideoStatus.WAITING_CLIPS], **values)  # fmt: skip
        else:
            _end_video(db, video_id, VideoStatus.FAILED, message,
                       allowed=[VideoStatus.RENDERING], attempts=attempts, **values)  # fmt: skip


def _cancel_video(video_id: str, *, attempts: int) -> None:
    """Cancel the render attempt ``attempts`` (a newer attempt or a re-queued video is left alone)."""
    with SessionLocal() as db:
        _end_video(db, video_id, VideoStatus.CANCELLED, "バッチがキャンセルされました。",
                   allowed=[VideoStatus.RENDERING], attempts=attempts)  # fmt: skip


def _quality_error(report: Mapping[str, Any]) -> str:
    failed = [c for c in report.get("checks") or [] if not c.get("passed")]
    detail = ", ".join(f"{c.get('name')}={c.get('value')}（期待値 {c.get('expected')}）" for c in failed)
    return (
        f"出力ファイルの品質チェックに失敗しました: {detail}"
        if detail
        else "出力ファイルの品質チェックに失敗しました。"
    )


def render_video(video_id: str) -> None:
    """Render task (§7): clips → claim → FFmpeg → validate → upload → ``done``; recomputes the batch status.

    The render is claimed with a compare-and-swap (``queued``/``waiting_clips``
    → ``rendering``, ``attempts + 1``); a duplicate or stale task that loses
    the claim exits without side effects, and every later transition is
    conditioned on the claimed attempt.
    """
    with SessionLocal() as db:
        video = db.get(Video, video_id)
        if video is None:
            logger.info("render_video: video %s no longer exists", video_id)
            return
        batch_id = video.batch_id
        if video.batch.status not in ACTIVE_BATCH_STATUSES:
            # Cancelled/finished meanwhile: the batch's videos were settled then; mop up leftovers.
            _cleanup_terminal_batch(db, batch_id)
            return
        if video.status not in (VideoStatus.QUEUED, VideoStatus.WAITING_CLIPS):
            logger.info("render_video: video %s is %s; nothing to do", video_id, video.status)
            return

    work = _work_dir("renders", video_id)
    attempt = _RenderAttempt()
    try:
        _render_once(video_id, batch_id, work, attempt)
    except _ClaimLost:
        logger.info("render_video: video %s is rendered by another task", video_id)
    except JobCancelled:
        if attempt.attempts is not None:
            _cancel_video(video_id, attempts=attempt.attempts)
        # Before the claim, whoever cancelled the batch/video already settled it.
    except RenderError as exc:
        logger.error("render_video %s failed: %s\n%s", video_id, exc.message, exc.stderr_tail)
        _fail_video(video_id, exc.message or "動画のレンダリングに失敗しました。", attempts=attempt.attempts)
    except ServiceError as exc:
        logger.warning("render_video %s failed: %s", video_id, exc)
        _fail_video(video_id, exc.message or "動画のレンダリングに失敗しました。", attempts=attempt.attempts)
    except StorageNotFound as exc:
        logger.warning("render_video %s: missing input %s", video_id, exc)
        _fail_video(video_id, "入力ファイル（音源または素材）が見つかりません。", attempts=attempt.attempts)
    except Exception:
        logger.exception("render_video %s: unexpected failure", video_id)
        _fail_video(
            video_id, "レンダリング中に予期しないエラーが発生しました。再試行してください。", attempts=attempt.attempts
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
        with SessionLocal() as db:
            recompute_batch_status(db, batch_id)


def _claim_render(db: Session, video: Video, plan: dict[str, Any]) -> int | None:
    """Compare-and-swap ``queued``/``waiting_clips`` → ``rendering``; returns the claimed ``attempts``."""
    seen = int(video.attempts or 0)
    result = db.execute(
        update(Video)
        .where(
            Video.id == video.id,
            Video.status.in_([VideoStatus.QUEUED, VideoStatus.WAITING_CLIPS]),
            Video.attempts == seen,
        )
        .values(
            status=VideoStatus.RENDERING, attempts=seen + 1, plan=plan, progress=0.01, error=None, updated_at=utcnow()
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return seen + 1 if isinstance(result, CursorResult) and result.rowcount > 0 else None


def _render_once(video_id: str, batch_id: str, work: Path, attempt: _RenderAttempt) -> None:
    storage = get_storage()
    with SessionLocal() as db:
        video = db.get(Video, video_id)
        if video is None:
            raise JobCancelled(video_id)
        require_rights(video.project)  # consent can be withdrawn after the job was queued
    _ensure_video_clips(video_id, batch_id)

    with SessionLocal() as db:
        video = db.get(Video, video_id)
        batch = db.get(Batch, batch_id)
        if video is None or batch is None or batch.status != BatchStatus.RENDERING:
            raise JobCancelled(video_id)
        if video.status not in (VideoStatus.QUEUED, VideoStatus.WAITING_CLIPS):
            raise _ClaimLost(video_id)
        plan = copy.deepcopy(dict(video.plan or {}))
        provider, model = _batch_footage(batch)
        ok, substitutions = resolve_plan_clips(
            db, plan, video.project_id, preferred=_own_clip_ids(db, batch), provider=provider, model=model
        )
        if not ok:
            raise ServiceError("使用できる動画素材がないためレンダリングできませんでした。", code="no_clips")
        if substitutions:
            plan["substitutions"] = int(plan.get("substitutions") or 0) + substitutions
        mine = _claim_render(db, video, plan)
        if mine is None:
            raise _ClaimLost(video_id)
        attempt.attempts = mine
        db.refresh(video)

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
    result = render_service.render_video(spec, progress_cb=_RenderProgress(video_id, batch_id, mine))
    report = quality_service.validate_output(result.output_path, expected_duration=duration)
    if not report.get("passed"):
        _fail_video(video_id, _quality_error(report), attempts=mine, quality=report, progress=0.0)
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
        result_ok = db.execute(
            update(Video)
            .where(Video.id == video_id, Video.status == VideoStatus.RENDERING, Video.attempts == mine)
            .values(
                status=VideoStatus.DONE,
                asset_id=video_asset.id,
                thumbnail_asset_id=thumb_asset.id,
                plan={key: value for key, value in plan.items() if key != "previous"},
                quality=report,
                progress=1.0,
                error=None,
                rendered_at=utcnow(),
                updated_at=utcnow(),
            )
            .execution_options(synchronize_session=False)
        )
        if not (isinstance(result_ok, CursorResult) and result_ok.rowcount > 0):
            # Cancelled (or taken over) while finishing: drop the new files.
            db.rollback()
            for key in (video_asset.storage_key, thumb_asset.storage_key):
                storage.delete(key)
            raise JobCancelled(video_id)
        db.commit()
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


def _check_batch_budget(db: Session, batch: Batch, what: str) -> None:
    """422 ``budget_exceeded`` unless spend + every planned (pending) clip of the batch fits its budget (flushes)."""
    db.flush()  # SessionLocal has autoflush off; clips just planned must be visible to the query
    needed = _pending_budget_usd(db, batch)
    limit = _num((batch.estimate or {}).get("budget_limit_usd"))
    spent = float(db.scalar(select(Batch.cost_usd).where(Batch.id == batch.id)) or 0.0)
    if limit is not None and needed > 0 and spent + needed > limit + 1e-9:
        db.rollback()
        raise BudgetExceeded(
            f"{what} {_money(needed)} を加えると予算上限 {_money(limit)} を超えます（使用済み {_money(spent)}）。",
            estimate_usd=spent + needed,
            limit_usd=limit,
        )


def _status_for_plan(db: Session, plan: Mapping[str, Any]) -> str:
    """``queued`` when every clip of the plan is ready, else ``waiting_clips`` (the render task generates/waits)."""
    ids = video_clip_ids(dict(plan))
    statuses = set(db.scalars(select(Clip.status).where(Clip.id.in_(ids)))) if ids else set()
    return VideoStatus.WAITING_CLIPS if statuses & {ClipStatus.PENDING, ClipStatus.GENERATING} else VideoStatus.QUEUED


def prepare_retry(db: Session, video: Video) -> None:
    """``POST /videos/{id}/retry``: failed/cancelled videos are queued again with the same plan.

    Clips of the plan that this batch failed or cancelled (provider outage,
    budget stop, cancel) are reset to ``pending`` so the render task generates
    them again — subject to the batch budget (422 ``budget_exceeded``).
    """
    if video.status not in (VideoStatus.FAILED, VideoStatus.CANCELLED):
        raise ServiceError(
            "失敗またはキャンセルされた動画のみ再試行できます。", status_code=409, code="video_not_retryable"
        )
    require_rights(video.project)
    batch = video.batch
    ids = video_clip_ids(video.plan)
    retry_clips = [
        clip
        for clip in (db.scalars(select(Clip).where(Clip.id.in_(ids))).all() if ids else [])
        if clip.status in (ClipStatus.FAILED, ClipStatus.CANCELLED) and _clip_batch_id(clip) == batch.id
    ]
    for clip in retry_clips:
        clip.status = ClipStatus.PENDING
        clip.attempts = 0
        clip.error = None
        clip.params = _without(clip.params, "budget_refused", "failure_code", "reserved_usd", "open_job")
    if retry_clips:
        _check_batch_budget(db, batch, "失敗した素材の再生成")
    video.status = VideoStatus.WAITING_CLIPS if retry_clips else _status_for_plan(db, video.plan or {})
    video.error = None
    video.progress = 0.0
    reopen_batch(db, batch)
    db.commit()


def apply_video_edit(db: Session, video: Video, edit: VideoEdit) -> None:
    """Merge :class:`VideoEdit` overrides into the plan, bump ``render_version`` and queue a re-render.

    The finished render stays the fallback (``plan.previous``) until the new one succeeds.
    """
    _require_idle(video)
    project = video.project
    require_rights(project)
    previous = _previous_render(video)
    plan = copy.deepcopy(dict(video.plan or {}))
    plan.pop("previous", None)
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
    if previous is not None:
        plan["previous"] = previous
    video.plan = plan
    video.render_version = int(video.render_version or 1) + 1
    video.status = _status_for_plan(db, plan)
    video.error = None
    video.progress = 0.0
    reopen_batch(db, video.batch)
    db.commit()


def regenerate_video_plan(db: Session, video: Video, *, new_clips: bool) -> None:
    """``POST /videos/{id}/regenerate``: a new plan variant (new seed) for one video, then re-render.

    The video keeps its own start, lyric style, per-line lyric timing and (when
    edited, or fixed by the batch) colour grade and text overrides — only the
    shots change. Without ``new_clips`` only
    ready (or in-flight) clips are used (no spend). With it, fresh clips are
    planned (pending Clip rows, generated by the render task within the batch
    budget); the admission check counts every clip the batch still has to
    generate. The finished render stays the fallback until the new one succeeds.
    """
    _require_idle(video)
    batch = video.batch
    project = video.project
    _require_analysis(project)
    require_rights(project)
    current = dict(video.plan or {})
    data, provider = resolve_generation_settings(project, dict(batch.settings or {}))
    data["count"] = 1
    # Planning a single video would otherwise give it video #1's start / lyric style / grade.
    if _num(current.get("start")) is not None:
        data["start_strategy"] = "custom"
        data["start_sec"] = float(current["start"])
    if current.get("lyric_style"):
        data["lyric_style"] = str(current["lyric_style"])
    if current.get("color_grade") and (current.get("edited") or data.get("color_grade") != "auto"):
        data["color_grade"] = str(current["color_grade"])
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
    plan = _plan(project, data, storyboard, provider, existing_clip_refs(db, project.id, include_in_flight=True), seed)
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
        _check_batch_budget(db, batch, "新しい素材の生成")
    previous = _previous_render(video)
    video_plan.update({"index": video.index, "title": video.title, "seed": seed, "regenerated": True})
    # Manual edits survive: per-line lyric timing always, the text overrides once the video was edited.
    if current.get("lyric_offsets"):
        video_plan["lyric_offsets"] = copy.deepcopy(current["lyric_offsets"])
    if current.get("edited"):
        video_plan["edited"] = True
        if current.get("text_style") is not None:
            video_plan["text_style"] = copy.deepcopy(current["text_style"])
    if previous is not None:
        video_plan["previous"] = previous
    video.plan = video_plan
    video.render_version = int(video.render_version or 1) + 1
    db.flush()
    video.status = _status_for_plan(db, video_plan)
    video.error = None
    video.progress = 0.0
    reopen_batch(db, batch)
    db.commit()


def abandon_video(db: Session, video_id: str, message: str) -> None:
    """A (re-)render that could not be queued (queue down): ``failed`` — or back to the previous
    render — and the re-opened batch is recomputed instead of staying ``rendering``."""
    batch_id = db.scalar(select(Video.batch_id).where(Video.id == video_id))
    _end_video(db, video_id, VideoStatus.FAILED, message, allowed=ACTIVE_VIDEO_STATUSES)
    if batch_id is not None:
        recompute_batch_status(db, batch_id)


# --- recovery -----------------------------------------------------------------------------------------------------


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


def _reset_orphaned_clips(db: Session, cutoff: datetime) -> int:
    """``generating`` clips whose task died (no heartbeat since ``cutoff``) → ``pending`` again.

    Their budget reservation is settled: an attempt that may have reached the
    provider (an API provider, unless it reports submissions and none was
    reported) is booked as presumed billed — the remote job keeps running —
    and a job that can be resumed keeps its id (``params.open_job``).
    """
    reset = 0
    for clip in db.scalars(select(Clip).where(Clip.status == ClipStatus.GENERATING, Clip.updated_at <= cutoff)).all():
        params = dict(clip.params or {})
        reserved = _num(params.pop("reserved_usd", None)) or 0.0
        open_job = params.get("open_job")
        try:
            provider: VideoProvider | None = get_provider(clip.provider)
        except ServiceError:
            provider = None
        api = provider is not None and provider.kind == "api"
        resumable = bool(open_job) and provider is not None and callable(getattr(provider, "resume", None))
        reports_submit = provider is not None and _accepts_keyword(provider.generate, "on_submitted")
        presumed_billed = api and not resumable and reserved > 0 and (bool(open_job) or not reports_submit)
        if not resumable:
            params.pop("open_job", None)
            params.pop("submitted_usd", None)
        result = db.execute(
            update(Clip)
            .where(Clip.id == clip.id, Clip.status == ClipStatus.GENERATING, Clip.updated_at <= cutoff)
            .values(status=ClipStatus.PENDING, params=params, updated_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        if not (isinstance(result, CursorResult) and result.rowcount > 0):
            continue
        reset += 1
        batch_id = _clip_batch_id(clip)
        if presumed_billed:
            units = float(params.get("duration") or cost_service.TARGET_CLIP_SEC)
            db.add(
                CostRecord(
                    project_id=clip.project_id,
                    batch_id=batch_id,
                    clip_id=clip.id,
                    kind=CostKind.VIDEO_GENERATION,
                    provider=clip.provider,
                    model=clip.model,
                    units=units,
                    unit=CostUnit.SECOND,
                    unit_price_usd=round(reserved / units, 6) if units else 0.0,
                    amount_usd=round(reserved, 6),
                    cached=False,
                    note="中断された生成ジョブ（課金済みとみなす）",
                )
            )
            db.execute(
                update(Clip)
                .where(Clip.id == clip.id)
                .values(cost_usd=Clip.cost_usd + reserved)
                .execution_options(synchronize_session=False)
            )
        else:
            _add_batch_cost(db, batch_id, -reserved)  # release the reservation
        db.commit()
    return reset


def recover_interrupted_jobs(*, stale_after: float | None = None) -> dict[str, int]:
    """Re-enqueue work a dead process/worker left unfinished; every task is idempotent.

    * Inline mode (``stale_after=None``, at API start-up): tasks lived in the
      previous process, so all unfinished work is resumed — analyses, upstream
      batches (``run_batch`` resumes), queued/waiting/rendering videos of
      ``rendering`` batches (rendering ones are reset to ``queued`` first) —
      and old scratch directories are removed.
    * Celery mode (``stale_after`` seconds, on ``worker_ready``): only work
      idle for longer than that is picked up (other workers may be running);
      duplicates are harmless because clips and renders are claimed.

    Both settle terminal batches that still have active videos or pending
    clips, and reset ``generating`` clips whose task died (see
    :func:`_reset_orphaned_clips`).
    """
    sweep = stale_after is not None
    if not sweep:
        removed = clean_stale_work_dirs()
        if removed:
            logger.info("removed %d stale work directories", removed)
    now = utcnow()
    cutoff = now - timedelta(seconds=stale_after or 0.0)
    counts = {"analyses": 0, "batches": 0, "videos": 0}
    with SessionLocal() as db:
        # Upstream batches first: resetting their orphaned clips below counts as activity.
        batches = db.execute(
            select(Batch.id, Batch.status, Batch.project_id, Batch.created_at, Batch.started_at).where(
                Batch.status.in_(list(ACTIVE_BATCH_STATUSES))
            )
        ).all()
        upstream = [
            batch_id
            for batch_id, status, project_id, created_at, started_at in batches
            if status in _UPSTREAM
            and (not sweep or _batch_idle_since(db, (batch_id, project_id, created_at, started_at)) <= cutoff)
        ]
        clip_cutoff = now - timedelta(seconds=max(stale_after or 0.0, STALE_CLIP_SEC)) if sweep else now
        clips_reset = _reset_orphaned_clips(db, clip_cutoff)
        analyses = select(Project.id).where(
            Project.analysis_status.in_([AnalysisStatus.QUEUED, AnalysisStatus.RUNNING]),
            Project.audio_asset_id.is_not(None),
        )
        if sweep:
            analyses = analyses.where(Project.updated_at <= cutoff)
        projects = db.scalars(analyses).all()

        # Terminal batches with active videos or pending clips left behind (e.g. by an older version).
        leftovers = set(
            db.scalars(
                select(Video.batch_id)
                .join(Batch, Batch.id == Video.batch_id)
                .where(~Batch.status.in_(list(ACTIVE_BATCH_STATUSES)), Video.status.in_(list(ACTIVE_VIDEO_STATUSES)))
            ).all()
        )
        pending_owners = {
            b
            for b in (_clip_batch_id(c) for c in db.scalars(select(Clip).where(Clip.status == ClipStatus.PENDING)))
            if b
        }
        if pending_owners:
            leftovers |= set(
                db.scalars(
                    select(Batch.id).where(Batch.id.in_(pending_owners), ~Batch.status.in_(list(ACTIVE_BATCH_STATUSES)))
                ).all()
            )
        settled = 0
        for batch_id in sorted(leftovers):
            settled += _cleanup_terminal_batch(db, batch_id)

        for batch_id, status, *_rest in batches:
            if status == BatchStatus.RENDERING:
                recompute_batch_status(db, batch_id)

        videos = db.execute(
            select(Video.id, Video.status, Video.updated_at)
            .join(Batch, Batch.id == Video.batch_id)
            .where(Batch.status == BatchStatus.RENDERING, Video.status.in_(list(ACTIVE_VIDEO_STATUSES)))
            .order_by(Video.created_at)
        ).all()
        video_ids: list[str] = []
        for video_id, status, updated_at in videos:
            if sweep and updated_at is not None and updated_at > cutoff:
                continue
            if status == VideoStatus.RENDERING and not _transition_video(
                db, video_id, [VideoStatus.RENDERING], VideoStatus.QUEUED, progress=0.0
            ):
                continue
            video_ids.append(video_id)
    for project_id in projects:
        worker.enqueue("analyze", project_id)
        counts["analyses"] += 1
    for batch_id in upstream:
        worker.enqueue("run_batch", batch_id)
        counts["batches"] += 1
    for video_id in video_ids:
        worker.enqueue("render_video", video_id)
        counts["videos"] += 1
    if any(counts.values()) or clips_reset or settled:
        logger.warning("resumed interrupted jobs: %s (clips reset %d, videos settled %d)", counts, clips_reset, settled)
    return counts


def _batch_idle_since(db: Session, batch: tuple[str, str, datetime | None, datetime | None]) -> datetime:
    """Last sign of life of an upstream batch: creation, start of planning or its clips' latest update
    (generating clips heartbeat every :data:`CLIP_HEARTBEAT_SEC`)."""
    batch_id, project_id, created_at, started_at = batch
    moments = [m for m in (created_at, started_at) if m is not None]
    moments += [c.updated_at for c in _batch_clips(db, batch_id, project_id, list(ClipStatus)) if c.updated_at]
    return max(moments) if moments else datetime.min.replace(tzinfo=UTC)


worker.register_task("analyze", analyze_project_audio)
worker.register_task("run_batch", run_batch)
worker.register_task("render_video", render_video)
