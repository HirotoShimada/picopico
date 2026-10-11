"""Typed service exceptions (ARCHITECTURE.md §6).

Messages are user-facing Japanese text. ``status_code`` is the HTTP status
the API maps the error to when a router lets it propagate (see the exception
handler in ``app.main``); ``code`` is an optional machine-readable identifier
returned alongside ``detail``.
"""

from __future__ import annotations


class ServiceError(Exception):
    status_code: int = 500
    code: str | None = None

    def __init__(self, message: str = "", *, status_code: int | None = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code

    def __str__(self) -> str:
        return self.message


class MediaValidationError(ServiceError):
    """Uploaded media rejected (400; 413 when too large)."""

    status_code = 400
    code = "media_invalid"


class AnalysisError(ServiceError):
    status_code = 500
    code = "analysis_failed"


class LyricsParseError(ServiceError):
    status_code = 400
    code = "lyrics_invalid"


class WhisperUnavailable(ServiceError):
    status_code = 503
    code = "whisper_unavailable"


class RenderError(ServiceError):
    status_code = 500
    code = "render_failed"

    def __init__(self, message: str = "", stderr_tail: str = "", **kwargs: object):
        super().__init__(message, **kwargs)  # type: ignore[arg-type]
        self.stderr_tail = stderr_tail


class ProviderError(ServiceError):
    """Video provider failure. ``retryable`` marks 429/5xx/timeouts."""

    status_code = 502
    code = "provider_error"

    def __init__(self, message: str = "", *, retryable: bool = False, **kwargs: object):
        super().__init__(message, **kwargs)  # type: ignore[arg-type]
        self.retryable = retryable


class ProviderNotConfigured(ServiceError):
    status_code = 422
    code = "provider_not_configured"


class BudgetExceeded(ServiceError):
    status_code = 422
    code = "budget_exceeded"

    def __init__(
        self,
        message: str = "",
        *,
        estimate_usd: float | None = None,
        limit_usd: float | None = None,
        **kwargs: object,
    ):
        super().__init__(message, **kwargs)  # type: ignore[arg-type]
        self.estimate_usd = estimate_usd
        self.limit_usd = limit_usd
