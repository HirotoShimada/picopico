"""ORM → API model conversion. Media fields become signed URLs via storage."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Asset, Batch, Clip, ClipStatus, Project, Video, VideoStatus
from app.schemas import (
    RIGHTS_ITEMS,
    AssetOut,
    BatchCounts,
    BatchOut,
    ClipCounts,
    ClipOut,
    ProjectCounts,
    ProjectOut,
    ProjectSummary,
    VideoOut,
)
from app.storage import get_storage


def media_url(key: str | None, *, download: bool = False, filename: str | None = None) -> str | None:
    if not key:
        return None
    return get_storage().url(key, download=download, filename=filename)


def _asset_url(asset: Asset | None) -> str | None:
    return media_url(asset.storage_key) if asset is not None else None


# --- rights -------------------------------------------------------------------------------


def rights_with_defaults(rights: dict[str, Any] | None) -> dict[str, bool]:
    """Every §9 checklist key (False when unset), plus any stored extras."""
    merged = {key: False for key in RIGHTS_ITEMS}
    for key, value in (rights or {}).items():
        merged[key] = bool(value)
    return merged


def all_rights_confirmed(rights: dict[str, Any] | None) -> bool:
    return all(bool((rights or {}).get(key)) for key in RIGHTS_ITEMS)


# --- assets & clips -------------------------------------------------------------------------


def to_asset_out(asset: Asset) -> AssetOut:
    return AssetOut(
        id=asset.id,
        kind=asset.kind,  # type: ignore[arg-type]
        filename=asset.filename,
        content_type=asset.content_type,
        size_bytes=asset.size_bytes,
        duration=asset.duration,
        width=asset.width,
        height=asset.height,
        url=media_url(asset.storage_key) or "",
        created_at=asset.created_at,
    )


def video_clip_ids(plan: dict[str, Any] | None) -> set[str]:
    """Clip IDs referenced by a VideoPlan's shots."""
    ids: set[str] = set()
    for shot in (plan or {}).get("shots") or []:
        if isinstance(shot, dict) and shot.get("clip_id"):
            ids.add(str(shot["clip_id"]))
    return ids


def clip_usage_counts(db: Session, project_id: str) -> Counter[str]:
    """clip_id → number of videos of the project whose plan uses it (cancelled videos do not count)."""
    usage: Counter[str] = Counter()
    for plan in db.scalars(
        select(Video.plan).where(Video.project_id == project_id, Video.status != VideoStatus.CANCELLED)
    ):
        usage.update(video_clip_ids(plan))
    return usage


def to_clip_out(clip: Clip, *, used_in_videos: int = 0) -> ClipOut:
    ready = clip.status == ClipStatus.READY
    return ClipOut(
        id=clip.id,
        source=clip.source,  # type: ignore[arg-type]
        provider=clip.provider,
        model=clip.model,
        scene_id=clip.scene_id,
        prompt=clip.prompt,
        status=clip.status,  # type: ignore[arg-type]
        duration=clip.duration,
        width=clip.width,
        height=clip.height,
        cost_usd=clip.cost_usd or 0.0,
        error=clip.error,
        url=_asset_url(clip.asset) if ready else None,
        thumbnail_url=_asset_url(clip.thumbnail_asset),
        created_at=clip.created_at,
        used_in_videos=used_in_videos,
    )


def prefetch_assets(db: Session, rows: Iterable[Clip | Video]) -> list[Asset]:
    """Load the assets/thumbnails of ``rows`` in one query.

    ``row.asset`` / ``row.thumbnail_asset`` are many-to-one by primary key, so
    SQLAlchemy then resolves them from the identity map instead of issuing one
    query per row (lists of 30+ videos/clips are polled every few seconds).
    The session's identity map holds objects weakly: keep the returned list
    alive while serializing.
    """
    ids = {asset_id for row in rows for asset_id in (row.asset_id, row.thumbnail_asset_id) if asset_id}
    return list(db.scalars(select(Asset).where(Asset.id.in_(ids)))) if ids else []


def to_video_outs(db: Session, videos: Sequence[Video]) -> list[VideoOut]:
    assets = prefetch_assets(db, videos)  # noqa: F841 - keeps the prefetched rows in the identity map
    return [to_video_out(v) for v in videos]


def to_clip_outs(db: Session, clips: Sequence[Clip]) -> list[ClipOut]:
    """Serialize clips (of one or more projects) with ``used_in_videos`` filled in."""
    assets = prefetch_assets(db, clips)  # noqa: F841 - keeps the prefetched rows in the identity map
    usage: Counter[str] = Counter()
    for project_id in {c.project_id for c in clips}:
        usage.update(clip_usage_counts(db, project_id))
    return [to_clip_out(c, used_in_videos=usage.get(c.id, 0)) for c in clips]


# --- videos & batches -------------------------------------------------------------------------


def to_video_out(video: Video) -> VideoOut:
    plan = dict(video.plan or {})
    duration = video.asset.duration if video.asset is not None else None
    if duration is None and isinstance(plan.get("duration"), (int, float)):
        duration = float(plan["duration"])
    done = video.status == VideoStatus.DONE
    return VideoOut(
        id=video.id,
        project_id=video.project_id,
        batch_id=video.batch_id,
        index=video.index,
        title=video.title,
        status=video.status,  # type: ignore[arg-type]
        progress=video.progress or 0.0,
        error=video.error,
        favorite=video.favorite,
        url=_asset_url(video.asset) if done else None,
        thumbnail_url=_asset_url(video.thumbnail_asset),
        duration=duration,
        quality=video.quality,
        plan=plan,
        render_version=video.render_version,
        created_at=video.created_at,
        rendered_at=video.rendered_at,
    )


def batch_clip_ids(batch: Batch, videos: Iterable[Video]) -> list[str]:
    """Clip IDs belonging to a batch, from its plan's clip specs and its videos' shots."""
    ids: dict[str, None] = {}
    for spec in (batch.plan or {}).get("clip_specs") or []:
        if isinstance(spec, dict):
            for field in ("clip_id", "existing_clip_id"):
                if spec.get(field):
                    ids[str(spec[field])] = None
    for video in videos:
        for clip_id in sorted(video_clip_ids(video.plan)):
            ids[clip_id] = None
    return list(ids)


def batch_counts(db: Session, batch_id: str) -> BatchCounts:
    rows = db.execute(select(Video.status, func.count()).where(Video.batch_id == batch_id).group_by(Video.status)).all()
    by_status = {status: int(count) for status, count in rows}
    return BatchCounts(
        total=sum(by_status.values()),
        done=by_status.get(VideoStatus.DONE, 0),
        rendering=by_status.get(VideoStatus.RENDERING, 0),
        queued=by_status.get(VideoStatus.QUEUED, 0) + by_status.get(VideoStatus.WAITING_CLIPS, 0),
        failed=by_status.get(VideoStatus.FAILED, 0),
        cancelled=by_status.get(VideoStatus.CANCELLED, 0),
    )


def _clip_counts(db: Session, batch: Batch, clip_ids: list[str]) -> ClipCounts:
    plan = batch.plan or {}
    stats = plan.get("stats") or {}
    specs = [s for s in plan.get("clip_specs") or [] if isinstance(s, dict)]
    reused = sum(1 for s in specs if s.get("existing_clip_id")) if specs else int(stats.get("reused_clips") or 0)
    if not clip_ids:
        return ClipCounts(total=int(stats.get("unique_clips") or 0), reused=reused)
    statuses = Counter(db.scalars(select(Clip.status).where(Clip.id.in_(clip_ids))).all())
    return ClipCounts(
        total=len(clip_ids),
        ready=statuses.get(ClipStatus.READY, 0),
        generating=statuses.get(ClipStatus.GENERATING, 0) + statuses.get(ClipStatus.PENDING, 0),
        failed=statuses.get(ClipStatus.FAILED, 0),
        reused=reused,
    )


def to_batch_out(db: Session, batch: Batch, *, include_children: bool = False) -> BatchOut:
    videos = list(batch.videos)
    clip_ids = batch_clip_ids(batch, videos)
    out = BatchOut(
        id=batch.id,
        project_id=batch.project_id,
        status=batch.status,  # type: ignore[arg-type]
        settings=dict(batch.settings or {}),
        estimate=batch.estimate,
        storyboard=batch.storyboard,
        error=batch.error,
        cost_usd=batch.cost_usd or 0.0,
        created_at=batch.created_at,
        started_at=batch.started_at,
        finished_at=batch.finished_at,
        counts=batch_counts(db, batch.id),
        clip_counts=_clip_counts(db, batch, clip_ids),
        reopened_from=(batch.plan or {}).get("reopened_from"),
        previous_error=(batch.plan or {}).get("previous_error"),
    )
    if include_children:
        out.videos = to_video_outs(db, videos)
        clips = list(db.scalars(select(Clip).where(Clip.id.in_(clip_ids)))) if clip_ids else []
        order = {cid: i for i, cid in enumerate(clip_ids)}
        clips.sort(key=lambda c: order.get(c.id, len(order)))
        out.clips = to_clip_outs(db, clips)
    return out


# --- projects ---------------------------------------------------------------------------------------


def project_counts(db: Session, project_id: str) -> ProjectCounts:
    def count(model: type[Clip] | type[Video] | type[Batch]) -> int:
        return int(db.scalar(select(func.count()).select_from(model).where(model.project_id == project_id)) or 0)

    return ProjectCounts(clips=count(Clip), videos=count(Video), batches=count(Batch))


def to_project_out(db: Session, project: Project) -> ProjectOut:
    audio = project.audio_asset
    return ProjectOut(
        id=project.id,
        title=project.title,
        artist_name=project.artist_name,
        artist_aliases=list(project.artist_aliases or []),
        mood=project.mood,
        purpose=project.purpose,
        notes=project.notes,
        audio_asset_id=project.audio_asset_id,
        audio=to_asset_out(audio) if audio is not None else None,
        analysis=project.analysis,
        analysis_status=project.analysis_status,  # type: ignore[arg-type]
        analysis_error=project.analysis_error,
        lyrics_text=project.lyrics_text or "",
        lyrics_format=project.lyrics_format or "txt",  # type: ignore[arg-type]
        lyric_lines=list(project.lyric_lines or []),  # type: ignore[arg-type]
        segment_start=project.segment_start,
        segment_end=project.segment_end,
        world_preset=project.world_preset,
        custom_world=project.custom_world,
        rights=rights_with_defaults(project.rights),
        rights_confirmed_at=project.rights_confirmed_at,
        budget_limit_usd=project.budget_limit_usd,
        created_at=project.created_at,
        updated_at=project.updated_at,
        counts=project_counts(db, project.id),
    )


def to_project_summaries(db: Session, projects: Sequence[Project]) -> list[ProjectSummary]:
    """Batch-serialize projects for the list view (constant number of queries)."""
    ids = [p.id for p in projects]
    if not ids:
        return []
    video_counts = dict(
        db.execute(
            select(Video.project_id, func.count()).where(Video.project_id.in_(ids)).group_by(Video.project_id)
        ).all()
    )
    covers: dict[str, str] = {}
    video_thumbs = db.execute(
        select(Video.project_id, Asset.storage_key)
        .join(Asset, Asset.id == Video.thumbnail_asset_id)
        .where(Video.project_id.in_(ids), Video.status == VideoStatus.DONE)
        .order_by(Video.updated_at.desc())
    ).all()
    clip_thumbs = db.execute(
        select(Clip.project_id, Asset.storage_key)
        .join(Asset, Asset.id == Clip.thumbnail_asset_id)
        .where(Clip.project_id.in_(ids), Clip.status == ClipStatus.READY)
        .order_by(Clip.created_at)
    ).all()
    for project_id, key in [*video_thumbs, *clip_thumbs]:
        covers.setdefault(project_id, key)
    return [
        ProjectSummary(
            id=p.id,
            title=p.title,
            artist_name=p.artist_name,
            created_at=p.created_at,
            updated_at=p.updated_at,
            has_audio=p.audio_asset_id is not None,
            analysis_status=p.analysis_status,  # type: ignore[arg-type]
            lyric_line_count=len(p.lyric_lines or []),
            video_count=int(video_counts.get(p.id, 0)),
            cover_url=media_url(covers.get(p.id)),
        )
        for p in projects
    ]


def to_project_summary(db: Session, project: Project) -> ProjectSummary:
    return to_project_summaries(db, [project])[0]
