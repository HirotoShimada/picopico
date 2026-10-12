"""Storyboard preview, cost estimate and generation batches (ARCHITECTURE.md §5.6).

``POST /batches`` runs the same planning as ``POST /estimate`` and refuses with
422 (``{detail, code}``) when the song is missing/unanalysed
(``audio_missing``/``analysis_missing``), the rights checklist is incomplete
(``rights_unconfirmed``), the provider/model cannot be used
(``provider_not_configured``), ``library`` has no uploads (``library_empty``)
or the estimate exceeds the budget (``budget_exceeded``).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app import worker
from app.config import get_settings
from app.db import DbSession
from app.models import Batch, BatchStatus, utcnow
from app.pipeline import (
    cancel_batch,
    create_batch,
    llm_cost_label,
    llm_cost_record,
    preview_batch,
    update_batch_budget,
)
from app.routers.videos import zip_link, zip_response
from app.schemas import (
    BatchOut,
    BatchUpdate,
    Estimate,
    GenerationSettings,
    Storyboard,
    StoryboardRequest,
    ZipLinkOut,
)
from app.security import CurrentUser, SlidingWindowLimiter, get_owned_batch, get_owned_project
from app.serializers import to_batch_out
from app.services.errors import ServiceError
from app.services.media import sanitize_filename

router = APIRouter(tags=["generation"])


# Paid Claude previews per user (template previews are not limited). In-process: one inline API process.
CLAUDE_PREVIEW_LIMIT = 10
CLAUDE_PREVIEW_WINDOW_SEC = 600
claude_preview_limiter = SlidingWindowLimiter(CLAUDE_PREVIEW_LIMIT, CLAUDE_PREVIEW_WINDOW_SEC)
STORYBOARD_LYRICS_RIGHTS_WARNING = (
    "権利確認の「歌詞の権利」が未確認のため、歌詞を外部の AI（Claude）に送らず、テンプレートで絵コンテを作成しました。"
    "Claude の絵コンテを見るには、権利確認タブで歌詞の権利を確認してください。"
)


def _claude_preview_allowed(user_id: str) -> None:
    wait = claude_preview_limiter.acquire(user_id)
    if wait is not None:
        raise ServiceError(
            f"Claude による絵コンテのプレビューは {CLAUDE_PREVIEW_WINDOW_SEC // 60} 分間に "
            f"{CLAUDE_PREVIEW_LIMIT} 回までです。約 {wait} 秒後にもう一度お試しください。",
            status_code=429,
            code="rate_limited",
            headers={"Retry-After": str(wait)},
        )


@router.post("/projects/{project_id}/storyboard", response_model=Storyboard)
def preview_storyboard(project_id: str, body: StoryboardRequest, db: DbSession, user: CurrentUser) -> Any:
    """Storyboard preview (not stored). Uses Claude when configured; its cost is still recorded.

    Claude is reached directly (``ANTHROPIC_API_KEY``) or else via OpenRouter (``OPENROUTER_API_KEY``);
    both routes get the same guards. Claude receives the lyrics, so it is used only when the
    ``lyrics_rights`` item is confirmed (otherwise the template is used, with a warning), and at most
    ``CLAUDE_PREVIEW_LIMIT`` times per ``CLAUDE_PREVIEW_WINDOW_SEC`` per user (429 ``rate_limited``).
    """
    from app.services.storyboard import generate_storyboard

    project = get_owned_project(db, project_id, user)
    settings = get_settings()
    use_claude = settings.llm_configured  # Anthropic direct or OpenRouter: same guards for both routes
    warnings: list[str] = []
    if use_claude and project.lyric_lines and not (project.rights or {}).get("lyrics_rights"):
        use_claude = False
        warnings.append(STORYBOARD_LYRICS_RIGHTS_WARNING)
    if use_claude:
        _claude_preview_allowed(user.id)
    storyboard = generate_storyboard(
        title=project.title,
        artist_name=project.artist_name,
        mood=project.mood,
        purpose=project.purpose,
        lyrics=list(project.lyric_lines or []),
        analysis=project.analysis if project.analysis_status == "done" else None,
        preset_id=body.preset,
        custom_world=body.custom_world or project.custom_world,
        n_scenes=body.scenes,
        seed=None,
        use_claude=use_claude,
        artist_aliases=list(project.artist_aliases or []),
        end_user=user.id,
    )
    if warnings:
        storyboard["warnings"] = [*warnings, *(storyboard.get("warnings") or [])]
    record = llm_cost_record(project.id, None, storyboard)
    if record is not None:
        record.note = llm_cost_label(storyboard, "絵コンテのプレビュー")
        db.add(record)
        db.commit()
    return storyboard


@router.post("/projects/{project_id}/estimate", response_model=Estimate)
def estimate_batch(project_id: str, body: GenerationSettings, db: DbSession, user: CurrentUser) -> Any:
    project = get_owned_project(db, project_id, user)
    return preview_batch(db, project, body).estimate


@router.post("/projects/{project_id}/batches", response_model=BatchOut, status_code=status.HTTP_201_CREATED)
def create_generation_batch(project_id: str, body: GenerationSettings, db: DbSession, user: CurrentUser) -> BatchOut:
    project = get_owned_project(db, project_id, user)
    batch = create_batch(db, project, body)
    try:
        worker.enqueue("run_batch", batch.id)
    except worker.QueueUnavailable as exc:
        batch.status = BatchStatus.FAILED
        batch.error = exc.message
        batch.finished_at = utcnow()
        db.commit()
        raise
    return to_batch_out(db, batch)


@router.get("/projects/{project_id}/batches", response_model=list[BatchOut])
def list_batches(project_id: str, db: DbSession, user: CurrentUser) -> list[BatchOut]:
    project = get_owned_project(db, project_id, user)
    batches = db.scalars(
        select(Batch).where(Batch.project_id == project.id).order_by(Batch.created_at.desc(), Batch.id)
    ).all()
    return [to_batch_out(db, b) for b in batches]


@router.get("/batches/{batch_id}", response_model=BatchOut)
def get_batch(batch_id: str, db: DbSession, user: CurrentUser) -> BatchOut:
    return to_batch_out(db, get_owned_batch(db, batch_id, user), include_children=True)


@router.patch("/batches/{batch_id}", response_model=BatchOut)
def update_generation_batch(batch_id: str, body: BatchUpdate, db: DbSession, user: CurrentUser) -> BatchOut:
    """Change the batch budget (later clip generations and retries are checked against it)."""
    batch = get_owned_batch(db, batch_id, user)
    update_batch_budget(db, batch, body.budget_limit_usd)
    return to_batch_out(db, batch, include_children=True)


@router.post("/batches/{batch_id}/cancel", response_model=BatchOut)
def cancel_generation_batch(batch_id: str, db: DbSession, user: CurrentUser) -> BatchOut:
    batch = get_owned_batch(db, batch_id, user)
    cancel_batch(db, batch)
    return to_batch_out(db, batch, include_children=True)


def _batch_zip_name(batch: Batch) -> str:
    project = batch.project
    return sanitize_filename(f"{project.artist_name}_{project.title}_videos.zip", default="videos.zip")


@router.get("/batches/{batch_id}/zip")
def download_batch_zip(batch_id: str, db: DbSession, user: CurrentUser) -> StreamingResponse:
    batch = get_owned_batch(db, batch_id, user)
    return zip_response(list(batch.videos), _batch_zip_name(batch))


@router.post("/batches/{batch_id}/zip-link", response_model=ZipLinkOut)
def batch_zip_link(batch_id: str, db: DbSession, user: CurrentUser) -> ZipLinkOut:
    """Signed link to the ZIP of the batch's finished videos (browser-native download, also in token mode)."""
    batch = get_owned_batch(db, batch_id, user)
    return zip_link(user.id, list(batch.videos), _batch_zip_name(batch))
