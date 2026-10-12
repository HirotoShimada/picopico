"""Claude route selection and the OpenRouter route (chat completions) for storyboards and translation.

Route order: ANTHROPIC_API_KEY → Anthropic directly (unchanged; see test_storyboard.py), else
OPENROUTER_API_KEY → Claude via OpenRouter, else the template. OpenRouter HTTP is mocked with respx
only (openrouter.ai is unreachable from the dev container: nothing here is verified for real).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import pipeline, schemas
from app.models import Batch, CostRecord, Project
from app.routers.generation import (
    STORYBOARD_LYRICS_RIGHTS_WARNING,
    claude_preview_limiter,
)
from app.services import llm
from app.services.openrouter import (
    CHAT_PROVIDER_PREFERENCES,
    OpenRouterChatClient,
    end_user_id,
)
from app.services.prompting import make_claude_client, translate_to_english
from app.services.storyboard import (
    STORYBOARD_SCHEMA,
    SYSTEM_PROMPT,
    claude_cost_usd,
    generate_storyboard,
)

OR_KEY = "or-test-key-not-real"
CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
ARTIST = "Mika Starlight"
LYRICS = [
    {"id": "l001", "text": "夜明け前の街を ひとりで歩いた", "start": 8.0, "end": 11.6},
    {"id": "l002", "text": "叫べ 今この瞬間を", "start": 24.0, "end": 27.6},
]


def _storyboard_json(n: int = 3) -> dict[str, Any]:
    protagonist = "a fictional woman in her late twenties with short silver hair, wearing a red raincoat"
    scene = {
        "section": "chorus", "lyric_ref": "l002", "scene_type": "walking", "camera_angle": "eye_level",
        "camera_move": "slow_push_in", "description_ja": "雨の港を歩く主人公",
        "prompt_en": f"Medium shot of {protagonist}, walking along a rainy harbor at night, vertical 9:16 composition",
        "negative_prompt_en": "text, watermark", "duration_hint": 5,
    }  # fmt: skip
    return {
        "world_bible": {
            "summary_ja": "雨の港町。", "look_en": "neon reflections, 35mm anamorphic", "protagonist_en": protagonist,
            "location_en": "a rainy harbor town at night", "palette": ["#0b0f2b", "#ff2e88", "#2ee6ff"],
            "consistency_tokens_en": "red raincoat, silver hair, magenta and cyan palette",
        },
        "scenes": [dict(scene) for _ in range(n)],
        "detected_real_people": [],
    }


def _chat(
    content: Any,
    *,
    finish: str = "stop",
    refusal: str | None = None,
    cost: float | None = 0.0123,
    model: str = "anthropic/claude-opus-5.5",
    usage: dict[str, Any] | None = None,
) -> httpx.Response:
    usage = usage if usage is not None else {
        "prompt_tokens": 6000, "completion_tokens": 3000, "total_tokens": 9000, "is_byok": False,
        "prompt_tokens_details": {"cached_tokens": 4000, "cache_write_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 200},
    }  # fmt: skip
    if cost is not None:
        usage = {**usage, "cost": cost}
    return httpx.Response(200, json={
        "id": "gen-123", "object": "chat.completion", "created": 1791763200, "model": model,
        "system_fingerprint": None,
        "choices": [{"index": 0, "finish_reason": finish,
                     "message": {"role": "assistant", "content": content, "refusal": refusal, "reasoning": None}}],
        "usage": usage,
    })  # fmt: skip


def _or_client(**kwargs: Any) -> OpenRouterChatClient:
    return OpenRouterChatClient(sleep=lambda _s: None, **kwargs)


def _board(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "title": "夜明けの歌", "artist_name": ARTIST, "mood": "切ない", "purpose": "新曲のプロモーション",
        "lyrics": LYRICS, "analysis": None, "preset_id": "cinematic", "custom_world": None, "n_scenes": 3,
        "seed": 11, "use_claude": True,
    }  # fmt: skip
    kwargs.update(overrides)
    return generate_storyboard(**kwargs)


@pytest.fixture
def openrouter_only(set_env: Callable[..., Any]) -> Any:
    return set_env(OPENROUTER_API_KEY=OR_KEY, ANTHROPIC_API_KEY="")


# --- route selection ---------------------------------------------------------------------------------------------


def test_route_order_anthropic_then_openrouter_then_template(set_env: Callable[..., Any]) -> None:
    settings = set_env(ANTHROPIC_API_KEY="sk-ant-test", OPENROUTER_API_KEY=OR_KEY)
    assert settings.llm_route == "anthropic" and llm.configured_model() == "claude-opus-5-5"
    assert type(make_claude_client()).__name__ == "Anthropic"

    settings = set_env(ANTHROPIC_API_KEY="", OPENROUTER_API_KEY=OR_KEY)
    assert settings.llm_route == "openrouter" and settings.llm_configured
    client = make_claude_client()
    assert isinstance(client, OpenRouterChatClient) and client.model == "anthropic/claude-opus-5.5"
    info = llm.llm_info()
    assert info["route"] == "openrouter" and info["model"] == "anthropic/claude-opus-5.5" and info["verified"] is False
    assert "OpenRouter" in info["data_policy_note"] and "歌詞" in info["data_policy_note"]

    settings = set_env(ANTHROPIC_API_KEY="", OPENROUTER_API_KEY="", OPENROUTER_CLAUDE_MODEL="anthropic/claude-sonnet-5.5")
    assert settings.llm_route is None and make_claude_client() is None and llm.llm_info()["model"] is None
    assert "OPENROUTER_API_KEY" not in repr(settings) and "sk-ant" not in repr(settings)


def test_openrouter_model_ids_are_priced_like_anthropic_ids() -> None:
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert llm.pricing_model_id("anthropic/claude-opus-5.5-20260921:batch") == "claude-opus-5-5"
    assert claude_cost_usd("anthropic/claude-opus-5.5", usage) == claude_cost_usd("claude-opus-5-5", usage) == 24.0
    assert claude_cost_usd("anthropic/claude-sonnet-5.5", usage) == pytest.approx(12.0)


# --- storyboard via OpenRouter ---------------------------------------------------------------------------------------


@respx.mock
def test_openrouter_storyboard_success(openrouter_only: Any) -> None:
    route = respx.post(CHAT_URL).mock(return_value=_chat(json.dumps(_storyboard_json())))
    board = _board(end_user="owner-123")  # no client passed: the configured route is used
    schemas.Storyboard.model_validate(board)
    assert board["source"] == "claude" and board["route"] == "openrouter"
    assert board["model"] == "anthropic/claude-opus-5.5" and len(board["scenes"]) == 3
    assert board["cost_usd"] == pytest.approx(0.0123)  # OpenRouter's usage.cost
    assert board["usage"] == {"input_tokens": 2000, "output_tokens": 3000, "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 4000}  # fmt: skip

    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {OR_KEY}" and "anthropic-beta" not in request.headers
    assert request.headers["x-openrouter-title"] == "AI Music Video Factory"
    body = json.loads(request.content)
    assert body["model"] == "anthropic/claude-opus-5.5" and body["stream"] is False
    assert body["max_completion_tokens"] == 16000 and body["reasoning"] == {"effort": "low"}
    assert body["provider"] == {"data_collection": "deny", "zdr": True, "require_parameters": True}
    assert body["provider"] == CHAT_PROVIDER_PREFERENCES
    assert body["response_format"] == {"type": "json_schema", "json_schema": {
        "name": "storyboard", "strict": True, "schema": STORYBOARD_SCHEMA}}  # fmt: skip
    system, user = body["messages"]
    assert system == {"role": "system", "content": [{"type": "text", "text": SYSTEM_PROMPT,
                                                       "cache_control": {"type": "ephemeral"}}]}  # fmt: skip
    payload = json.loads(user["content"])
    assert user["role"] == "user" and payload["forbidden_likeness"]["names"] == [ARTIST]
    assert ARTIST not in json.dumps({k: v for k, v in payload.items() if k != "forbidden_likeness"}, ensure_ascii=False)
    assert body["user"] == end_user_id("owner-123") and "owner-123" not in json.dumps(body)
    assert set(body) == {"model", "messages", "max_completion_tokens", "reasoning", "provider", "stream",
                         "response_format", "user"}  # fmt: skip


@respx.mock
def test_openrouter_cost_from_tokens_when_not_reported(openrouter_only: Any) -> None:
    respx.post(CHAT_URL).mock(return_value=_chat(json.dumps(_storyboard_json()), cost=None))
    board = _board()
    expected = (2000 * 4 + 3000 * 20 + 4000 * 0.20) / 1e6
    assert board["source"] == "claude" and board["cost_usd"] == pytest.approx(expected)


@respx.mock
def test_openrouter_content_parts_are_joined(openrouter_only: Any) -> None:
    text = json.dumps(_storyboard_json())
    parts = [{"type": "text", "text": text[:50]}, {"type": "text", "text": text[50:]}]
    respx.post(CHAT_URL).mock(return_value=_chat(parts))
    assert _board()["source"] == "claude"


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (_chat(None, refusal="I can't help with that."), "見送った"),
        (_chat("{}", finish="content_filter"), "見送った"),
        (_chat('{"world_bible": {', finish="length"), "途中まで"),
        (_chat(None, finish="error"), "提供元でエラー"),
        (_chat("not json at all {"), "解析できなかった"),
        (_chat(json.dumps({"world_bible": {}, "scenes": []})), "解析できなかった"),
    ],
)
@respx.mock
def test_openrouter_unusable_answers_fall_back_but_are_booked(
    openrouter_only: Any, response: httpx.Response, expected: str
) -> None:
    respx.post(CHAT_URL).mock(return_value=response)
    board = _board()
    assert board["source"] == "template" and len(board["scenes"]) == 3
    assert any(expected in w for w in board["warnings"]), board["warnings"]
    assert board["route"] == "openrouter" and board["model"] == "anthropic/claude-opus-5.5"
    assert board["cost_usd"] == pytest.approx(0.0123)  # the answered call is billed
    assert board["scenes"] == _board(use_claude=False)["scenes"], "fallback equals the deterministic template"


@pytest.mark.parametrize(
    ("status", "message", "expected", "calls"),
    [
        (402, "Insufficient credits", "クレジットが不足", 1),
        (403, "Flagged by moderation", "モデレーション", 1),
        (404, "No endpoints found matching your data policy (Zero data retention)", "提供元", 1),
        (503, "No allowed providers are available for the selected model", "提供元", 1),
        (401, "No auth credentials found", "API キーが無効", 1),
        (400, "Invalid schema", "受け付けなかった", 1),
        (429, "Rate limit exceeded", "利用上限", 3),
        (500, "Internal error", "HTTP 500", 3),
        (529, "Provider overloaded", "HTTP 529", 3),
    ],
)
@respx.mock
def test_openrouter_http_errors_fall_back_to_the_template(
    openrouter_only: Any, status: int, message: str, expected: str, calls: int
) -> None:
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(status, json={"error": {"code": status, "message": message}})
    )
    board = _board(client=_or_client())
    assert board["source"] == "template" and board["cost_usd"] == 0.0 and board["model"] is None
    assert board["route"] == "openrouter"
    assert any(expected in w for w in board["warnings"]), board["warnings"]
    assert route.call_count == calls  # 429/5xx are retried like the Anthropic SDK (2 retries); others are not


@respx.mock
def test_openrouter_connection_error_falls_back(openrouter_only: Any) -> None:
    route = respx.post(CHAT_URL).mock(side_effect=httpx.ConnectError("unreachable"))
    board = _board(client=_or_client(max_retries=1))
    assert board["source"] == "template" and any("接続できなかった" in w for w in board["warnings"])
    assert route.call_count == 2


@respx.mock
def test_openrouter_retry_honours_retry_after(openrouter_only: Any) -> None:
    sleeps: list[float] = []
    respx.post(CHAT_URL).mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "3"}, json={"error": {"message": "slow down"}}),
        _chat(json.dumps(_storyboard_json())),
    ])  # fmt: skip
    board = _board(client=OpenRouterChatClient(sleep=sleeps.append))
    assert board["source"] == "claude" and sleeps == [3.0]


def test_template_without_any_key_sends_nothing(set_env: Callable[..., Any]) -> None:
    set_env(OPENROUTER_API_KEY="", ANTHROPIC_API_KEY="")
    with respx.mock(assert_all_called=False) as router:
        route = router.post(CHAT_URL).mock(return_value=_chat("{}"))
        board = _board()
    assert board["source"] == "template" and board["route"] is None and route.called is False


# --- translation ---------------------------------------------------------------------------------------------------


@respx.mock
def test_translation_uses_the_openrouter_route(openrouter_only: Any) -> None:
    route = respx.post(CHAT_URL).mock(return_value=_chat("“A quiet seaside at dawn”"))
    assert translate_to_english("夜明けの静かな海辺") == ("A quiet seaside at dawn", "claude")
    body = json.loads(route.calls.last.request.content)
    assert body["max_completion_tokens"] == 4000 and "response_format" not in body
    assert body["provider"] == CHAT_PROVIDER_PREFERENCES
    assert body["messages"][0]["role"] == "system" and isinstance(body["messages"][0]["content"], str)
    assert body["messages"][1] == {"role": "user", "content": "夜明けの静かな海辺"}


@pytest.mark.parametrize(
    "response",
    [httpx.Response(402, json={"error": {"message": "credits"}}), _chat(None, refusal="no"), _chat("x", finish="length")],
)
@respx.mock
def test_translation_falls_back_to_the_glossary(openrouter_only: Any, response: httpx.Response) -> None:
    respx.post(CHAT_URL).mock(return_value=response)
    assert translate_to_english("森", client=_or_client()) == ("forest", "glossary")


# --- API: preview guards, cost records, capabilities -------------------------------------------------------------------


@pytest.fixture
def or_api(make_client: Callable[..., TestClient]) -> Iterator[TestClient]:
    claude_preview_limiter.reset()
    yield make_client(OPENROUTER_API_KEY=OR_KEY, ANTHROPIC_API_KEY="")
    claude_preview_limiter.reset()


def test_preview_via_openrouter_requires_lyrics_rights_and_books_openrouter(
    or_api: TestClient, db: Session, ready_project: Project
) -> None:
    url = f"/api/projects/{ready_project.id}/storyboard"
    with respx.mock(assert_all_called=False) as router:
        route = router.post(CHAT_URL).mock(return_value=_chat(json.dumps(_storyboard_json())))
        body = or_api.post(url, json={"scenes": 3}).json()
        assert body["source"] == "claude" and body["route"] == "openrouter" and route.call_count == 1
        sent = json.loads(json.loads(route.calls.last.request.content)["messages"][1]["content"])
        assert [line["id"] for line in sent["lyrics"]]  # the lyrics went to OpenRouter (rights confirmed)
        record = db.scalars(select(CostRecord)).one()
        assert (record.kind, record.provider, record.model) == ("llm", "openrouter", "anthropic/claude-opus-5.5")
        assert record.amount_usd == pytest.approx(0.0123) and record.note == "絵コンテのプレビュー（Claude・OpenRouter 経由）"
        assert record.batch_id is None and record.owner_id == ready_project.owner_id

        ready_project.rights = {**ready_project.rights, "lyrics_rights": False}
        ready_project.rights_confirmed_at = None
        db.commit()
        body = or_api.post(url, json={"scenes": 3}).json()
        assert body["source"] == "template" and body["warnings"][0] == STORYBOARD_LYRICS_RIGHTS_WARNING
        assert route.call_count == 1  # nothing (and no lyrics) sent to OpenRouter
    assert len(db.scalars(select(CostRecord)).all()) == 1


def test_openrouter_previews_are_rate_limited_per_user(or_api: TestClient, ready_project: Project) -> None:
    url = f"/api/projects/{ready_project.id}/storyboard"
    with respx.mock(assert_all_called=False) as router:
        route = router.post(CHAT_URL).mock(return_value=_chat(json.dumps(_storyboard_json(1))))
        for _ in range(10):
            assert or_api.post(url, json={"scenes": 1}).status_code == 200
        limited = or_api.post(url, json={"scenes": 1})
    assert limited.status_code == 429 and limited.json()["code"] == "rate_limited"
    assert "retry-after" in limited.headers and route.call_count == 10


def test_capabilities_report_the_llm_route_without_secrets(make_client: Callable[..., TestClient]) -> None:
    client = make_client(OPENROUTER_API_KEY=OR_KEY, ANTHROPIC_API_KEY="", OPENROUTER_API_BASE="http://127.0.0.1:9/api/v1")
    response = client.get("/api/system/capabilities")
    body = response.json()
    schemas.CapabilitiesOut.model_validate(body)
    assert OR_KEY not in response.text
    assert body["claude_configured"] is True and body["llm"]["route"] == "openrouter"
    assert body["llm"]["model"] == "anthropic/claude-opus-5.5" and "OpenRouter" in body["llm"]["data_policy_note"]
    openrouter = next(p for p in body["providers"] if p["id"] == "openrouter")
    assert openrouter["configured"] and openrouter["available"] and openrouter["kind"] == "api"
    assert openrouter["models_source"] == "fallback" and openrouter["models"]  # listing unreachable → built-in list
    assert "ZDR" in openrouter["commercial_use_note"] and "SynthID" in openrouter["commercial_use_note"]

    client = make_client(ANTHROPIC_API_KEY="sk-ant-test-secret", OPENROUTER_API_KEY=OR_KEY)
    body = client.get("/api/system/capabilities").json()
    assert body["llm"]["route"] == "anthropic" and body["llm"]["model"] == "claude-opus-5-5"


# --- pipeline -----------------------------------------------------------------------------------------------------------


def test_batch_storyboard_via_openrouter_books_an_openrouter_llm_record(
    db: Session, ready_project: Project, sync_tasks: list, fake_render: Any, fake_clips: Any, set_env: Any
) -> None:
    set_env(OPENROUTER_API_KEY=OR_KEY, ANTHROPIC_API_KEY="", OPENROUTER_API_BASE="http://127.0.0.1:9/api/v1")
    settings = schemas.GenerationSettings(provider="mock", count=1, duration_sec=5, seed=3)
    preview = pipeline.preview_batch(db, ready_project, settings)
    assert preview.estimate["llm_usd"] > 0  # the OpenRouter route is priced in the estimate too
    batch = pipeline.create_batch(db, ready_project, settings)
    with respx.mock(assert_all_called=False) as router:
        route = router.post("http://127.0.0.1:9/api/v1/chat/completions").mock(
            return_value=_chat(json.dumps(_storyboard_json(4)), cost=0.05)
        )
        pipeline.run_batch(batch.id)
    assert route.call_count == 1
    db.expire_all()
    batch = db.get(Batch, batch.id)
    assert batch.storyboard["source"] == "claude" and batch.storyboard["route"] == "openrouter"
    body = json.loads(route.calls.last.request.content)
    assert body["user"] == end_user_id(ready_project.owner_id)
    (record,) = db.scalars(select(CostRecord).where(CostRecord.kind == "llm")).all()
    assert (record.provider, record.amount_usd, record.batch_id) == ("openrouter", pytest.approx(0.05), batch.id)
    assert record.note == "絵コンテ生成（Claude・OpenRouter 経由）"
    assert batch.cost_usd == pytest.approx(0.05) and batch.status == "completed"


# --- BYOK (review follow-up) -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("usage_extra", "expected"),
    [
        # usage.cost = OpenRouter's fee; the vendor's charge is reported separately [B] → both are booked.
        ({"cost": 0.0, "is_byok": True, "cost_details": {"upstream_inference_cost": 0.184}}, 0.184),
        ({"cost": 0.009, "is_byok": True, "cost_details": {"upstream_inference_cost": 0.184}}, 0.193),
        # No upstream figure: at least the token price (6000 in / 3000 out, 4000 cached).
        ({"cost": 0.001, "is_byok": True}, (2000 * 4 + 3000 * 20 + 4000 * 0.20) / 1e6),
        # Not BYOK: usage.cost is the whole charge (upstream_inference_cost is ignored).
        ({"cost": 0.0123, "is_byok": False, "cost_details": {"upstream_inference_cost": 0.5}}, 0.0123),
    ],
    ids=["byok-fee-zero", "byok-fee-plus-upstream", "byok-no-upstream", "not-byok"],
)
@respx.mock
def test_byok_storyboards_book_the_vendor_charge(openrouter_only: Any, usage_extra: dict[str, Any],
                                                 expected: float) -> None:  # fmt: skip
    usage = {
        "prompt_tokens": 6000, "completion_tokens": 3000, "total_tokens": 9000,
        "prompt_tokens_details": {"cached_tokens": 4000, "cache_write_tokens": 0}, **usage_extra,
    }  # fmt: skip
    respx.post(CHAT_URL).mock(return_value=_chat(json.dumps(_storyboard_json()), cost=None, usage=usage))
    board = _board()
    assert board["source"] == "claude" and board["cost_usd"] == pytest.approx(expected)
