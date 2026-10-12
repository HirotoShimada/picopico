"""OpenRouter basics shared by the video provider and the Claude route (docs/VIDEO_PROVIDERS.md §8).

OpenRouter (``OPENROUTER_API_BASE``, default ``https://openrouter.ai/api/v1``)
relays requests to the models of many vendors with one key. This app uses it
for video generation (``providers/openrouter.py``) and — when
``ANTHROPIC_API_KEY`` is not set — for Claude storyboards and translation
(:class:`OpenRouterChatClient`, ``POST /chat/completions``).

The wire format mirrors the official SDKs (npm ``@openrouter/sdk`` 1.4.25 and
PyPI ``openrouter`` 1.3.43, read for the contract but not used as a
dependency: the Python SDK pins ``pydantic<2.13`` and retries ``POST /videos``
by itself, §8.7). **Nothing here has been verified against the real API**:
openrouter.ai is unreachable from the dev container, so every behaviour is
tested with respx only.

Claude requests always carry ``provider: {data_collection: "deny", zdr: true,
require_parameters: true}`` (only endpoints that neither collect nor retain
prompts — the lyrics — and support every parameter sent). The video API has
no such fields (§8.5), see ``providers/openrouter.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "CHAT_PROVIDER_PREFERENCES",
    "ROUTE_OPENROUTER",
    "ChatReply",
    "OpenRouterChatClient",
    "OpenRouterChatError",
    "api_base",
    "end_user_id",
    "error_message",
    "request_headers",
    "same_origin",
]

ROUTE_OPENROUTER = "openrouter"
# [A] ProviderPreferences (chat): data_collection "deny" = only providers that do not collect user data;
# zdr = only zero-data-retention endpoints; require_parameters = only providers supporting every parameter
# sent (response_format json_schema, reasoning). Sent on every Claude request (user decision).
CHAT_PROVIDER_PREFERENCES: dict[str, Any] = {"data_collection": "deny", "zdr": True, "require_parameters": True}
# Errors that mean "no endpoint satisfies the routing requirements" (ZDR / data policy) [B]/[C].
_NO_PROVIDER = re.compile(
    r"no (?:allowed |available |eligible )?(?:endpoints?|providers?)|routing requirement|data[ _-]?polic"
    r"|zero[ -]data[ -]retention|\bzdr\b|data_collection|privacy setting",
    re.IGNORECASE,
)
_RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 524, 529})
_MAX_RETRY_AFTER_SEC = 20.0
_DETAIL_MAX_CHARS = 300


def api_base(settings: Settings | None = None) -> str:
    return (settings or get_settings()).OPENROUTER_API_BASE.rstrip("/")


def same_origin(a: str, b: str) -> bool:
    """Same scheme, host and port (credentials only ever go to the API origin)."""
    pa, pb = urlsplit(a), urlsplit(b)
    return (pa.scheme, (pa.hostname or "").lower(), pa.port) == (pb.scheme, (pb.hostname or "").lower(), pb.port)


def request_headers(settings: Settings | None = None, *, accept: str = "application/json") -> dict[str, str]:
    """``Authorization: Bearer`` plus the optional app attribution headers [A] (lib/security.js, funcs/*)."""
    settings = settings or get_settings()
    headers = {"Accept": accept}
    if settings.OPENROUTER_API_KEY:
        headers["Authorization"] = f"Bearer {settings.OPENROUTER_API_KEY}"
    title = (settings.OPENROUTER_APP_TITLE or "").strip()
    if title and title.isascii():  # header values must be ASCII
        headers["X-OpenRouter-Title"] = title
    referer = (settings.OPENROUTER_APP_URL or "").strip()
    if referer and referer.isascii():
        headers["HTTP-Referer"] = referer
    return headers


def end_user_id(owner_id: str | None, settings: Settings | None = None) -> str | None:
    """Stable pseudonymous ``user`` for OpenRouter (HMAC of the owner id; the raw id never leaves).

    OpenRouter uses it to scope provider-side blocks to one end user instead of the whole account [A].
    """
    if not owner_id:
        return None
    key = ((settings or get_settings()).SECRET_KEY or "").encode("utf-8")
    digest = hmac.new(key, f"openrouter-user\n{owner_id}".encode(), hashlib.sha256).hexdigest()
    return f"mvf-{digest[:32]}"


def error_message(response: httpx.Response) -> str:
    """``error.message`` of an OpenRouter error body ``{"error": {"code", "message", "metadata"}}`` [A]."""
    detail = ""
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError):
        body = None
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, Mapping):
            detail = str(error.get("message") or "")
            metadata = error.get("metadata")
            if isinstance(metadata, Mapping) and isinstance(metadata.get("reasons"), list):
                detail = f"{detail} ({', '.join(str(r) for r in metadata['reasons'][:5])})"
        elif isinstance(error, str):
            detail = error
        elif isinstance(body.get("message"), str):
            detail = body["message"]
    elif response.content:
        try:
            detail = response.text
        except UnicodeDecodeError:
            detail = ""
    detail = " ".join(detail.split())
    return detail[:_DETAIL_MAX_CHARS] + ("…" if len(detail) > _DETAIL_MAX_CHARS else "")


def no_provider_error(status: int, message: str) -> bool:
    """404/503 because no endpoint meets the routing requirements (ZDR / data collection)."""
    return status in (404, 503) and bool(_NO_PROVIDER.search(message or ""))


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _int(value: Any) -> int:
    number = _num(value)
    return max(0, int(number)) if number is not None else 0


def _retry_after(response: httpx.Response) -> float | None:
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        value = response.headers.get(name)
        if value:
            try:
                return max(0.0, min(_MAX_RETRY_AFTER_SEC, float(value) * scale))
            except ValueError:
                continue
    return None


# --- chat completions (Claude route) -----------------------------------------------------------------


@dataclass
class ChatReply:
    """One ``/chat/completions`` answer, normalised.

    ``usage`` uses the Anthropic Messages key names (``input_tokens`` = uncached
    prompt tokens, ``cache_read_input_tokens``, ``cache_creation_input_tokens``,
    ``output_tokens``) so the storyboard code books it like a direct call;
    ``cost_usd`` is OpenRouter's ``usage.cost`` (USD charged) when reported.
    """

    text: str | None
    model: str
    finish_reason: str | None
    refusal: str | None
    usage: dict[str, int] | None
    cost_usd: float | None
    is_byok: bool | None = None
    upstream_cost_usd: float | None = None  # cost_details.upstream_inference_cost (BYOK) [A]/[B]

    @property
    def refused(self) -> bool:
        return bool(self.refusal) or self.finish_reason == "content_filter"


class OpenRouterChatError(Exception):
    """A chat request failed before producing an answer.

    ``kind``: ``bad_request`` (400/413/422), ``auth`` (401), ``credits`` (402),
    ``moderation`` (403), ``no_provider`` (404/503: no endpoint satisfies ZDR /
    data collection), ``rate_limited`` (429), ``http_error`` (other statuses),
    ``connection`` (network/timeout), ``bad_response`` (unreadable body).
    """

    def __init__(self, kind: str, message: str = "", *, status: int | None = None):
        super().__init__(message or kind)
        self.kind = kind
        self.status = status
        self.message = message


def _classify_chat_status(status: int, message: str) -> str:
    if status == 401:
        return "auth"
    if status == 402:
        return "credits"
    if status == 403:
        return "moderation"
    if no_provider_error(status, message):
        return "no_provider"
    if status == 429:
        return "rate_limited"
    if status in (400, 413, 422):
        return "bad_request"
    return "http_error"


def _message_text(content: Any) -> str | None:
    """``message.content`` is a string, a list of content parts or null [A] (chatassistantmessage)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [str(part.get("text") or "") for part in content
                 if isinstance(part, Mapping) and part.get("type", "text") == "text"]  # fmt: skip
        return "".join(parts) if parts else None
    return None


def _normalise_usage(usage: Any) -> tuple[dict[str, int] | None, float | None, bool | None, float | None]:
    """``(Anthropic-style usage, usage.cost, is_byok, upstream cost)`` from a ChatUsage object [A] (chatusage.js).

    Assumption [C]: ``prompt_tokens`` includes the cached and cache-write tokens (OpenAI convention).
    The upstream cost is ``cost_details.upstream_inference_cost`` [A] (costdetails.js), which is said to
    be meaningful for BYOK only [B].
    """
    if not isinstance(usage, Mapping):
        return None, None, None, None
    raw_details = usage.get("prompt_tokens_details")
    details: Mapping[str, Any] = raw_details if isinstance(raw_details, Mapping) else {}
    prompt = _int(usage.get("prompt_tokens"))
    cached = _int(details.get("cached_tokens"))
    written = _int(details.get("cache_write_tokens"))
    normalised = {
        "input_tokens": max(0, prompt - cached - written),
        "output_tokens": _int(usage.get("completion_tokens")),
        "cache_creation_input_tokens": written,
        "cache_read_input_tokens": cached,
    }
    cost = _num(usage.get("cost"))
    byok = usage.get("is_byok") if isinstance(usage.get("is_byok"), bool) else None
    raw_cost_details = usage.get("cost_details")
    upstream = _num(raw_cost_details.get("upstream_inference_cost")) if isinstance(raw_cost_details, Mapping) else None
    return (normalised, (max(0.0, cost) if cost is not None else None), byok,
            (max(0.0, upstream) if upstream is not None else None))  # fmt: skip


class OpenRouterChatClient:
    """Minimal client for ``POST /chat/completions`` with Claude (httpx; respx-tested only).

    Retries 408/429/5xx/524/529 (not "no provider" 503s) and connection
    errors up to ``max_retries`` times — like the Anthropic SDK client used on
    the direct route — honouring ``Retry-After`` (capped at 20 s).
    """

    route = ROUTE_OPENROUTER

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        model: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 300.0,
        max_retries: int = 2,
        sleep: Callable[[float], None] | None = None,
    ):
        self._settings = settings or get_settings()
        self.model = (model or self._settings.OPENROUTER_CLAUDE_MODEL).strip()
        self._transport = transport
        self._timeout = httpx.Timeout(timeout, connect=10.0)
        self._max_retries = max(0, int(max_retries))
        self._sleep = sleep or time.sleep

    def build_body(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
        json_schema: Mapping[str, Any] | None = None,
        schema_name: str = "response",
        cache_system: bool = False,
        end_user: str | None = None,
    ) -> dict[str, Any]:
        """Request body [A] (chatrequest.js outbound; field names are the wire's snake_case)."""
        system_content: Any = system
        if cache_system:
            # Content-block cache breakpoint on the stable system prompt [A] (chatcontenttext / cache_control).
            system_content = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system_content}, {"role": "user", "content": user}],
            "max_completion_tokens": int(max_tokens),
            # Opus 5.5 cannot disable thinking, so never "none"/"minimal" [C]; "low" like the direct route.
            "reasoning": {"effort": "low"},
            "provider": dict(CHAT_PROVIDER_PREFERENCES),
            "stream": False,
        }
        if json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": dict(json_schema)},
            }
        pseudonym = end_user_id(end_user, self._settings)
        if pseudonym:
            body["user"] = pseudonym
        return body

    def chat(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
        json_schema: Mapping[str, Any] | None = None,
        schema_name: str = "response",
        cache_system: bool = False,
        end_user: str | None = None,
    ) -> ChatReply:
        if not self._settings.OPENROUTER_API_KEY:
            raise OpenRouterChatError("auth", "OPENROUTER_API_KEY が未設定です")
        body = self.build_body(system=system, user=user, max_tokens=max_tokens, json_schema=json_schema,
                               schema_name=schema_name, cache_system=cache_system, end_user=end_user)  # fmt: skip
        url = f"{api_base(self._settings)}/chat/completions"
        headers = request_headers(self._settings)
        attempt = 0
        with httpx.Client(timeout=self._timeout, transport=self._transport, follow_redirects=False) as client:
            while True:
                try:
                    response = client.post(url, json=body, headers=headers)
                except httpx.ReadTimeout as exc:  # the request may still be running (and billed): no retry
                    raise OpenRouterChatError("connection", f"応答がタイムアウトしました（{type(exc).__name__}）") from exc
                except httpx.TransportError as exc:
                    if attempt < self._max_retries:
                        attempt += 1
                        self._sleep(float(2 ** (attempt - 1)))
                        continue
                    raise OpenRouterChatError("connection", f"接続エラー（{type(exc).__name__}）") from exc
                if response.is_success:
                    return self._parse(response)
                message = error_message(response)
                kind = _classify_chat_status(response.status_code, message)
                if response.status_code in _RETRYABLE_STATUSES and kind != "no_provider" and attempt < self._max_retries:
                    attempt += 1
                    wait = _retry_after(response)
                    self._sleep(wait if wait is not None else float(2 ** (attempt - 1)))
                    continue
                raise OpenRouterChatError(kind, message, status=response.status_code)

    def _parse(self, response: httpx.Response) -> ChatReply:
        """ChatResult [A] (chatresult.js, chatchoice.js, chatassistantmessage.js, chatusage.js)."""
        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise OpenRouterChatError("bad_response", "応答を読み取れませんでした", status=response.status_code) from exc
        if not isinstance(body, Mapping):
            raise OpenRouterChatError("bad_response", "応答の形式が想定外です", status=response.status_code)
        usage, cost, byok, upstream = _normalise_usage(body.get("usage"))
        model = str(body.get("model") or self.model)[:100]  # CostRecord.model is String(100)
        raw_choices = body.get("choices")
        choices: list[Any] = raw_choices if isinstance(raw_choices, list) else []
        choice = choices[0] if choices and isinstance(choices[0], Mapping) else None
        if choice is None:
            # No answer at all (e.g. an upstream error reported in a 200): billed tokens are still booked.
            return ChatReply(None, model, "error", None, usage, cost, byok, upstream)
        raw_message = choice.get("message")
        message: Mapping[str, Any] = raw_message if isinstance(raw_message, Mapping) else {}
        refusal = message.get("refusal")
        finish = choice.get("finish_reason")
        return ChatReply(
            text=_message_text(message.get("content")),
            model=model,
            finish_reason=str(finish) if isinstance(finish, str) else None,
            refusal=str(refusal) if isinstance(refusal, str) and refusal.strip() else None,
            usage=usage,
            cost_usd=cost,
            is_byok=byok,
            upstream_cost_usd=upstream,
        )
