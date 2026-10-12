"""Application settings (ARCHITECTURE.md §3).

Environment variables are prefix-free and match the field names exactly, e.g.
``DATABASE_URL``.  Values are also read from ``backend/.env`` when present.
Relative directory paths are resolved against the ``backend/`` directory.

Use :func:`get_settings` everywhere (never instantiate ``Settings`` directly) so
the process shares one cached instance; tests call ``get_settings.cache_clear()``
after changing the environment.
"""

from __future__ import annotations

import logging
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parent.parent

_MIB = 1024 * 1024


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- infrastructure ---------------------------------------------------
    DATABASE_URL: str = "postgresql+psycopg://mvf:mvf@127.0.0.1:5432/mvf"
    REDIS_URL: str = "redis://127.0.0.1:6379/0"
    TASK_MODE: Literal["inline", "celery"] = "inline"

    # --- storage ------------------------------------------------------------
    STORAGE_BACKEND: Literal["local", "s3"] = "local"
    STORAGE_DIR: Path = Path("var/storage")
    S3_ENDPOINT_URL: str | None = None
    S3_BUCKET: str | None = None
    S3_ACCESS_KEY_ID: str | None = Field(default=None, repr=False)
    S3_SECRET_ACCESS_KEY: str | None = Field(default=None, repr=False)
    S3_REGION: str | None = None
    WORK_DIR: Path = Path("var/work")

    # --- HTTP -----------------------------------------------------------------
    PUBLIC_API_BASE_URL: str = "http://localhost:8000"
    CORS_ORIGINS: str = "http://localhost:3000"

    # --- auth & signing -------------------------------------------------------
    AUTH_MODE: Literal["dev", "token"] = "dev"
    API_TOKENS: str | None = Field(default=None, repr=False)
    SECRET_KEY: str | None = Field(default=None, repr=False)
    MEDIA_URL_TTL: int = Field(default=3600, ge=60, le=7 * 24 * 3600)

    # --- upload limits --------------------------------------------------------
    MAX_AUDIO_MB: int = Field(default=100, ge=1)
    MAX_VIDEO_MB: int = Field(default=500, ge=1)

    # --- AI services (server-side only; never returned by the API) ----------
    ANTHROPIC_API_KEY: str | None = Field(default=None, repr=False)
    ANTHROPIC_MODEL: str = "claude-opus-5-5"
    # Kling's current API authenticates with KLING_API_KEY; the access/secret key
    # pair is only for the legacy API (docs/VIDEO_PROVIDERS.md §2.4).
    KLING_API_KEY: str | None = Field(default=None, repr=False)
    KLING_ACCESS_KEY: str | None = Field(default=None, repr=False)
    KLING_SECRET_KEY: str | None = Field(default=None, repr=False)
    KLING_API_BASE: str = "https://api-singapore.klingai.com"
    RUNWAY_API_KEY: str | None = Field(default=None, repr=False)
    RUNWAY_API_BASE: str = "https://api.dev.runwayml.com"
    GOOGLE_API_KEY: str | None = Field(default=None, repr=False)
    # OpenRouter (docs/VIDEO_PROVIDERS.md §8): one key for the "openrouter" video provider and,
    # when ANTHROPIC_API_KEY is not set, for Claude storyboards/translation. Never verified against
    # the real API from the dev container (openrouter.ai is unreachable there).
    OPENROUTER_API_KEY: str | None = Field(default=None, repr=False)
    OPENROUTER_API_BASE: str = "https://openrouter.ai/api/v1"
    OPENROUTER_CLAUDE_MODEL: str = "anthropic/claude-opus-5.5"
    # Optional app attribution headers (HTTP-Referer / X-OpenRouter-Title); blank = not sent.
    OPENROUTER_APP_URL: str | None = None
    OPENROUTER_APP_TITLE: str | None = "AI Music Video Factory"
    OPENROUTER_VIDEO_DEFAULT_MODEL: str = "google/veo-3.1-lite"
    # Seconds the GET /videos/models listing is reused (memory + WORK_DIR/cache file) before refetching
    # (min 60: the catalogue is read many times per request).
    OPENROUTER_MODELS_CACHE_TTL: int = Field(default=3600, ge=60)

    # --- generation defaults ----------------------------------------------------
    DEFAULT_BUDGET_USD: float = Field(default=50.0, ge=0)
    WHISPER_MODEL: str = "small"
    FONTS_DIR: Path | None = None

    @field_validator(
        "S3_ENDPOINT_URL",
        "S3_BUCKET",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
        "S3_REGION",
        "API_TOKENS",
        "SECRET_KEY",
        "ANTHROPIC_API_KEY",
        "KLING_API_KEY",
        "KLING_ACCESS_KEY",
        "KLING_SECRET_KEY",
        "RUNWAY_API_KEY",
        "GOOGLE_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENROUTER_APP_URL",
        "OPENROUTER_APP_TITLE",
        "FONTS_DIR",
        mode="before",
    )
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        # `KEY=` in a .env file should mean "unset", not "empty string".
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("STORAGE_DIR", "WORK_DIR", "FONTS_DIR")
    @classmethod
    def _resolve_dir(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        value = value.expanduser()
        if not value.is_absolute():
            value = BACKEND_DIR / value
        return value.resolve()

    @field_validator("PUBLIC_API_BASE_URL", "OPENROUTER_API_BASE")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("OPENROUTER_CLAUDE_MODEL", "OPENROUTER_VIDEO_DEFAULT_MODEL")
    @classmethod
    def _strip_model_id(cls, value: str) -> str:
        return value.strip()

    # --- derived helpers ----------------------------------------------------------
    @property
    def cors_origins(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def max_audio_bytes(self) -> int:
        return self.MAX_AUDIO_MB * _MIB

    @property
    def max_video_bytes(self) -> int:
        return self.MAX_VIDEO_MB * _MIB

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")

    @property
    def anthropic_configured(self) -> bool:
        return bool(self.ANTHROPIC_API_KEY)

    @property
    def openrouter_configured(self) -> bool:
        return bool(self.OPENROUTER_API_KEY)

    @property
    def llm_route(self) -> Literal["anthropic", "openrouter"] | None:
        """How Claude is reached: Anthropic directly first, else OpenRouter, else not at all (template)."""
        if self.anthropic_configured:
            return "anthropic"
        if self.openrouter_configured:
            return "openrouter"
        return None

    @property
    def llm_configured(self) -> bool:
        return self.llm_route is not None

    def ensure_dirs(self) -> None:
        self.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        self.WORK_DIR.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    if not settings.SECRET_KEY:
        # A per-process key keeps dev setups working, but signed media URLs then
        # stop validating after a restart and differ between processes.
        settings.SECRET_KEY = secrets.token_urlsafe(48)
        logger.warning(
            "SECRET_KEY is not set; using a random per-process key. "
            "Set SECRET_KEY in backend/.env so signed media URLs survive restarts."
        )
    settings.ensure_dirs()
    return settings
