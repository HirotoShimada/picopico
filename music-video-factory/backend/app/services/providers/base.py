"""Video provider contract and shared adapter machinery (ARCHITECTURE.md §6.7).

``ProviderModel``, ``ClipRequest``, ``ClipResult`` and ``VideoProvider`` are the
contract other modules code against. The contract fields come first in each
dataclass; the extra fields after them have defaults, so positional
construction with the contract fields keeps working.

``ApiVideoProvider`` implements the flow shared by the hosted APIs
(submit -> poll with exponential backoff -> download -> probe):

* errors are :class:`ProviderError` with ``retryable`` set for HTTP 429/5xx,
  timeouts and connection failures, and cleared for other 4xx answers;
* failures of a submitted remote job are :class:`ProviderTaskError` (they
  carry ``external_job_id`` so the pipeline can record the job);
* ``cancel_check()`` is honoured before submitting, between polls and while
  downloading (-> :class:`ProviderCancelled`);
* transient errors *while polling* are tolerated (a paid job keeps running
  server-side), up to ``MAX_CONSECUTIVE_POLL_ERRORS`` in a row.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import random
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal
from urllib.parse import urljoin, urlsplit

import httpx

from app.services.errors import ProviderError, ProviderNotConfigured

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_ASPECT_RATIO",
    "MAX_CONSECUTIVE_POLL_ERRORS",
    "PRICING_AS_OF",
    "ApiVideoProvider",
    "CancelCheck",
    "ClipRequest",
    "ClipResult",
    "PollConfig",
    "PollOutcome",
    "ProgressCallback",
    "ProviderCancelled",
    "ProviderModel",
    "ProviderTaskError",
    "SubmittedJob",
    "VideoProvider",
    "classify_http_status",
    "download_file",
    "error_detail",
    "notify_progress",
    "read_image_base64",
    "safe_path_id",
    "snap_duration",
    "truncate_utf16",
]

DEFAULT_ASPECT_RATIO = "9:16"
# Date of the price research in docs/VIDEO_PROVIDERS.md that the model tables quote.
PRICING_AS_OF = "2026-10-11"
MAX_CONSECUTIVE_POLL_ERRORS = 5
DEFAULT_MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024
_MAX_REDIRECTS = 5
_DOWNLOAD_CHUNK = 1024 * 1024
_DETAIL_MAX_CHARS = 300

ProgressCallback = Callable[[float, str], None]
"""``on_progress(fraction 0..1, stage)``; stages: submitted, queued, running, downloading, done."""
CancelCheck = Callable[[], bool]
"""Returns True when the caller wants the generation abandoned."""


# --- contract dataclasses ------------------------------------------------------------------


@dataclass
class ProviderModel:
    id: str
    name: str
    clip_durations: list[float]  # supported lengths; empty = any length up to max_clip_duration
    max_clip_duration: float
    resolution: str  # output size, e.g. "720x1280"
    supports_vertical: bool
    price_per_second_usd: float
    price_note: str  # Japanese, shown in the UI
    verified: bool  # True only after a real generation with this model succeeded
    # --- compatible additions (defaults keep the contract constructor working) ---
    default_duration: float = 5.0  # clip length used for a 5 s request; drives cost comparisons
    text_to_video: bool = True  # False: image-to-video only (ClipRequest.image_path required)
    image_to_video: bool = False
    source_url: str = ""  # official pricing page
    as_of: str = ""  # date the price was taken from source_url
    params: dict[str, Any] = field(default_factory=dict)  # adapter-specific request settings

    def snap_duration(self, wanted: float) -> float:
        return snap_duration(self.clip_durations, self.max_clip_duration, wanted)

    def cost_for(self, seconds: float) -> float:
        return round(self.price_per_second_usd * max(0.0, seconds), 4)

    def to_info(self) -> dict[str, Any]:
        """``ProviderInfo.models[]`` entry (§5.1) plus the additive fields."""
        return {
            "id": self.id,
            "name": self.name,
            "clip_durations": list(self.clip_durations),
            "max_clip_duration": self.max_clip_duration,
            "resolution": self.resolution,
            "supports_vertical": self.supports_vertical,
            "price_per_second_usd": self.price_per_second_usd,
            "price_note": self.price_note,
            "verified": self.verified,
            "default_duration": self.default_duration,
            "text_to_video": self.text_to_video,
            "image_to_video": self.image_to_video,
            "as_of": self.as_of,
            "source_url": self.source_url,
        }


@dataclass
class ClipRequest:
    prompt: str
    negative_prompt: str
    duration: float
    model: str
    aspect_ratio: str = DEFAULT_ASPECT_RATIO
    seed: int | None = None
    image_path: str | None = None
    scene: dict[str, Any] | None = None  # preset/palette hints for mock


@dataclass
class ClipResult:
    path: str
    duration: float
    width: int
    height: int
    model: str
    cost_usd: float
    external_job_id: str | None
    raw: dict[str, Any]


# --- errors ------------------------------------------------------------------------------------


class ProviderTaskError(ProviderError):
    """A submitted remote job failed, timed out or was cancelled."""

    def __init__(
        self,
        message: str = "",
        *,
        retryable: bool = False,
        external_job_id: str | None = None,
        failure_code: str | None = None,
        **kwargs: Any,
    ):
        super().__init__(message, retryable=retryable, **kwargs)
        self.external_job_id = external_job_id
        self.failure_code = failure_code


class ProviderCancelled(ProviderTaskError):
    """``cancel_check()`` asked to stop (or the job was cancelled remotely)."""

    code = "provider_cancelled"

    def __init__(self, message: str = "生成がキャンセルされました", **kwargs: Any):
        kwargs.setdefault("retryable", False)
        super().__init__(message, **kwargs)


# --- small helpers -------------------------------------------------------------------------------


def snap_duration(durations: Sequence[float], max_duration: float, wanted: float, *, min_duration: float = 1.0) -> float:
    """Nearest supported duration >= ``wanted`` (the longest one when none is long enough).

    With no discrete list, ``wanted`` is clamped to ``[min_duration, max_duration]``.
    """
    try:
        target = float(wanted)
    except (TypeError, ValueError):
        target = 0.0
    if not math.isfinite(target) or target <= 0:
        target = min_duration
    options = sorted({float(d) for d in durations})
    if options:
        for option in options:
            if option >= target - 1e-6:
                return option
        return options[-1]
    return round(min(max(target, min_duration), float(max_duration)), 3)


def truncate_utf16(text: str, max_units: int) -> str:
    """Cut ``text`` to at most ``max_units`` UTF-16 code units (Runway counts this way)."""
    if len(text.encode("utf-16-le")) // 2 <= max_units:
        return text
    out: list[str] = []
    used = 0
    for char in text:
        units = 2 if ord(char) > 0xFFFF else 1
        if used + units > max_units:
            break
        out.append(char)
        used += units
    return "".join(out).rstrip()


def notify_progress(callback: ProgressCallback | None, fraction: float, stage: str) -> None:
    """Best-effort progress report; a failing callback never breaks generation."""
    if callback is None:
        return
    try:
        callback(max(0.0, min(1.0, fraction)), stage)
    except Exception:  # noqa: BLE001 - cancellation has its own channel (cancel_check)
        logger.debug("on_progress callback failed", exc_info=True)


def _cancelled(cancel_check: CancelCheck | None) -> bool:
    if cancel_check is None:
        return False
    try:
        return bool(cancel_check())
    except Exception:  # noqa: BLE001 - a broken check must not look like a cancel request
        logger.warning("cancel_check raised; continuing", exc_info=True)
        return False


_IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def read_image_base64(path: str | os.PathLike[str], *, max_bytes: int, allowed: Sequence[str] = (".png", ".jpg", ".jpeg")) -> tuple[str, str]:
    """``(base64 text, mime type)`` of an input image for image-to-video requests."""
    image = Path(path)
    suffix = image.suffix.lower()
    if suffix not in allowed or suffix not in _IMAGE_MIME:
        raise ProviderError(
            f"入力画像の形式に対応していません（{suffix or '拡張子なし'}）。対応形式: {', '.join(allowed)}",
            retryable=False,
            code="provider_bad_input",
        )
    try:
        size = image.stat().st_size
    except OSError as exc:
        raise ProviderError("入力画像が見つかりません", retryable=False, code="provider_bad_input") from exc
    # base64 grows the payload by 4/3.
    if size * 4 / 3 > max_bytes:
        limit_mb = max_bytes / (1024 * 1024)
        raise ProviderError(
            f"入力画像が大きすぎます（エンコード後 {limit_mb:g}MB まで）", retryable=False, code="provider_bad_input"
        )
    return base64.b64encode(image.read_bytes()).decode("ascii"), _IMAGE_MIME[suffix]


def error_detail(response: httpx.Response) -> str:
    """Human-readable error text from a provider response body (truncated, single line)."""
    detail = ""
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError):
        body = None
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, Mapping):
            detail = str(error.get("message") or error.get("status") or "")
        elif isinstance(error, str):
            detail = error
        if not detail:
            for key in ("message", "detail", "msg", "failure"):
                if isinstance(body.get(key), str) and body[key]:
                    detail = body[key]
                    break
    elif response.content:
        try:
            detail = response.text
        except UnicodeDecodeError:
            detail = ""
    detail = " ".join(detail.split())
    return detail[:_DETAIL_MAX_CHARS] + ("…" if len(detail) > _DETAIL_MAX_CHARS else "")


def classify_http_status(status: int) -> tuple[bool, str, str]:
    """``(retryable, code, Japanese summary)`` for an unsuccessful HTTP status."""
    if status == 429:
        return True, "provider_rate_limited", "リクエストが多すぎます（レート制限・同時実行数の上限）"
    if status in (408, 425) or status >= 500:
        return True, "provider_unavailable", "プロバイダ側で一時的なエラーが発生しました"
    if status == 401:
        return False, "provider_auth_failed", "API キーが無効です"
    if status == 403:
        return False, "provider_auth_failed", "アクセスが拒否されました（API キーの権限・課金設定・利用地域を確認してください）"
    if status == 404:
        return False, "provider_rejected", "リソースが見つかりません"
    return False, "provider_rejected", "リクエストが拒否されました"


def _same_origin(a: str, b: str) -> bool:
    pa, pb = urlsplit(a), urlsplit(b)
    return (pa.scheme, pa.hostname, pa.port) == (pb.scheme, pb.hostname, pb.port)


def download_file(
    client: httpx.Client,
    url: str,
    dest: str | os.PathLike[str],
    *,
    provider_name: str,
    headers: Mapping[str, str] | None = None,
    max_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
    cancel_check: CancelCheck | None = None,
    external_job_id: str | None = None,
) -> int:
    """Stream ``url`` into ``dest`` (atomically) and return the byte count.

    Redirects are followed manually: ``headers`` (credentials) are sent only to
    the origin of ``url``, never to the storage host a redirect points at.
    """
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_name(f".{dest_path.name}.part")
    origin = url
    current = url
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            if urlsplit(current).scheme not in ("https", "http"):
                raise ProviderTaskError(
                    f"{provider_name}: 動画の URL が不正です", retryable=False, external_job_id=external_job_id
                )
            send_headers = dict(headers or {}) if _same_origin(current, origin) else {}
            request = client.build_request("GET", current, headers=send_headers)
            try:
                response = client.send(request, stream=True, follow_redirects=False)
            except httpx.TimeoutException as exc:
                raise ProviderTaskError(
                    f"{provider_name}: 動画のダウンロードがタイムアウトしました",
                    retryable=True, code="provider_timeout", external_job_id=external_job_id,
                ) from exc  # fmt: skip
            except httpx.TransportError as exc:
                raise ProviderTaskError(
                    f"{provider_name}: 動画のダウンロードに失敗しました（接続エラー: {type(exc).__name__}）",
                    retryable=True, code="provider_unreachable", external_job_id=external_job_id,
                ) from exc  # fmt: skip
            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ProviderTaskError(
                            f"{provider_name}: 動画のダウンロードで不正なリダイレクトを受け取りました",
                            retryable=True, external_job_id=external_job_id,
                        )  # fmt: skip
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    response.read()
                    retryable, code, summary = classify_http_status(response.status_code)
                    detail = error_detail(response)
                    raise ProviderTaskError(
                        f"{provider_name}: 動画をダウンロードできませんでした（{summary}、HTTP {response.status_code}"
                        f"{': ' + detail if detail else ''}）",
                        retryable=retryable, code=code, external_job_id=external_job_id,
                    )  # fmt: skip
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise ProviderTaskError(
                        f"{provider_name}: 動画ファイルが大きすぎます（{int(declared) // (1024 * 1024)}MB）",
                        retryable=False, code="provider_bad_output", external_job_id=external_job_id,
                    )  # fmt: skip
                written = 0
                with open(tmp_path, "wb") as handle:
                    for chunk in response.iter_bytes(_DOWNLOAD_CHUNK):
                        written += len(chunk)
                        if written > max_bytes:
                            raise ProviderTaskError(
                                f"{provider_name}: 動画ファイルが大きすぎます（{max_bytes // (1024 * 1024)}MB 超）",
                                retryable=False, code="provider_bad_output", external_job_id=external_job_id,
                            )  # fmt: skip
                        if _cancelled(cancel_check):
                            raise ProviderCancelled(external_job_id=external_job_id)
                        handle.write(chunk)
            except httpx.TimeoutException as exc:
                raise ProviderTaskError(
                    f"{provider_name}: 動画のダウンロードがタイムアウトしました",
                    retryable=True, code="provider_timeout", external_job_id=external_job_id,
                ) from exc  # fmt: skip
            except httpx.TransportError as exc:
                raise ProviderTaskError(
                    f"{provider_name}: 動画のダウンロードが途中で切断されました",
                    retryable=True, code="provider_unreachable", external_job_id=external_job_id,
                ) from exc  # fmt: skip
            finally:
                response.close()
            if written == 0:
                raise ProviderTaskError(
                    f"{provider_name}: ダウンロードした動画が空でした",
                    retryable=True, code="provider_bad_output", external_job_id=external_job_id,
                )  # fmt: skip
            os.replace(tmp_path, dest_path)
            return written
        raise ProviderTaskError(
            f"{provider_name}: 動画のダウンロードでリダイレクトが多すぎます",
            retryable=False, code="provider_bad_output", external_job_id=external_job_id,
        )  # fmt: skip
    finally:
        tmp_path.unlink(missing_ok=True)


def _max_download_bytes() -> int:
    try:
        from app.config import get_settings

        return int(get_settings().max_video_bytes)
    except Exception:  # noqa: BLE001 - settings are optional for this helper
        return DEFAULT_MAX_DOWNLOAD_BYTES


# --- provider base classes ---------------------------------------------------------------------


@dataclass(frozen=True)
class PollConfig:
    """Polling schedule: ``initial`` seconds, times ``factor`` per poll, capped at ``maximum``, ±``jitter``."""

    initial: float = 5.0
    maximum: float = 30.0
    factor: float = 1.5
    jitter: float = 0.2
    minimum: float = 1.0  # never poll faster than this (after jitter)

    def interval(self, attempt: int, rng: random.Random) -> float:
        base = min(self.maximum, self.initial * (self.factor ** max(0, attempt)))
        jittered = base * (1.0 + rng.uniform(-self.jitter, self.jitter))
        return max(self.minimum, jittered)


@dataclass
class SubmittedJob:
    id: str  # what the provider uses to identify the job (stored as Clip.external_job_id)
    raw: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)  # adapter-private state for polling


@dataclass
class PollOutcome:
    state: Literal["queued", "running", "succeeded"]
    progress: float | None = None  # provider-reported 0..1, when available
    video_url: str | None = None
    cost_usd: float | None = None  # actual cost reported by the provider
    raw: dict[str, Any] = field(default_factory=dict)


class VideoProvider(ABC):
    """One video source. Instances are cheap; configuration is read on every call."""

    id: ClassVar[str]
    name: ClassVar[str]
    kind: ClassVar[str]  # "mock" | "library" | "api"
    commercial_use_note: ClassVar[str] = ""
    poll_config: ClassVar[PollConfig] = PollConfig()

    def __init__(
        self,
        *,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        poll: PollConfig | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self.poll = poll or type(self).poll_config
        self._transport = transport
        self._rng = random.Random()

    # -- catalogue ----------------------------------------------------------------------------

    @abstractmethod
    def models(self) -> list[ProviderModel]: ...

    def default_model(self) -> str:
        return self.models()[0].id

    def get_model(self, model: str | None) -> ProviderModel:
        wanted = model or self.default_model()
        for candidate in self.models():
            if candidate.id == wanted:
                return candidate
        raise ProviderError(
            f"{self.name} に「{wanted}」というモデルはありません", retryable=False, code="provider_unknown_model"
        )

    def clip_duration_for(self, model: str, wanted: float) -> float:
        """Nearest supported clip duration >= ``wanted`` for ``model`` (provider default when empty)."""
        return self.get_model(model).snap_duration(wanted)

    # -- readiness ----------------------------------------------------------------------------

    def is_configured(self) -> tuple[bool, str | None]:
        return True, None

    def is_available(self) -> tuple[bool, str | None]:
        """Usable right now (configured and local prerequisites present)."""
        return self.is_configured()

    def model_credentials_problem(self, model: ProviderModel) -> str | None:
        """Why the configured credentials cannot serve ``model`` (providers with per-model keys)."""
        return None

    def check_model(self, model: str | None, *, has_image: bool = False) -> tuple[bool, str | None]:
        """Whether ``model`` can serve a request (configuration, credentials, text/image input)."""
        try:
            entry = self.get_model(model)
        except ProviderError as exc:
            return False, str(exc)
        available, reason = self.is_available()
        if not available:
            return False, reason
        problem = self.model_credentials_problem(entry)
        if problem:
            return False, problem
        if not entry.text_to_video and not has_image:
            return False, (
                f"{entry.name} は画像→動画専用のモデルです。入力画像がないため、テキストだけでは生成できません"
            )
        if has_image and not entry.image_to_video:
            return False, f"{entry.name} は入力画像からの生成に対応していません"
        return True, None

    def require_model(self, model: str | None, *, has_image: bool = False) -> ProviderModel:
        """Like :meth:`check_model` but raising; returns the resolved model.

        Missing credentials -> :class:`ProviderNotConfigured`; an unsuitable
        request (e.g. text-only for an image-only model) -> ``ProviderError``.
        """
        entry = self.get_model(model)
        configured, reason = self.is_configured()
        if not configured:
            raise ProviderNotConfigured(reason or f"{self.name} は設定されていません")
        problem = self.model_credentials_problem(entry)
        if problem:
            raise ProviderNotConfigured(problem)
        ok, reason = self.check_model(entry.id, has_image=has_image)
        if not ok:
            raise ProviderError(reason or f"{entry.name} はこの条件で使用できません", retryable=False, code="provider_bad_input")
        return entry

    def info(self) -> dict[str, Any]:
        """``ProviderInfo`` (§5.1) plus ``default_model``."""
        configured, reason = self.is_configured()
        available, available_reason = self.is_available()
        return {
            "id": self.id,
            "name": self.name,
            "configured": configured,
            "available": available,
            "reason": reason or available_reason,
            "kind": self.kind,
            "commercial_use_note": self.commercial_use_note,
            "default_model": self.default_model(),
            "models": [model.to_info() for model in self.models()],
        }

    # -- generation -----------------------------------------------------------------------------

    @abstractmethod
    def generate(
        self,
        req: ClipRequest,
        out_path: str,
        *,
        timeout: float = 900,
        on_progress: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> ClipResult: ...


class ApiVideoProvider(VideoProvider):
    """Template for hosted APIs: subclasses implement submit / poll / cancel."""

    kind: ClassVar[str] = "api"
    api_timeout: ClassVar[httpx.Timeout] = httpx.Timeout(30.0, connect=10.0)
    download_timeout: ClassVar[httpx.Timeout] = httpx.Timeout(120.0, connect=10.0)
    supported_aspects: ClassVar[tuple[str, ...]] = ("9:16", "16:9")

    # -- hooks ------------------------------------------------------------------------------------

    @abstractmethod
    def _submit(self, client: httpx.Client, req: ClipRequest, model: ProviderModel, duration: float) -> SubmittedJob: ...

    @abstractmethod
    def _poll(self, client: httpx.Client, job: SubmittedJob, model: ProviderModel) -> PollOutcome: ...

    def _cancel_remote(self, client: httpx.Client, job: SubmittedJob) -> None:
        """Best-effort remote cancel; default: the API has no cancel endpoint."""

    def _download_headers(self, url: str) -> dict[str, str]:
        return {}

    # -- HTTP -------------------------------------------------------------------------------------

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.api_timeout, transport=self._transport, follow_redirects=False)

    def _request(self, client: httpx.Client, method: str, url: str, *, what: str, **kwargs: Any) -> httpx.Response:
        """Send a request; non-2xx answers and transport failures become ProviderError."""
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"{self.name}: {what}がタイムアウトしました", retryable=True, code="provider_timeout"
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderError(
                f"{self.name}: {what}で接続エラーが発生しました（{type(exc).__name__}）",
                retryable=True, code="provider_unreachable",
            ) from exc  # fmt: skip
        if not response.is_success:
            raise self._http_error(response, what)
        return response

    def _http_error(self, response: httpx.Response, what: str) -> ProviderError:
        retryable, code, summary = classify_http_status(response.status_code)
        detail = error_detail(response)
        return ProviderError(
            f"{self.name}: {what}に失敗しました（{summary}、HTTP {response.status_code}{': ' + detail if detail else ''}）",
            retryable=retryable,
            code=code,
        )

    def _json(self, response: httpx.Response, what: str) -> dict[str, Any]:
        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderError(
                f"{self.name}: {what}の応答を読み取れませんでした", retryable=True, code="provider_bad_response"
            ) from exc
        if not isinstance(body, dict):
            raise ProviderError(f"{self.name}: {what}の応答形式が想定外です", retryable=True, code="provider_bad_response")
        return body

    # -- flow -------------------------------------------------------------------------------------

    def generate(
        self,
        req: ClipRequest,
        out_path: str,
        *,
        timeout: float = 900,
        on_progress: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> ClipResult:
        model = self.require_model(req.model, has_image=bool(req.image_path))
        if req.aspect_ratio not in self.supported_aspects:
            raise ProviderError(
                f"{self.name} はアスペクト比 {req.aspect_ratio} に対応していません", retryable=False, code="provider_bad_input"
            )
        duration = model.snap_duration(req.duration)
        if _cancelled(cancel_check):
            raise ProviderCancelled()
        deadline = self._clock() + float(timeout)
        with self._client() as client:
            job = self._submit(client, req, model, duration)
            logger.info("%s: submitted job %s (model=%s, %.1fs)", self.id, job.id, model.id, duration)
            notify_progress(on_progress, 0.05, "submitted")
            try:
                outcome = self._wait(client, job, model, deadline=deadline, timeout=float(timeout),
                                     on_progress=on_progress, cancel_check=cancel_check)  # fmt: skip
            except ProviderTaskError as exc:
                if exc.code in ("provider_cancelled", "provider_timeout"):
                    self._try_cancel_remote(client, job)
                raise
            if not outcome.video_url:
                raise ProviderTaskError(
                    f"{self.name}: 生成は完了しましたが動画の URL が返されませんでした",
                    retryable=True, code="provider_bad_response", external_job_id=job.id,
                )  # fmt: skip
            notify_progress(on_progress, 0.9, "downloading")
            with httpx.Client(timeout=self.download_timeout, transport=self._transport, follow_redirects=False) as dl:
                size = download_file(
                    dl, outcome.video_url, out_path, provider_name=self.name,
                    headers=self._download_headers(outcome.video_url), max_bytes=_max_download_bytes(),
                    cancel_check=cancel_check, external_job_id=job.id,
                )  # fmt: skip
        media = self._probe_output(out_path, job.id)
        notify_progress(on_progress, 1.0, "done")
        cost = outcome.cost_usd if outcome.cost_usd is not None else model.cost_for(duration)
        return ClipResult(
            path=str(out_path),
            duration=float(media.get("duration") or duration),
            width=int(media.get("width") or 0),
            height=int(media.get("height") or 0),
            model=model.id,
            cost_usd=round(cost, 4),
            external_job_id=job.id,
            raw={
                "provider": self.id,
                "model": model.id,
                "requested_duration": duration,
                "billed_duration": duration,
                "bytes": size,
                "submit": job.raw,
                "final": outcome.raw,
            },
        )

    def _wait(
        self,
        client: httpx.Client,
        job: SubmittedJob,
        model: ProviderModel,
        *,
        deadline: float,
        timeout: float,
        on_progress: ProgressCallback | None,
        cancel_check: CancelCheck | None,
    ) -> PollOutcome:
        attempt = 0
        errors = 0
        while True:
            if _cancelled(cancel_check):
                raise ProviderCancelled(external_job_id=job.id)
            try:
                outcome = self._poll(client, job, model)
                errors = 0
            except ProviderTaskError:
                raise
            except ProviderError as exc:
                if not exc.retryable:
                    raise ProviderTaskError(
                        str(exc), retryable=False, code=exc.code, external_job_id=job.id
                    ) from exc
                errors += 1
                logger.warning("%s: polling job %s failed (%d in a row): %s", self.id, job.id, errors, exc)
                if errors >= MAX_CONSECUTIVE_POLL_ERRORS:
                    raise ProviderTaskError(
                        str(exc), retryable=True, code=exc.code, external_job_id=job.id
                    ) from exc
                outcome = None
            if outcome is not None:
                if outcome.state == "succeeded":
                    return outcome
                if outcome.progress is not None:
                    notify_progress(on_progress, 0.05 + 0.85 * max(0.0, min(1.0, outcome.progress)), outcome.state)
                else:
                    notify_progress(on_progress, 0.05, outcome.state)
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise ProviderTaskError(
                    f"{self.name}: 生成が {timeout:.0f} 秒以内に完了しませんでした",
                    retryable=True, code="provider_timeout", external_job_id=job.id,
                )  # fmt: skip
            self._sleep(min(self.poll.interval(attempt, self._rng), remaining))
            attempt += 1

    def _try_cancel_remote(self, client: httpx.Client, job: SubmittedJob) -> None:
        try:
            self._cancel_remote(client, job)
        except Exception:  # noqa: BLE001 - best effort; the original error matters more
            logger.warning("%s: could not cancel remote job %s", self.id, job.id, exc_info=True)

    def _probe_output(self, path: str, job_id: str) -> dict[str, Any]:
        from app.services.errors import ServiceError
        from app.services.media import probe

        try:
            media = probe(path)
        except ServiceError as exc:
            Path(path).unlink(missing_ok=True)
            raise ProviderTaskError(
                f"{self.name}: ダウンロードした動画を読み取れませんでした（ファイルが壊れている可能性があります）",
                retryable=True, code="provider_bad_output", external_job_id=job_id,
            ) from exc  # fmt: skip
        if not media.get("has_video"):
            Path(path).unlink(missing_ok=True)
            raise ProviderTaskError(
                f"{self.name}: ダウンロードしたファイルに映像が含まれていません",
                retryable=True, code="provider_bad_output", external_job_id=job_id,
            )  # fmt: skip
        return media


_SAFE_ID = re.compile(r"^[A-Za-z0-9._:/=-]{1,512}$")


def safe_path_id(value: object, *, provider_name: str, what: str = "ジョブ ID") -> str:
    """Validate an id the provider returned before putting it into a URL path."""
    text = str(value or "")
    if not _SAFE_ID.match(text) or ".." in text or text.startswith("/"):
        raise ProviderError(f"{provider_name}: {what}の形式が想定外です", retryable=False, code="provider_bad_response")
    return text
