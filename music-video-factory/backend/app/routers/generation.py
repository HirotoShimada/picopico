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
from app.pipeline import cancel_batch, create_batch, llm_cost_record, preview_batch, update_batch_budget
from app.routers.videos import zip_response
from app.schemas import BatchOut, BatchUpdate, Estimate, GenerationSettings, Storyboard, StoryboardRequest
from app.security import CurrentUser, get_owned_batch, get_owned_project
from app.serializers import to_batch_out
from app.services.media import sanitize_filename

router = APIRouter(tags=["generation"])


@router.post("/projects/{project_id}/storyboard", response_model=Storyboard)
def preview_storyboard(project_id: str, body: StoryboardRequest, db: DbSession, user: CurrentUser) -> Any:
    """Storyboard preview (not stored). Uses Claude when configured; its cost is still recorded."""
    from app.services.storyboard import generate_storyboard

    project = get_owned_project(db, project_id, user)
    settings = get_settings()
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
        use_claude=settings.anthropic_configured,
        artist_aliases=list(project.artist_aliases or []),
    )
    record = llm_cost_record(project.id, None, storyboard)
    if record is not None:
        record.note = "絵コンテのプレビュー（Claude）"
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


@router.get("/batches/{batch_id}/zip")
def download_batch_zip(batch_id: str, db: DbSession, user: CurrentUser) -> StreamingResponse:
    batch = get_owned_batch(db, batch_id, user)
    project = batch.project
    name = sanitize_filename(f"{project.artist_name}_{project.title}_videos.zip", default="videos.zip")
    return zip_response(list(batch.videos), name)
