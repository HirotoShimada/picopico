"""Authentication, ownership checks and signed media URLs.

Auth modes (``AUTH_MODE``)
- ``dev``: no token; every request acts as one auto-created ``local`` user.
- ``token``: ``Authorization: Bearer <token>`` required. Users are seeded from
  ``API_TOKENS`` (``name:token,...``) at startup; only the SHA-256 of a token
  is stored.

Signed media URLs are capabilities: ``/media/{key}?exp=..&sig=..`` where
``sig = HMAC-SHA256(SECRET_KEY, key | exp | download | filename)``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Annotated
from urllib.parse import quote, urlencode

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import DbSession
from app.models import Batch, Clip, Project, User, Video

logger = logging.getLogger(__name__)

LOCAL_USER_ID = "0" * 31 + "1"
LOCAL_USER_NAME = "local"
MIN_TOKEN_LENGTH = 16

_bearer = HTTPBearer(auto_error=False, description="AUTH_MODE=token のときに必要な API トークン")


# --- users -----------------------------------------------------------------------------


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def parse_api_tokens(raw: str | None) -> list[tuple[str, str]]:
    """Parse ``name:token,name2:token2``; malformed entries are skipped with a warning."""
    entries: list[tuple[str, str]] = []
    for index, item in enumerate((raw or "").split(",")):
        item = item.strip()
        if not item:
            continue
        name, sep, token = item.partition(":")
        name, token = name.strip(), token.strip()
        if not sep or not name or not token:
            logger.warning("API_TOKENS entry #%d is malformed (expected name:token); skipped", index + 1)
            continue
        if len(token) < MIN_TOKEN_LENGTH:
            logger.warning("API token for user %r is shorter than %d characters", name, MIN_TOKEN_LENGTH)
        entries.append((name[:100], token))
    return entries


def seed_users(db: Session) -> list[User]:
    """Create/update token users from ``API_TOKENS`` (idempotent; matched by name)."""
    settings = get_settings()
    entries = parse_api_tokens(settings.API_TOKENS)
    if settings.AUTH_MODE == "token" and not entries:
        logger.warning("AUTH_MODE=token but API_TOKENS is empty: every API request will get 401")
    users: list[User] = []
    for name, token in entries:
        token_hash = hash_token(token)
        user = db.scalar(select(User).where(User.token_hash == token_hash))
        if user is None:
            user = db.scalar(
                select(User).where(User.name == name, User.id != LOCAL_USER_ID).order_by(User.created_at)
            )
            if user is None:
                user = User(name=name, token_hash=token_hash)
                db.add(user)
            else:
                user.token_hash = token_hash  # token rotated: keep the user's projects
        elif user.name != name:
            user.name = name
        users.append(user)
    if settings.AUTH_MODE == "token":
        # A user removed from API_TOKENS loses access (their projects stay; re-adding the name restores them).
        kept = {user.id for user in users}
        for stale in db.scalars(select(User).where(User.token_hash.is_not(None))).all():
            if stale.id not in kept:
                stale.token_hash = None
                logger.warning("revoked the API token of user %r (no longer in API_TOKENS)", stale.name)
    db.commit()
    return users


def get_local_user(db: Session) -> User:
    user = db.get(User, LOCAL_USER_ID)
    if user is not None:
        return user
    try:
        user = User(id=LOCAL_USER_ID, name=LOCAL_USER_NAME, token_hash=None)
        db.add(user)
        db.commit()
    except IntegrityError:
        # Another request created it concurrently.
        db.rollback()
        user = db.get(User, LOCAL_USER_ID)
        if user is None:
            raise
    return user


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    """FastAPI dependency returning the authenticated :class:`User`."""
    settings = get_settings()
    if settings.AUTH_MODE == "dev":
        return get_local_user(db)
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise _unauthorized("認証が必要です。設定画面で API トークンを入力してください。")
    user = db.scalar(select(User).where(User.token_hash == hash_token(credentials.credentials)))
    if user is None:
        raise _unauthorized("API トークンが無効です。")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


# --- ownership --------------------------------------------------------------------------

PROJECT_NOT_FOUND = "プロジェクトが見つかりません。"


def get_owned_project(db: Session, project_id: str, user: User) -> Project:
    project = db.get(Project, project_id)
    if project is None or project.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=PROJECT_NOT_FOUND)
    return project


def get_owned_clip(db: Session, clip_id: str, user: User) -> Clip:
    clip = db.get(Clip, clip_id)
    if clip is None or clip.project.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="素材が見つかりません。")
    return clip


def get_owned_batch(db: Session, batch_id: str, user: User) -> Batch:
    batch = db.get(Batch, batch_id)
    if batch is None or batch.project.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="バッチが見つかりません。")
    return batch


def get_owned_video(db: Session, video_id: str, user: User) -> Video:
    video = db.get(Video, video_id)
    if video is None or video.project.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="動画が見つかりません。")
    return video


# --- signed media URLs --------------------------------------------------------------------


def _signature(key: str, exp: int, download: bool, filename: str | None) -> str:
    secret = get_settings().SECRET_KEY or ""
    message = "\n".join((key, str(exp), "1" if download else "0", filename or "")).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


# Upper bound of the expiry rounding in `sign_media_url` (see there).
_SIGNATURE_WINDOW_MAX_SEC = 300


def sign_media_url(
    key: str,
    *,
    download: bool = False,
    filename: str | None = None,
    ttl: int | None = None,
) -> str:
    """Absolute signed URL ``{PUBLIC_API_BASE_URL}/media/{key}?exp=..&sig=..``.

    ``exp`` is rounded up to a window of ``ttl / 10`` (at most 5 minutes), so repeated
    calls return the *same* URL for a while: the UI polls lists every few seconds, and
    a fresh signature on every response would defeat the browser cache and make every
    thumbnail and video reload. The URL stays valid for at least ``ttl`` seconds.
    """
    settings = get_settings()
    ttl_sec = int(ttl if ttl is not None else settings.MEDIA_URL_TTL)
    window = max(1, min(_SIGNATURE_WINDOW_MAX_SEC, ttl_sec // 10))
    exp = -(-(int(time.time()) + ttl_sec) // window) * window
    params: dict[str, str] = {"exp": str(exp), "sig": _signature(key, exp, download, filename)}
    if download:
        params["download"] = "1"
    if filename:
        params["filename"] = filename
    return f"{settings.PUBLIC_API_BASE_URL}/media/{quote(key, safe='/')}?{urlencode(params, quote_via=quote)}"


def verify_media_signature(
    key: str,
    exp: int | str | None,
    sig: str | None,
    *,
    download: bool = False,
    filename: str | None = None,
    now: float | None = None,
) -> bool:
    """True when ``sig`` matches and ``exp`` is in the future (constant-time compare)."""
    if not sig or exp is None:
        return False
    try:
        exp_int = int(exp)
    except (TypeError, ValueError):
        return False
    if exp_int < (now if now is not None else time.time()):
        return False
    expected = _signature(key, exp_int, download, filename)
    return hmac.compare_digest(expected, sig)
