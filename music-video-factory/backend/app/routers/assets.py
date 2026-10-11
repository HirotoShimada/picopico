"""Song upload (ARCHITECTURE.md §5.3).

Replacing the audio deletes the previous audio asset and clears the analysis
and selected segment (they describe the old file); lyrics are kept. A new song
un-confirms the ``audio_rights`` checklist item (the confirmation vouched for
the previous file); the response says so in ``warnings``.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, UploadFile, status

from app import worker
from app.config import get_settings
from app.db import DbSession
from app.models import AnalysisStatus, Asset, AssetKind, new_id
from app.pipeline import AUDIO_RIGHTS_RESET_MESSAGE, revoke_right
from app.routers.projects import has_active_batch
from app.schemas import ProjectOut
from app.security import CurrentUser, get_owned_project
from app.serializers import to_project_out
from app.services.media import (
    AUDIO_CONTENT_TYPES,
    AUDIO_EXTENSIONS,
    check_extension,
    sanitize_filename,
    save_upload_file,
    sha256_file,
    validate_audio_upload,
)
from app.storage import get_storage, make_key

logger = logging.getLogger(__name__)

router = APIRouter(tags=["assets"])

ANALYZE_TASK = "analyze"


def new_upload_tmp_path(ext: str) -> Path:
    """Unique scratch path under ``WORK_DIR/uploads`` for an incoming upload."""
    directory = get_settings().WORK_DIR / "uploads"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{uuid.uuid4().hex}{ext}"


@router.post(
    "/projects/{project_id}/audio",
    response_model=ProjectOut,
    status_code=status.HTTP_201_CREATED,
)
def upload_audio(
    project_id: str,
    file: Annotated[UploadFile, File(description="楽曲ファイル（mp3, wav, m4a, aac, flac, ogg）")],
    db: DbSession,
    user: CurrentUser,
) -> ProjectOut:
    settings = get_settings()
    project = get_owned_project(db, project_id, user)
    if has_active_batch(db, project.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="生成中のバッチがあるため音源を差し替えできません。完了またはキャンセル後に再度お試しください。",
        )

    filename = sanitize_filename(file.filename, default="audio")
    ext = check_extension(filename, AUDIO_EXTENSIONS, "音声")
    storage = get_storage()
    tmp = new_upload_tmp_path(ext)
    asset = Asset(
        id=new_id(),
        project_id=project.id,
        owner_id=user.id,
        kind=AssetKind.AUDIO,
        filename=filename,
        content_type=AUDIO_CONTENT_TYPES[ext],
    )
    asset.storage_key = make_key(project.id, AssetKind.AUDIO, asset.id, ext)
    try:
        asset.size_bytes = save_upload_file(file, tmp, settings.max_audio_bytes)
        info = validate_audio_upload(tmp, filename)
        asset.duration = info["duration"]
        asset.meta = {
            "codec": info["codecs"]["audio"],
            "sample_rate": info["sample_rate"],
            "channels": info["channels"],
            "bit_rate": info["bit_rate"],
            "format_name": info["format_name"],
            "sha256": sha256_file(tmp),
        }
        storage.put_file(asset.storage_key, tmp, content_type=asset.content_type, move=True)
    finally:
        tmp.unlink(missing_ok=True)

    old_asset = project.audio_asset
    old_key = old_asset.storage_key if old_asset is not None else None
    try:
        db.add(asset)
        project.audio_asset = asset
        project.analysis = None
        project.analysis_error = None
        project.analysis_status = AnalysisStatus.QUEUED
        project.segment_start = None
        project.segment_end = None
        rights_reset = revoke_right(project, "audio_rights")
        db.flush()
        if old_asset is not None:
            db.delete(old_asset)
        db.commit()
    except Exception:
        db.rollback()
        storage.delete(asset.storage_key)
        raise

    if old_key:
        try:
            storage.delete(old_key)
        except Exception:
            logger.exception("could not delete replaced audio file %s", old_key)

    try:
        worker.enqueue(ANALYZE_TASK, project.id)
    except worker.QueueUnavailable as exc:
        # The upload itself succeeded; report the analysis as failed so it can be re-run.
        project.analysis_status = AnalysisStatus.FAILED
        project.analysis_error = exc.message
        db.commit()
    out = to_project_out(db, project)
    if rights_reset:
        out.warnings = [AUDIO_RIGHTS_RESET_MESSAGE]
    return out
