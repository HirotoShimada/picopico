"""Output videos: list, detail, favourite/title, retry, regenerate, manual edit, download, ZIP (§5.7).

Every re-render keeps the video's identity; ``render_version`` increases on
edit and regenerate. Downloads redirect to a signed, short-lived URL with
``Content-Disposition: attachment``.
"""

from __future__ import annotations

import io
import logging
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query, status
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import select

from app import worker
from app.db import DbSession
from app.models import Batch, Clip, ClipStatus, Video, VideoStatus, utcnow
from app.pipeline import (
    abandon_video,
    apply_video_edit,
    download_filename,
    effective_lyric_lines,
    prepare_retry,
    regenerate_video_plan,
)
from app.schemas import VideoDetailOut, VideoEdit, VideoOut, VideoRegenerateRequest, VideoUpdate, VideoZipRequest
from app.security import CurrentUser, get_owned_project, get_owned_video
from app.serializers import to_clip_outs, to_video_out, to_video_outs
from app.services.errors import ServiceError
from app.storage import Storage, build_content_disposition, get_storage

logger = logging.getLogger(__name__)

router = APIRouter(tags=["videos"])

_ZIP_FLUSH_BYTES = 1024 * 1024


# --- ZIP streaming (shared with GET /api/batches/{id}/zip) --------------------------------------------------------


class _ZipSink(io.RawIOBase):
    """Write-only, non-seekable buffer: ``zipfile`` then emits data descriptors and never seeks."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._position = 0

    def writable(self) -> bool:
        return True

    def write(self, data: bytes | bytearray | memoryview) -> int:  # type: ignore[override]
        self._buffer += data
        self._position += len(data)
        return len(data)

    def tell(self) -> int:
        return self._position

    def pending(self) -> int:
        return len(self._buffer)

    def take(self) -> bytes:
        data = bytes(self._buffer)
        self._buffer.clear()
        return data


@dataclass(frozen=True)
class _ZipEntry:
    name: str
    key: str
    size: int
    modified: datetime | None


def _zip_entries(videos: Sequence[Video]) -> list[_ZipEntry]:
    entries: list[_ZipEntry] = []
    used: set[str] = set()
    for video in videos:
        if video.status != VideoStatus.DONE or video.asset is None:
            continue
        name = download_filename(video.project, video)
        stem, suffix = Path(name).stem, Path(name).suffix
        counter = 2
        while name.lower() in used:
            name = f"{stem}_{counter}{suffix}"
            counter += 1
        used.add(name.lower())
        entries.append(_ZipEntry(name, video.asset.storage_key, int(video.asset.size_bytes or 0), video.rendered_at))
    return entries


def _iter_zip(storage: Storage, entries: Sequence[_ZipEntry]) -> Iterator[bytes]:
    sink = _ZipSink()
    with zipfile.ZipFile(sink, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for entry in entries:
            when = (entry.modified or utcnow()).timetuple()[:6]
            info = zipfile.ZipInfo(entry.name, date_time=when)
            info.compress_type = zipfile.ZIP_STORED
            info.file_size = entry.size  # lets zipfile pick ZIP64 headers up front when needed
            info.external_attr = 0o644 << 16
            with archive.open(info, mode="w") as dest:
                for chunk in storage.iter_bytes(entry.key):
                    dest.write(chunk)
                    if sink.pending() >= _ZIP_FLUSH_BYTES:
                        yield sink.take()
            if sink.pending():
                yield sink.take()
    if sink.pending():
        yield sink.take()


def zip_response(videos: Sequence[Video], archive_name: str) -> StreamingResponse:
    """Stream the finished videos as an uncompressed ZIP (MP4 does not compress further)."""
    entries = _zip_entries(videos)
    if not entries:
        raise ServiceError("ダウンロードできる完成済みの動画がありません。", status_code=409, code="no_videos")
    return StreamingResponse(
        _iter_zip(get_storage(), entries),
        media_type="application/zip",
        headers={"Content-Disposition": build_content_disposition(archive_name)},
    )


# --- endpoints ----------------------------------------------------------------------------------------------------


def _enqueue_render(db: DbSession, video: Video) -> None:
    try:
        worker.enqueue("render_video", video.id)
    except worker.QueueUnavailable:
        # The video fails (or keeps its previous render) and the re-opened batch is recomputed.
        abandon_video(db, video.id, "ジョブキューに接続できないため、レンダリングを開始できませんでした。")
        raise


@router.get("/projects/{project_id}/videos", response_model=list[VideoOut])
def list_videos(
    project_id: str,
    db: DbSession,
    user: CurrentUser,
    favorite: Annotated[bool | None, Query()] = None,
    batch_id: Annotated[str | None, Query(max_length=32)] = None,
) -> list[VideoOut]:
    project = get_owned_project(db, project_id, user)
    stmt = (
        select(Video)
        .join(Batch, Batch.id == Video.batch_id)
        .where(Video.project_id == project.id)
        .order_by(Batch.created_at.desc(), Video.index)
    )
    if favorite is not None:
        stmt = stmt.where(Video.favorite.is_(favorite))
    if batch_id:
        stmt = stmt.where(Video.batch_id == batch_id)
    return to_video_outs(db, list(db.scalars(stmt)))


@router.post("/videos/zip")
def zip_selected_videos(body: VideoZipRequest, db: DbSession, user: CurrentUser) -> StreamingResponse:
    ids = list(dict.fromkeys(body.video_ids))
    videos = [get_owned_video(db, video_id, user) for video_id in ids]
    return zip_response(videos, "videos.zip")


@router.get("/videos/{video_id}", response_model=VideoDetailOut)
def get_video(video_id: str, db: DbSession, user: CurrentUser) -> VideoDetailOut:
    video = get_owned_video(db, video_id, user)
    project = video.project
    clips = db.scalars(
        select(Clip)
        .where(Clip.project_id == project.id, Clip.status == ClipStatus.READY, Clip.asset_id.is_not(None))
        .order_by(Clip.created_at, Clip.id)
    ).all()
    base = to_video_out(video)
    return VideoDetailOut(
        **base.model_dump(),
        lyric_lines=effective_lyric_lines(project.lyric_lines, (video.plan or {}).get("lyric_offsets")),  # type: ignore[arg-type]
        available_clips=to_clip_outs(db, clips),
    )


@router.patch("/videos/{video_id}", response_model=VideoOut)
def update_video(video_id: str, body: VideoUpdate, db: DbSession, user: CurrentUser) -> VideoOut:
    video = get_owned_video(db, video_id, user)
    changes = body.model_dump(exclude_unset=True)
    if "favorite" in changes and changes["favorite"] is not None:
        video.favorite = bool(changes["favorite"])
    if "title" in changes:
        if changes["title"] is None:
            raise ServiceError("タイトルは空にできません。", status_code=422, code="title_required")
        video.title = changes["title"].strip() or video.title
    db.commit()
    return to_video_out(video)


@router.post("/videos/{video_id}/retry", response_model=VideoOut)
def retry_video(video_id: str, db: DbSession, user: CurrentUser) -> VideoOut:
    video = get_owned_video(db, video_id, user)
    prepare_retry(db, video)
    _enqueue_render(db, video)
    db.refresh(video)
    return to_video_out(video)


@router.post("/videos/{video_id}/regenerate", response_model=VideoOut)
def regenerate_video(
    video_id: str, db: DbSession, user: CurrentUser, body: VideoRegenerateRequest | None = None
) -> VideoOut:
    video = get_owned_video(db, video_id, user)
    regenerate_video_plan(db, video, new_clips=bool(body and body.new_clips))
    _enqueue_render(db, video)
    db.refresh(video)
    return to_video_out(video)


@router.put("/videos/{video_id}/edit", response_model=VideoOut)
def edit_video(video_id: str, body: VideoEdit, db: DbSession, user: CurrentUser) -> VideoOut:
    video = get_owned_video(db, video_id, user)
    apply_video_edit(db, video, body)
    _enqueue_render(db, video)
    db.refresh(video)
    return to_video_out(video)


@router.get("/videos/{video_id}/download", status_code=status.HTTP_302_FOUND)
def download_video(video_id: str, db: DbSession, user: CurrentUser) -> RedirectResponse:
    video = get_owned_video(db, video_id, user)
    if video.status != VideoStatus.DONE or video.asset is None:
        raise ServiceError("この動画はまだ完成していません。", status_code=409, code="video_not_ready")
    url = get_storage().url(video.asset.storage_key, download=True, filename=download_filename(video.project, video))
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)
