"""Signed media delivery: ``GET /media/{key}?exp=&sig=[&download=1&filename=]``.

Not under ``/api`` and needs no auth header: the HMAC signature is the
capability (see ``app.security.sign_media_url``). Local storage only; S3
deployments hand out presigned URLs instead. HTTP Range requests (video
seeking) are served by Starlette's ``FileResponse`` (206 / 416).
"""

from __future__ import annotations

import mimetypes
import time
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import FileResponse

from app.security import verify_media_signature
from app.storage import StorageKeyError, build_content_disposition, get_storage

router = APIRouter(tags=["media"])

_EXTRA_TYPES = {".m4a": "audio/mp4", ".m4v": "video/x-m4v", ".webm": "video/webm", ".flac": "audio/flac"}


def _media_type(name: str) -> str:
    suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _EXTRA_TYPES.get(suffix) or mimetypes.guess_type(name)[0] or "application/octet-stream"


@router.api_route("/media/{key:path}", methods=["GET", "HEAD"], include_in_schema=False)
def get_media(
    key: str,
    exp: Annotated[int | None, Query()] = None,
    sig: Annotated[str | None, Query(max_length=128)] = None,
    download: Annotated[bool, Query()] = False,
    filename: Annotated[str | None, Query(max_length=255)] = None,
) -> FileResponse:
    if not verify_media_signature(key, exp, sig, download=download, filename=filename):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="URL の有効期限が切れているか、署名が無効です。ページを再読み込みしてください。",
        )
    not_found = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ファイルが見つかりません。")
    try:
        path = get_storage().local_path(key)
    except StorageKeyError:
        raise not_found from None
    if path is None or not path.is_file():
        raise not_found

    headers = {
        "Cache-Control": f"private, max-age={max(0, min(int(exp or 0) - int(time.time()), 86400))}",
        "X-Content-Type-Options": "nosniff",
    }
    if download:
        headers["Content-Disposition"] = build_content_disposition(filename or path.name)
    return FileResponse(path, media_type=_media_type(path.name), headers=headers)
