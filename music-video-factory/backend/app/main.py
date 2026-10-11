"""FastAPI application factory.

Run: ``cd backend && .venv/bin/uvicorn app.main:app --port 8000``
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app import __version__, pipeline, worker
from app.config import Settings, get_settings
from app.db import SessionLocal, init_db
from app.routers import (
    analysis,
    assets,
    clips,
    dashboard,
    generation,
    lyrics,
    media,
    projects,
    system,
    videos,
)
from app.security import seed_users
from app.services.errors import ServiceError
from app.storage import StorageNotFound

logger = logging.getLogger("app")

API_ROUTERS = (system, projects, assets, clips, analysis, lyrics, generation, videos, dashboard)

_MIB = 1024 * 1024
_MULTIPART_OVERHEAD = _MIB

# Upload endpoints whose request body is capped *before* it is read (Content-Length
# check). Per-file limits are additionally enforced while copying each file.
_UPLOAD_LIMITS: tuple[tuple[re.Pattern[str], Callable[[Settings], int]], ...] = (
    (re.compile(r"^/api/projects/[^/]+/audio/?$"), lambda s: s.max_audio_bytes + _MULTIPART_OVERHEAD),
    (
        re.compile(r"^/api/projects/[^/]+/clips/upload/?$"),
        lambda s: s.max_video_bytes * clips.MAX_FILES_PER_UPLOAD + _MULTIPART_OVERHEAD,
    ),
    (re.compile(r"^/api/projects/[^/]+/lyrics/upload/?$"), lambda s: 2 * _MIB),
)


class UploadSizeLimitMiddleware:
    """Reject oversized upload requests with 413 based on ``Content-Length``."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in ("POST", "PUT"):
            limit = self._limit_for(scope["path"])
            if limit is not None:
                length = self._content_length(scope)
                if length is not None and length > limit:
                    await self._reject(send, limit)
                    return
        await self.app(scope, receive, send)

    @staticmethod
    def _limit_for(path: str) -> int | None:
        settings = get_settings()
        for pattern, limit in _UPLOAD_LIMITS:
            if pattern.match(path):
                return limit(settings)
        return None

    @staticmethod
    def _content_length(scope: Scope) -> int | None:
        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    return int(value)
                except ValueError:
                    return None
        return None

    @staticmethod
    async def _reject(send: Send, limit: int) -> None:
        body = json.dumps(
            {"detail": f"アップロードサイズが上限（{limit // _MIB} MB）を超えています。", "code": "file_too_large"},
            ensure_ascii=False,
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    init_db()
    with SessionLocal() as db:
        seed_users(db)
    if settings.TASK_MODE == "inline":
        # Inline tasks die with the process; resume what a previous run left unfinished.
        try:
            pipeline.recover_interrupted_jobs()
        except Exception:
            logger.exception("could not resume interrupted jobs")
    logger.info(
        "AI Music Video Factory API ready (auth=%s, tasks=%s, storage=%s)",
        settings.AUTH_MODE,
        settings.TASK_MODE,
        settings.STORAGE_BACKEND,
    )
    try:
        yield
    finally:
        worker.shutdown(wait=False)


async def _service_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ServiceError)
    if exc.status_code == 503:
        logger.warning("service unavailable: %s", exc)
    elif exc.status_code >= 500:
        logger.error("service error: %s", exc, exc_info=exc)
    content: dict[str, object] = {"detail": exc.message or "処理中にエラーが発生しました。"}
    if exc.code:
        content["code"] = exc.code
    return JSONResponse(status_code=exc.status_code, content=content)


async def _storage_not_found_handler(_request: Request, exc: Exception) -> JSONResponse:
    logger.warning("stored file missing: %s", exc)
    return JSONResponse(status_code=404, content={"detail": "ファイルが見つかりません。"})


def _configure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger.setLevel(logging.INFO)


def create_app() -> FastAPI:
    _configure_logging()
    settings = get_settings()
    application = FastAPI(
        title="AI Music Video Factory API",
        version=__version__,
        lifespan=lifespan,
    )
    # Middleware added last runs first: CORS wraps the size limiter so 413s carry CORS headers.
    application.add_middleware(UploadSizeLimitMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition", "Content-Range", "Accept-Ranges", "Content-Length"],
    )
    application.add_exception_handler(ServiceError, _service_error_handler)
    application.add_exception_handler(StorageNotFound, _storage_not_found_handler)

    for module in API_ROUTERS:
        application.include_router(module.router, prefix="/api")
    application.include_router(media.router)
    return application


app = create_app()
