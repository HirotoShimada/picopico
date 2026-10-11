"""Authentication, ownership checks and signed media URLs.

Auth modes (``AUTH_MODE``)
- ``dev``: no token; every request acts as one auto-created ``local`` user.
- ``token``: ``Authorization: Bearer <token>`` required. Users are seeded from
  ``API_TOKENS`` (``name:token,...``) at startup; only the SHA-256 of a token
  is stored.

Signed media URLs are capabilities: ``/media/{key}?exp=..&sig=..`` where
``sig = HMAC-SHA256(SECRET_KEY, key | exp | download | filename)``.
Signed ZIP links (``/media-zip/{filename}?owner=..&videos=..&exp=..&sig=..``) let
a browser stream a ZIP natively, also in token mode: ``sig`` covers the owner,
the sorted video ids, ``exp`` and the file name, and the videos are checked to
still belong to that owner when the archive is streamed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import math
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Sequence
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


# --- signed ZIP links ---------------------------------------------------------------------

ZIP_LINK_PATH = "/media-zip"
_ID_BYTES = 16  # ids are uuid4().hex: 32 hex characters


def encode_video_ids(video_ids: Iterable[str]) -> str:
    """Compact URL form of 32-hex ids (base64url of the raw bytes, 22 chars per id instead of 33)."""
    raw = b"".join(bytes.fromhex(video_id) for video_id in video_ids)
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_video_ids(token: str | None) -> list[str] | None:
    """Inverse of :func:`encode_video_ids`; ``None`` for malformed input."""
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (binascii.Error, ValueError):
        return None
    if not raw or len(raw) % _ID_BYTES:
        return None
    return [raw[i : i + _ID_BYTES].hex() for i in range(0, len(raw), _ID_BYTES)]


def _zip_signature(owner_id: str, video_ids: Iterable[str], exp: int, filename: str) -> str:
    secret = get_settings().SECRET_KEY or ""
    ids = ",".join(sorted(set(video_ids)))
    message = "\n".join(("zip-link", owner_id, ids, str(exp), filename)).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def sign_zip_url(owner_id: str, video_ids: Sequence[str], filename: str, *, ttl: int | None = None) -> tuple[str, int]:
    """Absolute ``/media-zip/{filename}`` URL streaming ``video_ids`` of ``owner_id``; returns ``(url, exp)``.

    The ids travel in the given order (the ZIP keeps it); the signature covers them sorted.
    """
    settings = get_settings()
    exp = int(time.time()) + int(ttl if ttl is not None else settings.MEDIA_URL_TTL)
    params = {
        "owner": owner_id,
        "videos": encode_video_ids(video_ids),
        "exp": str(exp),
        "sig": _zip_signature(owner_id, video_ids, exp, filename),
    }
    url = f"{settings.PUBLIC_API_BASE_URL}{ZIP_LINK_PATH}/{quote(filename, safe='')}?{urlencode(params)}"
    return url, exp


def verify_zip_signature(
    owner_id: str | None,
    video_ids: Sequence[str] | None,
    exp: int | str | None,
    filename: str,
    sig: str | None,
    *,
    now: float | None = None,
) -> bool:
    """True when ``sig`` matches the owner, ids, expiry and file name and ``exp`` is in the future."""
    if not owner_id or not video_ids or not sig or exp is None:
        return False
    try:
        exp_int = int(exp)
    except (TypeError, ValueError):
        return False
    if exp_int < (now if now is not None else time.time()):
        return False
    return hmac.compare_digest(_zip_signature(owner_id, video_ids, exp_int, filename), sig)


# --- rate limiting --------------------------------------------------------------------------


class SlidingWindowLimiter:
    """At most ``limit`` events per ``window_sec`` and key (per process; e.g. paid Claude calls per user)."""

    def __init__(self, limit: int, window_sec: float, *, clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.window_sec = window_sec
        self._clock = clock
        self._events: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def acquire(self, key: str) -> int | None:
        """Record an event and return ``None``, or return the seconds until one is allowed (nothing recorded)."""
        now = self._clock()
        with self._lock:
            events = self._events[key]
            while events and events[0] <= now - self.window_sec:
                events.popleft()
            if len(events) >= self.limit:
                return max(1, math.ceil(events[0] + self.window_sec - now))
            events.append(now)
            return None

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
