"""System endpoints. ``GET /api/health`` never raises; each check reports a bool."""

from __future__ import annotations

import logging
import shutil
import subprocess
from functools import lru_cache

from fastapi import APIRouter
from sqlalchemy import text

from app.config import get_settings
from app.db import get_engine
from app.schemas import (
    CapabilitiesOut,
    ColorGradeInfo,
    FontInfo,
    HealthOut,
    LimitsInfo,
    LyricStyleInfo,
    ProviderInfo,
    WhisperInfo,
    WorldPresetInfo,
)
from app.security import CurrentUser

logger = logging.getLogger(__name__)

router = APIRouter(tags=["system"])


def check_db() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.warning("health: database check failed: %s", type(exc).__name__)
        return False


def check_redis() -> bool:
    try:
        import redis

        client = redis.Redis.from_url(get_settings().REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
        try:
            return bool(client.ping())
        finally:
            client.close()
    except Exception as exc:
        logger.info("health: redis check failed: %s", type(exc).__name__)
        return False


def check_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@router.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    settings = get_settings()
    db_ok = check_db()
    redis_ok = check_redis()
    ffmpeg_ok = check_ffmpeg()
    # Redis is only required when tasks go through Celery.
    required = [db_ok, ffmpeg_ok] + ([redis_ok] if settings.TASK_MODE == "celery" else [])
    return HealthOut(
        status="ok" if all(required) else "degraded",
        db=db_ok,
        redis=redis_ok,
        ffmpeg=ffmpeg_ok,
        task_mode=settings.TASK_MODE,
        storage_backend=settings.STORAGE_BACKEND,
    )


# Fonts offered for lyrics (fontconfig family names; libass resolves weights per family).
FONT_CHOICES: tuple[tuple[str, str, str], ...] = (
    ("noto_sans_jp", "Noto Sans CJK JP", "ゴシック体（Noto Sans CJK JP）"),
    ("noto_serif_jp", "Noto Serif CJK JP", "明朝体（Noto Serif CJK JP）"),
)


@lru_cache(maxsize=1)
def installed_font_families() -> frozenset[str] | None:
    """Font families known to fontconfig (``None`` when ``fc-list`` is unavailable)."""
    if shutil.which("fc-list") is None:
        return None
    try:
        out = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True, timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return frozenset(name.strip() for line in out.splitlines() for name in line.split(",") if name.strip())


def available_fonts() -> list[FontInfo]:
    installed = installed_font_families()
    return [
        FontInfo(id=font_id, family=family, label=label)
        for font_id, family, label in FONT_CHOICES
        if installed is None or family in installed
    ]


@router.get("/system/capabilities", response_model=CapabilitiesOut)
def capabilities(_user: CurrentUser) -> CapabilitiesOut:
    """Providers (configured/available, models, prices), Claude/Whisper availability, presets and limits.

    Never contains secrets: only booleans and reasons.
    """
    from app.services.lyrics_align import whisper_available
    from app.services.presets import color_grade_infos, lyric_style_infos, preset_infos
    from app.services.providers import provider_info

    settings = get_settings()
    whisper_ok, whisper_reason = whisper_available()
    return CapabilitiesOut(
        providers=[ProviderInfo.model_validate(p) for p in provider_info()],
        claude_configured=settings.anthropic_configured,
        whisper=WhisperInfo(available=whisper_ok, reason=whisper_reason),
        presets=[WorldPresetInfo.model_validate(p) for p in preset_infos()],
        lyric_styles=[LyricStyleInfo.model_validate(s) for s in lyric_style_infos()],
        color_grades=[ColorGradeInfo.model_validate(g) for g in color_grade_infos()],
        fonts=available_fonts(),
        limits=LimitsInfo(max_audio_mb=settings.MAX_AUDIO_MB, max_video_mb=settings.MAX_VIDEO_MB),
    )
