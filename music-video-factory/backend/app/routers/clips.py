"""Video material pool: list, upload, delete (ARCHITECTURE.md §5.5).

Uploads are all-or-nothing: every file is validated (and thumbnailed) before
anything is stored, so a single bad file rejects the request without side
effects.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import select

from app.config import get_settings
from app.db import DbSession
from app.models import (
    ACTIVE_VIDEO_STATUSES,
    Asset,
    AssetKind,
    Clip,
    ClipSource,
    ClipStatus,
    Video,
    new_id,
    utcnow,
)
from app.routers.assets import new_upload_tmp_path
from app.schemas import ClipOut
from app.security import CurrentUser, get_owned_clip, get_owned_project
from app.serializers import to_clip_outs, video_clip_ids
from app.services.errors import MediaValidationError
from app.services.media import (
    VIDEO_CONTENT_TYPES,
    VIDEO_EXTENSIONS,
    check_extension,
    make_thumbnail,
    sanitize_filename,
    save_upload_file,
    sha256_file,
    validate_video_upload,
)
from app.storage import get_storage, make_key

logger = logging.getLogger(__name__)

router = APIRouter(tags=["clips"])

MAX_FILES_PER_UPLOAD = 20
UPLOAD_PROVIDER = "upload"


@dataclass
class _StagedClip:
    filename: str
    ext: str
    video_path: Path
    thumb_path: Path
    size: int
    info: dict[str, Any]
    sha256: str


@router.get("/projects/{project_id}/clips", response_model=list[ClipOut])
def list_clips(
    project_id: str,
    db: DbSession,
    user: CurrentUser,
    include_cancelled: Annotated[bool, Query(description="false: キャンセルされた素材を除く")] = True,
) -> list[ClipOut]:
    project = get_owned_project(db, project_id, user)
    stmt = select(Clip).where(Clip.project_id == project.id).order_by(Clip.created_at, Clip.id)
    if not include_cancelled:
        stmt = stmt.where(Clip.status != ClipStatus.CANCELLED)
    return to_clip_outs(db, db.scalars(stmt).all())


def _stage(upload: UploadFile, max_bytes: int, temp_paths: list[Path]) -> _StagedClip:
    filename = sanitize_filename(upload.filename, default="clip")
    try:
        ext = check_extension(filename, VIDEO_EXTENSIONS, "動画")
        video_path = new_upload_tmp_path(ext)
        thumb_path = video_path.with_suffix(".jpg")
        temp_paths += [video_path, thumb_path]
        size = save_upload_file(upload, video_path, max_bytes)
        info = validate_video_upload(video_path, filename)
        make_thumbnail(video_path, thumb_path, at=min(1.0, (info["duration"] or 0.0) / 2))
    except MediaValidationError as exc:
        raise MediaValidationError(f"{filename}: {exc.message}", status_code=exc.status_code, code=exc.code) from exc
    return _StagedClip(filename, ext, video_path, thumb_path, size, info, sha256_file(video_path))


@router.post(
    "/projects/{project_id}/clips/upload",
    response_model=list[ClipOut],
    status_code=status.HTTP_201_CREATED,
)
def upload_clips(
    project_id: str,
    files: Annotated[list[UploadFile], File(description="動画素材（mp4, mov, webm, m4v）を1〜20本")],
    db: DbSession,
    user: CurrentUser,
) -> list[ClipOut]:
    settings = get_settings()
    project = get_owned_project(db, project_id, user)
    if not 1 <= len(files) <= MAX_FILES_PER_UPLOAD:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"一度にアップロードできる動画は1〜{MAX_FILES_PER_UPLOAD}本です。",
        )

    storage = get_storage()
    temp_paths: list[Path] = []
    stored_keys: list[str] = []
    try:
        staged = [_stage(upload, settings.max_video_bytes, temp_paths) for upload in files]
        clips: list[Clip] = []
        for item in staged:
            video_asset = Asset(
                id=new_id(),
                project_id=project.id,
                owner_id=user.id,
                kind=AssetKind.CLIP,
                filename=item.filename,
                content_type=VIDEO_CONTENT_TYPES[item.ext],
                size_bytes=item.size,
                duration=item.info["duration"],
                width=item.info["width"],
                height=item.info["height"],
                meta={
                    "codecs": item.info["codecs"],
                    "fps": item.info["fps"],
                    "rotation": item.info["rotation"],
                    "has_audio": item.info["has_audio"],
                    "format_name": item.info["format_name"],
                    "sha256": item.sha256,
                },
            )
            video_asset.storage_key = make_key(project.id, AssetKind.CLIP, video_asset.id, item.ext)
            thumb_asset = Asset(
                id=new_id(),
                project_id=project.id,
                owner_id=user.id,
                kind=AssetKind.THUMBNAIL,
                filename=f"{Path(item.filename).stem}.jpg",
                content_type="image/jpeg",
            )
            thumb_asset.storage_key = make_key(project.id, AssetKind.THUMBNAIL, thumb_asset.id, ".jpg")

            video_asset.size_bytes = storage.put_file(
                video_asset.storage_key, item.video_path, content_type=video_asset.content_type, move=True
            )
            stored_keys.append(video_asset.storage_key)
            thumb_asset.size_bytes = storage.put_file(
                thumb_asset.storage_key, item.thumb_path, content_type="image/jpeg", move=True
            )
            stored_keys.append(thumb_asset.storage_key)

            clip = Clip(
                project_id=project.id,
                source=ClipSource.UPLOAD,
                provider=UPLOAD_PROVIDER,
                status=ClipStatus.READY,
                asset=video_asset,
                thumbnail_asset=thumb_asset,
                duration=item.info["duration"],
                width=item.info["width"],
                height=item.info["height"],
                params={"original_filename": item.filename},
            )
            db.add_all([video_asset, thumb_asset, clip])
            clips.append(clip)
        project.updated_at = utcnow()
        db.commit()
    except Exception:
        db.rollback()
        for key in stored_keys:
            try:
                storage.delete(key)
            except Exception:
                logger.exception("cleanup of %s failed", key)
        raise
    finally:
        for path in temp_paths:
            path.unlink(missing_ok=True)

    return to_clip_outs(db, clips)


@router.delete("/clips/{clip_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_clip(clip_id: str, db: DbSession, user: CurrentUser) -> Response:
    clip = get_owned_clip(db, clip_id, user)
    if clip.status == ClipStatus.GENERATING:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="生成中の素材は削除できません。")
    active_plans = db.scalars(
        select(Video.plan).where(Video.project_id == clip.project_id, Video.status.in_(ACTIVE_VIDEO_STATUSES))
    )
    if any(clip.id in video_clip_ids(plan) for plan in active_plans):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="この素材を使う動画がレンダリング待ち・レンダリング中のため削除できません。",
        )

    assets = [a for a in (clip.asset, clip.thumbnail_asset) if a is not None]
    keys = [a.storage_key for a in assets]
    db.delete(clip)
    db.flush()
    for asset in assets:
        db.delete(asset)
    db.commit()

    storage = get_storage()
    for key in keys:
        try:
            storage.delete(key)
        except Exception:
            logger.exception("clip %s deleted but file %s could not be removed", clip_id, key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
