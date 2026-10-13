"""Prompt safety and JA→EN translation (ARCHITECTURE.md §6.3)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import anthropic
import httpx2
import pytest

from app.services.prompting import (
    LIKENESS_SAFETY_CLAUSE,
    MAX_PROMPT_CHARS,
    NO_TEXT_CLAUSE,
    artist_name_variants,
    detect_person_names,
    glossary_keywords,
    glossary_translate,
    mood_keywords,
    rewrite_brand_names,
    sanitize_prompt,
    screen_free_text,
    strip_forbidden_names,
    strip_person_references,
    translate_to_english,
)


def _assert_clauses_once(prompt: str) -> None:
    assert prompt.count(LIKENESS_SAFETY_CLAUSE) == 1
    assert prompt.count(NO_TEXT_CLAUSE) == 1
    assert prompt.rstrip(".").endswith(NO_TEXT_CLAUSE)


# --- sanitize_prompt ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Taylor Swift walking in the rain at night",
        "a singer like taylor swift on a rooftop",
        "TAYLOR-SWIFT dancing in a field",
        "Ｔａｙｌｏｒ　Ｓｗｉｆｔ under neon lights",  # full-width
        "taylorswift by the sea",
    ],
)
def test_removes_latin_artist_name_variants(text: str) -> None:
    prompt, warnings = sanitize_prompt(text, forbidden_names=["Taylor Swift"])
    assert "taylor" not in prompt.lower() and "swift" not in prompt.lower()
    assert any("Taylor Swift" in w and "削除" in w for w in warnings)
    _assert_clauses_once(prompt)


def test_latin_names_respect_word_boundaries() -> None:
    prompt, warnings = sanitize_prompt("Aurora borealis over a lake", forbidden_names=["Rora"])
    assert "Aurora borealis" in prompt and warnings == []


@pytest.mark.parametrize("text", ["米津玄師が夜の街を歩く", "米津 玄師 が夜の街を歩く", "米津・玄師の後ろ姿"])
def test_removes_japanese_artist_name(text: str) -> None:
    prompt, warnings = sanitize_prompt(text, forbidden_names=["米津 玄師"])
    assert "米津" not in prompt and "玄師" not in prompt
    assert warnings and all(not w.isascii() for w in warnings)
    _assert_clauses_once(prompt)


@pytest.mark.parametrize(
    ("text", "phrase"),
    [
        ("a celebrity walking down the street", "celebrity"),
        ("a lookalike of a pop star", "lookalike"),
        ("a look-alike singer on stage", "look-alike"),
        ("a famous singer at the beach", "famous singer"),
        ("a real person standing in the rain", "real person"),
        ("the artist himself on a rooftop", "the artist himself"),
        ("a deepfake portrait in neon light", "deepfake"),
        ("in the likeness of a movie star", "in the likeness of"),
    ],
)
def test_removes_real_person_phrasing_en(text: str, phrase: str) -> None:
    prompt, warnings = sanitize_prompt(text, forbidden_names=[])
    body = prompt.split(LIKENESS_SAFETY_CLAUSE)[0].lower()
    assert phrase.lower() not in body
    assert any(phrase.lower() in w.lower() for w in warnings)
    _assert_clauses_once(prompt)


@pytest.mark.parametrize("term", ["本人", "ご本人", "芸能人", "有名人", "そっくり", "激似", "実在の人物", "モノマネ"])
def test_removes_real_person_phrasing_ja(term: str) -> None:
    prompt, warnings = sanitize_prompt(f"アーティスト{term}が海辺に立つ", forbidden_names=[])
    assert term not in prompt
    assert any(term in w for w in warnings)


def test_japanese_person_is_not_flagged_as_honnin() -> None:
    prompt, warnings = sanitize_prompt("日本人の女性が海辺に立つ", forbidden_names=[])
    assert "日本人" in prompt and warnings == []


def test_clean_prompt_gets_clauses_without_warnings() -> None:
    prompt, warnings = sanitize_prompt(
        "A fictional woman walking through a misty forest at dawn", forbidden_names=["Hikaru"]
    )
    assert warnings == []
    assert prompt.startswith("A fictional woman walking through a misty forest at dawn. ")
    _assert_clauses_once(prompt)


def test_sanitize_is_idempotent_and_keeps_existing_safety_wording_single() -> None:
    text = f"A man on a pier at night, no real people, no text. {LIKENESS_SAFETY_CLAUSE}, {NO_TEXT_CLAUSE}."
    once, warnings = sanitize_prompt(text, forbidden_names=["Someone"])
    twice, warnings_twice = sanitize_prompt(once, forbidden_names=["Someone"])
    assert once == twice and warnings == warnings_twice == []
    assert "no real people" not in once and "no no" not in once
    _assert_clauses_once(once)


@pytest.mark.parametrize("text", ["word " * 400, "A long sentence about the sea. " * 60, "x" * 3000])
def test_sanitize_respects_max_chars(text: str) -> None:
    prompt, warnings = sanitize_prompt(text, forbidden_names=[])
    assert len(prompt) <= MAX_PROMPT_CHARS
    assert any("省略" in w for w in warnings)
    _assert_clauses_once(prompt)
    assert sanitize_prompt(prompt, forbidden_names=[])[0] == prompt
    short, _ = sanitize_prompt(text, forbidden_names=[], max_chars=400)
    assert len(short) <= 400


def test_prompt_of_only_forbidden_content_is_just_the_clauses() -> None:
    prompt, warnings = sanitize_prompt("Taylor Swift celebrity", forbidden_names=["Taylor Swift"])
    assert prompt == f"{LIKENESS_SAFETY_CLAUSE}, {NO_TEXT_CLAUSE}."
    assert len(warnings) == 2


def test_strip_forbidden_names_ignores_blank_and_single_char_names() -> None:
    text, removed = strip_forbidden_names("A quiet harbor at dawn", ["", "  ", "A"])
    assert text == "A quiet harbor at dawn" and removed == []


# --- translation ----------------------------------------------------------------------------------------


def test_translate_passthrough_for_ascii_and_empty() -> None:
    assert translate_to_english("Neon city at night") == ("Neon city at night", "passthrough")
    assert translate_to_english("   ") == ("", "passthrough")


def test_translate_uses_glossary_without_api_key() -> None:
    text, source = translate_to_english("夜明けの海辺、静かな波と青い光")
    assert source == "glossary"
    assert text == "dawn seaside, quiet waves, blue light"


def test_glossary_prefers_longest_match_and_keeps_ascii() -> None:
    assert glossary_translate("空港") == "airport"
    assert glossary_translate("夜明け前の街") == "pre-dawn city streets"
    assert glossary_keywords("ネオンとTokyoの雨", limit=3) == ["neon lights", "Tokyo", "rain"]
    assert glossary_keywords("ネオンとTokyoの雨", include_ascii=False) == ["neon lights", "rain"]
    assert glossary_translate("ひとりで歩いた") == "alone walking"
    assert glossary_translate("未知の単語ばかり") == ""


def _mock_client(handler: Callable[[httpx2.Request], httpx2.Response]) -> anthropic.Anthropic:
    return anthropic.Anthropic(
        api_key="test-key", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)), max_retries=0
    )


def _message(text: str, stop_reason: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": text}],
        "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 50, "output_tokens": 20},
    }  # fmt: skip


def test_translate_with_claude_client() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append({"body": json.loads(request.content), "beta": request.headers.get("anthropic-beta")})
        return httpx2.Response(200, json=_message("A quiet seaside at dawn with soft blue light"))

    text, source = translate_to_english("夜明けの静かな海辺", client=_mock_client(handler))
    assert (text, source) == ("A quiet seaside at dawn with soft blue light", "claude")
    body = requests[0]["body"]
    assert body["model"] == "claude-opus-5-5" and body["fallbacks"] == "default"
    assert body["output_config"] == {"effort": "low"}
    assert requests[0]["beta"] == "server-side-fallback-2026-07-01"
    assert body["messages"] == [{"role": "user", "content": "夜明けの静かな海辺"}]


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx2.Response(
            429, json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}}
        ),
        lambda r: httpx2.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "boom"}}),
        lambda r: httpx2.Response(
            400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}
        ),
        lambda r: httpx2.Response(200, json=_message("", stop_reason="refusal")),
    ],
    ids=["rate_limit", "server_error", "bad_request", "refusal"],
)
def test_translate_falls_back_to_glossary_on_claude_failure(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> None:
    text, source = translate_to_english("雨の夜の東京", client=_mock_client(handler))
    assert (text, source) == ("rain night Tokyo", "glossary")


def test_translate_falls_back_on_connection_error() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("network down", request=request)

    assert translate_to_english("森", client=_mock_client(handler)) == ("forest", "glossary")


def test_translate_skips_claude_when_disabled() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:  # pragma: no cover - must not be called
        raise AssertionError("Claude must not be called")

    assert translate_to_english("森", use_claude=False, client=_mock_client(handler)) == ("forest", "glossary")


# --- real people in free text (look-alike phrasing, names, aliases) -------------------------------------


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("a man resembling Brad Pitt", ["Brad", "Pitt"]),
        ("Keanu Reeves lookalike walking in the rain", ["Keanu", "Reeves"]),
        ("a man who looks exactly like Keanu Reeves walking in rain", ["Keanu", "Reeves"]),
        ("a man who looks like keanu reeves walking in rain", ["keanu", "reeves"]),
        ("neon street in the style of Taylor Swift", ["Taylor", "Swift"]),
        ("Kenshi Yonezu singing on a Tokyo rooftop at night", ["Kenshi", "Yonezu"]),
        ("Taylor Swift singing on a neon rooftop in Tokyo", ["Taylor", "Swift"]),
        ("Billie Eilish vibes at night", ["Billie", "Eilish"]),
        ("a girl like Aimyon on the beach", ["Aimyon"]),
        ("Lynch-esque motel at night", ["Lynch"]),
        ("木村拓哉似の男性が雨の東京を歩く", ["木村", "拓哉"]),
        ("米津玄師に似た男性が歩く", ["米津"]),
    ],
)
def test_removes_real_people_named_in_free_text(text: str, names: list[str]) -> None:
    prompt, warnings = sanitize_prompt(text, forbidden_names=[])
    body = prompt.split(LIKENESS_SAFETY_CLAUSE)[0]
    assert not any(name in body for name in names), body
    assert warnings and all(not w.isascii() for w in warnings)
    assert sanitize_prompt(prompt, forbidden_names=[])[0] == prompt  # idempotent
    _assert_clauses_once(prompt)


@pytest.mark.parametrize(
    "text",
    [
        "the sky looks like a dream over the harbor",
        "Tokyo Tower at dusk, New York streets in the rain",
        "inspired by 1970s cinema, warm grain",
        "Neon Rain Over Shibuya",
        "Medium Close-Up of a fictional woman, tungsten-balanced cinema film look",
        "Aurora borealis over a lake",
    ],
)
def test_ordinary_phrases_and_places_are_not_treated_as_people(text: str) -> None:
    refs = strip_person_references(text)
    assert refs.text == text and refs.phrases == [] and refs.names == []
    assert sanitize_prompt(text, forbidden_names=[])[1] == []


def test_detect_person_names() -> None:
    assert detect_person_names("Kenshi Yonezu singing with Lilas Ikuta in Shibuya") == ["Kenshi Yonezu", "Lilas Ikuta"]
    assert detect_person_names("渋谷で Kenshi Yonezu が歩く夜") == ["Kenshi Yonezu"]
    assert detect_person_names("Golden Hour over Tokyo Bay") == []
    # Title Case text needs two unknown words in a run before it counts as a name.
    assert detect_person_names("Velvet Forest Under Neon Lights") == []
    assert detect_person_names("a Velvet Forest under neon lights") == ["Velvet Forest"]


def test_kana_artist_names_are_also_forbidden_in_romaji() -> None:
    assert artist_name_variants("あいみょん") == ["あいみょん", "aimyon"]
    assert artist_name_variants("ヨルシカ") == ["ヨルシカ", "yorushika"]
    assert artist_name_variants("ゆうり") == ["ゆうり", "yuuri", "yuri"]
    assert artist_name_variants("米津玄師") == ["米津玄師"]  # kanji readings need an alias
    prompt, warnings = sanitize_prompt("Aimyon walking by the sea", forbidden_names=artist_name_variants("あいみょん"))
    assert "aimyon" not in prompt.lower() and warnings


def test_screen_free_text_and_mood_keywords() -> None:
    text, names, warnings = screen_free_text("Mika Starlight と Kenshi Yonezu が歩く夜", ["Mika Starlight"],
                                             where="カスタム世界観から")  # fmt: skip
    assert "Mika" not in text and "Kenshi" not in text and names == ["Kenshi Yonezu"]
    assert any("アーティスト名を削除" in w for w in warnings) and not any("Mika" in w for w in warnings)
    assert any("Kenshi Yonezu" in w and w.startswith("カスタム世界観から") for w in warnings)
    assert mood_keywords("Billie Eilish vibes") == []
    assert mood_keywords("dark, moody and cinematic, like Taylor Swift") == ["dark", "moody", "cinematic"]
    assert mood_keywords("切なくて静かな夜") == ["bittersweet", "quiet", "night"]


# --- product names get drawn as text by video models (seen live 2026-10-13) --------------------------------------

@pytest.mark.parametrize(
    ("text", "brand"),
    [
        ("35mm anamorphic, CineStill 800T halation, vertical 9:16", "CineStill 800T"),
        ("Kodak Portra 400 look with soft grain", "Kodak Portra 400"),
        ("Kodak Vision3 500T film look", "Kodak Vision3 500T"),
        ("16mm Kodak Ektachrome colours", "Kodak Ektachrome"),
        ("Fujifilm Eterna-like muted look", "Fujifilm Eterna-like"),
        ("shot on ARRI Alexa Mini LF with Cooke S4 primes", "ARRI Alexa Mini LF"),
        ("Ilford HP5 Plus 400 grain", "Ilford HP5 Plus 400"),
    ],
)
def test_sanitize_rewrites_product_names(text: str, brand: str) -> None:
    out, warnings = sanitize_prompt(text, forbidden_names=[])
    lowered = out.lower()
    assert not any(word in lowered for word in ("cinestill", "kodak", "fujifilm", "arri", "ilford", "cooke"))
    assert any(brand in w and "文字として描かれる" in w for w in warnings)
    assert sanitize_prompt(out, forbidden_names=[]) == (out, [])  # idempotent, no new warnings


def test_generic_film_and_place_words_are_kept() -> None:
    text = "Mount Fuji at dawn, 35mm anamorphic lens, tungsten night-film halation, 16mm grain"
    assert rewrite_brand_names(text) == (text, [])
