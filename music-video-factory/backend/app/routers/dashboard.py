"""Dashboard summary (ARCHITECTURE.md §5.1), scoped to the current user.

Usage figures come from ``CostRecord`` rows: ``clips_generated``/``clips_reused``
count ``video_generation`` records without/with ``cached``; ``saved_usd`` is
Σ ``units × unit_price_usd`` over cached records (spend avoided by reuse).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from fastapi import APIRouter
from sqlalchemy import case, func, select

from app.db import DbSession
from app.models import (
    ACTIVE_BATCH_STATUSES,
    Batch,
    CostKind,
    CostRecord,
    Project,
    Video,
    VideoStatus,
    utcnow,
)
from app.schemas import ActiveJob, DashboardOut, ProviderUsage, UsageOut
from app.security import CurrentUser
from app.serializers import to_video_outs

router = APIRouter(tags=["dashboard"])

RECENT_VIDEOS_LIMIT = 12  # fills whole rows of the dashboard grid (3 / 4 / 6 columns)


def _money(value: float | None) -> float:
    return round(float(value or 0.0), 4)


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@router.get("/dashboard", response_model=DashboardOut)
def get_dashboard(db: DbSession, user: CurrentUser) -> DashboardOut:
    owned = Project.owner_id == user.id

    project_count = db.scalar(select(func.count()).select_from(Project).where(owned)) or 0

    video_status_rows = db.execute(
        select(Video.status, func.count())
        .join(Project, Project.id == Video.project_id)
        .where(owned)
        .group_by(Video.status)
    ).all()
    videos_by_status = {s: int(n) for s, n in video_status_rows}

    active_rows = db.execute(
        select(Batch, Project.title)
        .join(Project, Project.id == Batch.project_id)
        .where(owned, Batch.status.in_(ACTIVE_BATCH_STATUSES))
        .order_by(Batch.created_at.desc())
    ).all()
    active_ids = [batch.id for batch, _ in active_rows]
    per_batch: dict[str, dict[str, int]] = defaultdict(dict)
    if active_ids:
        for batch_id, status, n in db.execute(
            select(Video.batch_id, Video.status, func.count())
            .where(Video.batch_id.in_(active_ids))
            .group_by(Video.batch_id, Video.status)
        ):
            per_batch[batch_id][status] = int(n)
    active_jobs = [
        ActiveJob(
            batch_id=batch.id,
            project_id=batch.project_id,
            project_title=title,
            status=batch.status,  # type: ignore[arg-type]
            done=per_batch[batch.id].get(VideoStatus.DONE, 0),
            total=sum(per_batch[batch.id].values()),
            failed=per_batch[batch.id].get(VideoStatus.FAILED, 0),
        )
        for batch, title in active_rows
    ]
    pending_estimate = sum(
        max(0.0, float((batch.estimate or {}).get("total_usd") or 0.0) - float(batch.cost_usd or 0.0))
        for batch, _ in active_rows
    )

    recent = db.scalars(
        select(Video)
        .join(Project, Project.id == Video.project_id)
        .where(owned, Video.status == VideoStatus.DONE)
        .order_by(func.coalesce(Video.rendered_at, Video.updated_at).desc())
        .limit(RECENT_VIDEOS_LIMIT)
    ).all()

    is_generation = CostRecord.kind == CostKind.VIDEO_GENERATION
    generated = case((is_generation & CostRecord.cached.is_(False), 1), else_=0)
    reused = case((is_generation & CostRecord.cached.is_(True), 1), else_=0)
    saved = case((CostRecord.cached.is_(True), CostRecord.units * CostRecord.unit_price_usd), else_=0.0)
    this_month = case((CostRecord.created_at >= _month_start(utcnow()), CostRecord.amount_usd), else_=0.0)
    totals = db.execute(
        select(
            func.coalesce(func.sum(CostRecord.amount_usd), 0.0),
            func.coalesce(func.sum(this_month), 0.0),
            func.coalesce(func.sum(generated), 0),
            func.coalesce(func.sum(reused), 0),
            func.coalesce(func.sum(saved), 0.0),
        )
        .select_from(CostRecord)
        .join(Project, Project.id == CostRecord.project_id)
        .where(owned)
    ).one()
    provider_amount = func.coalesce(func.sum(CostRecord.amount_usd), 0.0)
    provider_rows = db.execute(
        select(CostRecord.provider, provider_amount, func.coalesce(func.sum(generated), 0))
        .join(Project, Project.id == CostRecord.project_id)
        .where(owned)
        .group_by(CostRecord.provider)
        .order_by(provider_amount.desc(), CostRecord.provider)
    ).all()

    return DashboardOut(
        projects=int(project_count),
        videos_done=videos_by_status.get(VideoStatus.DONE, 0),
        videos_total=sum(videos_by_status.values()),
        active_jobs=active_jobs,
        recent_videos=to_video_outs(db, list(recent)),
        usage=UsageOut(
            total_usd=_money(totals[0]),
            month_usd=_money(totals[1]),
            by_provider=[
                ProviderUsage(provider=provider, amount_usd=_money(amount), clips=int(clips))
                for provider, amount, clips in provider_rows
            ],
            clips_generated=int(totals[2]),
            clips_reused=int(totals[3]),
            saved_usd=_money(totals[4]),
        ),
        pending_estimate_usd=_money(pending_estimate),
    )
