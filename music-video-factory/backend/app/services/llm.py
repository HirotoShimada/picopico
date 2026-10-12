"""Claude for storyboards and translation: route selection and one call shape for both routes.

Route order (user decision, docs/VIDEO_PROVIDERS.md §8):

1. ``ANTHROPIC_API_KEY`` set → **Anthropic directly** (``anthropic`` SDK 1.x,
   ``client.beta.messages.create`` exactly as before: server-side fallback
   beta, ``output_config`` json_schema + effort ``low``).
2. else ``OPENROUTER_API_KEY`` set → **Claude via OpenRouter**
   (:class:`~app.services.openrouter.OpenRouterChatClient`,
   ``POST /chat/completions`` with ``response_format`` json_schema strict and
   ``provider: {data_collection: "deny", zdr: true, require_parameters: true}``).
3. neither → no LLM: the storyboard uses the template, translation the glossary.

:func:`complete` sends one system + user message on either route and returns
an :class:`LlmReply` (``finish``: ``ok`` | ``refusal`` | ``length`` |
``error``) or raises :class:`LlmCallError` (``kind`` + HTTP status), so the
callers map both routes to the same Japanese fallback warnings. Neither route
has been verified with a real key (tests use mocked HTTP only).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.config import Settings, get_settings
from app.services.openrouter import (
    ROUTE_OPENROUTER,
    OpenRouterChatClient,
    OpenRouterChatError,
)

logger = logging.getLogger(__name__)

__all__ = [
    "FALLBACK_BETA",
    "ROUTE_ANTHROPIC",
    "ROUTE_OPENROUTER",
    "LlmCallError",
    "LlmReply",
    "api_label",
    "complete",
    "configured_model",
    "first_text_block",
    "llm_info",
    "llm_route",
    "make_llm_client",
    "pricing_model_id",
    "route_of",
    "usage_dict",
]

ROUTE_ANTHROPIC = "anthropic"
# Server-side refusal fallback ("default" routing) for claude-opus-5-5 requests (Anthropic route only).
FALLBACK_BETA = "server-side-fallback-2026-07-01"

DATA_POLICY_NOTES: dict[str | None, str] = {
    ROUTE_ANTHROPIC: (
        "絵コンテの生成時に、楽曲名・ムード・目的・カスタム世界観・歌詞の本文・楽曲解析の要約を Anthropic（Claude）に"
        "直接送ります。アーティスト名と別表記は「描写してはいけない名前」としてのみ送ります。"
    ),
    ROUTE_OPENROUTER: (
        "ANTHROPIC_API_KEY がないため、Claude には OpenRouter 経由で送ります。絵コンテの生成時に、歌詞の本文を含む"
        "同じ内容（楽曲名・ムード・目的・カスタム世界観・楽曲解析の要約）が OpenRouter と、その先の Claude の提供元"
        "（Anthropic / Amazon Bedrock / Google Vertex / Azure など）を通ります。毎回、データを収集しない提供元だけ"
        "（data_collection: deny）・データを保持しない提供元だけ（zdr: true）を指定しますが、実際の扱いは未検証です。"
        "OpenRouter のアカウント設定でプロンプトのログ記録はオフのままにしてください。"
    ),
    None: "Claude は設定されていません。絵コンテはテンプレートで作り、歌詞は外部に送りません。",
}


@dataclass
class LlmReply:
    text: str | None
    model: str
    usage: dict[str, int] | None  # Anthropic Messages key names on both routes
    cost_usd: float | None  # provider-reported USD (OpenRouter usage.cost); None → price the tokens
    finish: str  # "ok" | "refusal" | "length" | "error"
    route: str
    # OpenRouter BYOK (the vendor bills the user's own key): usage.cost may be OpenRouter's fee only [B].
    is_byok: bool | None = None
    upstream_cost_usd: float | None = None  # cost_details.upstream_inference_cost, when reported


class LlmCallError(Exception):
    """The request produced no answer (nothing to bill). ``kind`` as in ``OpenRouterChatError``."""

    def __init__(self, kind: str, *, route: str, status: int | None = None, detail: str = ""):
        super().__init__(detail or kind)
        self.kind = kind
        self.route = route
        self.status = status
        self.detail = detail


def llm_route(settings: Settings | None = None) -> str | None:
    return (settings or get_settings()).llm_route


def configured_model(route: str | None = None, settings: Settings | None = None) -> str | None:
    settings = settings or get_settings()
    route = route if route is not None else settings.llm_route
    if route == ROUTE_ANTHROPIC:
        return settings.ANTHROPIC_MODEL
    if route == ROUTE_OPENROUTER:
        return settings.OPENROUTER_CLAUDE_MODEL
    return None


def route_of(client: Any) -> str:
    return ROUTE_OPENROUTER if isinstance(client, OpenRouterChatClient) else ROUTE_ANTHROPIC


def api_label(route: str) -> str:
    return "OpenRouter" if route == ROUTE_OPENROUTER else "Claude API"


def make_llm_client(settings: Settings | None = None) -> Any | None:
    """The client of the configured route: ``anthropic.Anthropic``, ``OpenRouterChatClient`` or ``None``."""
    settings = settings or get_settings()
    route = settings.llm_route
    if route == ROUTE_ANTHROPIC:
        import anthropic

        return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=300.0, max_retries=2)
    if route == ROUTE_OPENROUTER:
        return OpenRouterChatClient(settings=settings)
    return None


_DATE_SUFFIX = re.compile(r"-\d{8}$")


def pricing_model_id(model: str | None) -> str:
    """Anthropic model id for token pricing: ``anthropic/claude-opus-5.5[-20260921][:batch]`` → ``claude-opus-5-5``."""
    text = str(model or "").strip()
    if "/" in text:
        text = text.split("/", 1)[1]
    text = text.split(":", 1)[0]
    text = _DATE_SUFFIX.sub("", text)
    return text.replace(".", "-")


def first_text_block(response: Any) -> str | None:
    """Text of the first ``text`` content block of a Messages API response (thinking blocks are skipped)."""
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text":
            return str(getattr(block, "text", ""))
    return None


def usage_dict(response: Any) -> dict[str, int] | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    result: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        value = getattr(usage, key, None)
        if isinstance(value, int):
            result[key] = value
    return result


def complete(
    client: Any,
    *,
    system: str,
    user: str,
    max_tokens: int,
    json_schema: Mapping[str, Any] | None = None,
    schema_name: str = "response",
    cache_system: bool = False,
    end_user: str | None = None,
    purpose: str = "request",
) -> LlmReply:
    """One Claude call on the route ``client`` belongs to (see the module docstring)."""
    if route_of(client) == ROUTE_OPENROUTER:
        return _complete_openrouter(
            client, system=system, user=user, max_tokens=max_tokens, json_schema=json_schema,
            schema_name=schema_name, cache_system=cache_system, end_user=end_user, purpose=purpose,
        )  # fmt: skip
    return _complete_anthropic(client, system=system, user=user, max_tokens=max_tokens, json_schema=json_schema,
                               cache_system=cache_system, purpose=purpose)  # fmt: skip


def _complete_anthropic(
    client: Any,
    *,
    system: str,
    user: str,
    max_tokens: int,
    json_schema: Mapping[str, Any] | None,
    cache_system: bool,
    purpose: str,
) -> LlmReply:
    import anthropic

    settings = get_settings()
    output_config: dict[str, Any] = {"effort": "low"}
    if json_schema is not None:
        output_config = {"format": {"type": "json_schema", "schema": json_schema}, "effort": "low"}
    system_param: Any = system
    if cache_system:
        system_param = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    try:
        response = client.beta.messages.create(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            output_config=output_config,
            system=system_param,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.BadRequestError as exc:
        logger.warning("Claude %s request rejected (400): %s", purpose, exc.message)
        raise LlmCallError("bad_request", route=ROUTE_ANTHROPIC, status=400, detail=str(exc.message)) from exc
    except anthropic.RateLimitError as exc:
        logger.warning("Claude %s request rate limited", purpose)
        raise LlmCallError("rate_limited", route=ROUTE_ANTHROPIC, status=429) from exc
    except anthropic.APIStatusError as exc:
        logger.warning("Claude %s request failed with HTTP %s", purpose, exc.status_code)
        raise LlmCallError("http_error", route=ROUTE_ANTHROPIC, status=exc.status_code) from exc
    except anthropic.APIConnectionError as exc:
        logger.warning("Claude %s request: connection error (%s)", purpose, type(exc).__name__)
        raise LlmCallError("connection", route=ROUTE_ANTHROPIC) from exc
    finish = "refusal" if getattr(response, "stop_reason", None) == "refusal" else "ok"
    return LlmReply(
        text=first_text_block(response),
        model=str(getattr(response, "model", None) or settings.ANTHROPIC_MODEL),
        usage=usage_dict(response),
        cost_usd=None,
        finish=finish,
        route=ROUTE_ANTHROPIC,
    )


def _complete_openrouter(
    client: OpenRouterChatClient,
    *,
    system: str,
    user: str,
    max_tokens: int,
    json_schema: Mapping[str, Any] | None,
    schema_name: str,
    cache_system: bool,
    end_user: str | None,
    purpose: str,
) -> LlmReply:
    try:
        reply = client.chat(system=system, user=user, max_tokens=max_tokens, json_schema=json_schema,
                            schema_name=schema_name, cache_system=cache_system, end_user=end_user)  # fmt: skip
    except OpenRouterChatError as exc:
        logger.warning("Claude %s via OpenRouter failed (%s, HTTP %s): %s", purpose, exc.kind, exc.status, exc.message)
        raise LlmCallError(exc.kind, route=ROUTE_OPENROUTER, status=exc.status, detail=exc.message) from exc
    if reply.refused:
        finish = "refusal"
    elif reply.finish_reason == "length":
        finish = "length"
    elif reply.finish_reason == "error":
        finish = "error"
    else:
        finish = "ok"
    if finish != "ok":
        logger.warning("Claude %s via OpenRouter ended with finish_reason=%s (refusal=%s)", purpose,
                       reply.finish_reason, bool(reply.refusal))  # fmt: skip
    return LlmReply(text=reply.text, model=reply.model, usage=reply.usage, cost_usd=reply.cost_usd, finish=finish,
                    route=ROUTE_OPENROUTER, is_byok=reply.is_byok, upstream_cost_usd=reply.upstream_cost_usd)  # fmt: skip


def llm_info(settings: Settings | None = None) -> dict[str, Any]:
    """``CapabilitiesOut.llm``: the route in use, its model id and what is sent where (no secrets)."""
    settings = settings or get_settings()
    route = settings.llm_route
    return {
        "route": route,
        "model": configured_model(route, settings),
        "data_policy_note": DATA_POLICY_NOTES[route],
        "verified": False,  # neither route has been exercised with a real key yet
    }
