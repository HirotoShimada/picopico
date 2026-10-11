"""Storyboard generation: template and Claude paths (ARCHITECTURE.md §6.3)."""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable
from typing import Any

import anthropic
import httpx2
import pytest

from app import schemas
from app.services.presets import CAMERA_ANGLES, CAMERA_MOVES, SCENE_TYPES, WORLD_PRESETS
from app.services.prompting import LIKENESS_SAFETY_CLAUSE, NO_TEXT_CLAUSE
from app.services.storyboard import (
    STORYBOARD_PROMPT_CHARS,
    STORYBOARD_SCHEMA,
    SYSTEM_PROMPT,
    claude_cost_usd,
    estimate_world,
    generate_storyboard,
)

ARTIST = "Mika Starlight"

# Mirrors the demo song (ARCHITECTURE.md §1): intro 0–8, verse 8–24, chorus 24–40, bridge 40–48, chorus 48–60.
ANALYSIS: dict[str, Any] = {
    "duration": 60.0,
    "bpm": 120.0,
    "beats": [round(i * 0.5, 3) for i in range(120)],
    "downbeats": [float(i * 2) for i in range(30)],
    "sections": [
        {"start": 0.0, "end": 8.0, "label": "intro", "energy": 0.3, "is_chorus_candidate": False},
        {"start": 8.0, "end": 24.0, "label": "verse", "energy": 0.55, "is_chorus_candidate": False},
        {"start": 24.0, "end": 40.0, "label": "chorus", "energy": 0.85, "is_chorus_candidate": True},
        {"start": 40.0, "end": 48.0, "label": "bridge", "energy": 0.35, "is_chorus_candidate": False},
        {"start": 48.0, "end": 60.0, "label": "chorus", "energy": 0.8, "is_chorus_candidate": True},
    ],
}
_LYRIC_TEXTS = [
    "夜明け前の街を ひとりで歩いた", "消えそうな光を 両手で包んで", "名前も知らない 風に吹かれて",
    "それでも僕らは 前を向いていた", "叫べ 今この瞬間を", "忘れないように 胸に刻んで",
    "走れ 遠く遥か先へ", "君と見た空を 信じているから", "叫べ 今この瞬間を", "忘れないように 胸に刻んで",
]  # fmt: skip
_LYRIC_STARTS = [8, 12, 16, 20, 24, 28, 32, 36, 48, 52]
LYRICS = [
    {"id": f"l{i + 1:03d}", "text": text, "start": float(start), "end": start + 3.6, "words": None,
     "source": "lrc", "confidence": None}
    for i, (text, start) in enumerate(zip(_LYRIC_TEXTS, _LYRIC_STARTS, strict=True))
]  # fmt: skip


def _template(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "title": "夜明けの歌", "artist_name": ARTIST, "mood": "切ない、疾走感", "purpose": "新曲のプロモーション",
        "lyrics": LYRICS, "analysis": ANALYSIS, "preset_id": "cinematic", "custom_world": None,
        "n_scenes": 8, "seed": 11, "use_claude": False,
    }  # fmt: skip
    kwargs.update(overrides)
    return generate_storyboard(**kwargs)


def _all_text(storyboard: dict[str, Any]) -> str:
    return json.dumps(storyboard, ensure_ascii=False)


# --- template ---------------------------------------------------------------------------------------------


def test_template_is_deterministic_for_a_seed() -> None:
    assert _template() == _template()
    assert _template(seed=11)["scenes"] != _template(seed=12)["scenes"]
    # Without a seed the result is still stable (derived from title/preset).
    assert _template(seed=None) == _template(seed=None)


@pytest.mark.parametrize("preset_id", list(WORLD_PRESETS))
def test_template_world_bible_is_shared_by_all_scenes(preset_id: str) -> None:
    board = _template(
        preset_id=preset_id, custom_world="雨の夜の東京、ネオンと水たまり" if preset_id == "custom" else None
    )
    schemas.Storyboard.model_validate(board)
    assert board["source"] == "template" and board["model"] is None and board["cost_usd"] == 0.0
    world = board["world_bible"]
    assert world["protagonist_en"].startswith("a fictional ")
    assert world["palette"] == WORLD_PRESETS[preset_id]["palette"]
    assert not world["summary_ja"].isascii()
    scenes = board["scenes"]
    assert [s["id"] for s in scenes] == [f"s{i:02d}" for i in range(1, 9)]
    for scene in scenes:
        prompt = scene["prompt_en"]
        assert world["consistency_tokens_en"] in prompt, "shared look tokens appear in every prompt"
        if SCENE_TYPES[scene["scene_type"]]["has_subject"]:
            assert world["protagonist_en"] in prompt, "same protagonist description in every protagonist scene"
        assert "Vertical 9:16 composition" in prompt and "photorealistic" in prompt
        assert "natural skin texture" in prompt and "realistic hands" in prompt and "subtle natural motion" in prompt
        assert prompt.count(LIKENESS_SAFETY_CLAUSE) == 1 and prompt.count(NO_TEXT_CLAUSE) == 1
        assert len(prompt) <= STORYBOARD_PROMPT_CHARS and prompt.isascii()
        negative = scene["negative_prompt_en"]
        for item in ("deformed hands", "extra fingers", "warped face", "morphing", "flicker", "text", "watermark",
                     "cartoon", "CGI", "oversaturated"):  # fmt: skip
            assert item in negative
        assert scene["scene_type"] in WORLD_PRESETS[preset_id]["scene_types"]
        assert scene["camera_angle"] in CAMERA_ANGLES and scene["camera_move"] in CAMERA_MOVES
        assert not scene["description_ja"].isascii() and 4 <= scene["duration_hint"] <= 6


def test_template_varies_scenes_and_camera() -> None:
    scenes = _template()["scenes"]
    for a, b in itertools.pairwise(scenes):
        assert a["scene_type"] != b["scene_type"]
        assert a["camera_move"] != b["camera_move"]
    assert len({s["scene_type"] for s in scenes}) >= 6
    assert len({s["camera_move"] for s in scenes}) >= 5
    assert any(not SCENE_TYPES[s["scene_type"]]["has_subject"] for s in scenes)
    assert sum(SCENE_TYPES[s["scene_type"]]["has_subject"] for s in scenes) >= len(scenes) // 2


def test_template_maps_scenes_to_sections_and_lyrics() -> None:
    scenes = _template(n_scenes=10)["scenes"]
    sections = ANALYSIS["sections"]
    assert [s["start"] for s in scenes] == sorted(s["start"] for s in scenes)
    assert {s["section"] for s in scenes} == {"intro", "verse", "chorus", "bridge"}
    lyric_by_id = {line["id"]: line for line in LYRICS}
    for scene in scenes:
        owner = next(sec for sec in sections if sec["start"] <= scene["start"] < sec["end"])
        assert owner["label"] == scene["section"] and scene["end"] <= owner["end"] + 1e-6
        if scene["lyric_ref"] is not None:
            line = lyric_by_id[scene["lyric_ref"]]
            assert line["start"] < scene["end"] and line["end"] > scene["start"]
            assert "歌詞" in scene["description_ja"]
    assert scenes[0]["section"] == "intro" and scenes[0]["lyric_ref"] is None
    chorus = [s for s in scenes if s["section"] == "chorus"]
    intro = [s for s in scenes if s["section"] == "intro"]
    assert len(chorus) >= 3
    assert all(s["duration_hint"] == 4 for s in chorus) and all(s["duration_hint"] == 6 for s in intro)
    high_energy = {"high", "mid"}
    assert all(SCENE_TYPES[s["scene_type"]]["energy"] in high_energy for s in chorus)


def test_template_without_analysis_and_with_untimed_lyrics() -> None:
    untimed = [{**line, "start": None, "end": None} for line in LYRICS]
    board = _template(analysis=None, lyrics=untimed, n_scenes=6)
    assert any("楽曲解析" in w for w in board["warnings"])
    scenes = board["scenes"]
    assert len(scenes) == 6 and scenes[0]["section"] == "intro" and scenes[-1]["section"] == "outro"
    refs = [s["lyric_ref"] for s in scenes if s["lyric_ref"]]
    assert refs == sorted(refs) and len(refs) >= 3
    assert all(s["start"] is None for s in scenes)


def test_template_never_leaks_the_artist_name() -> None:
    lyrics = [*LYRICS, {"id": "l099", "text": f"{ARTIST}と夜の海", "start": 30.0, "end": 33.0}]
    board = _template(
        mood=f"{ARTIST}みたいな雰囲気", custom_world=f"{ARTIST}本人が海辺に立つ", preset_id="custom", lyrics=lyrics
    )
    text = _all_text(board).lower()
    assert "mika" not in text and "starlight" not in text


def test_latin_words_from_lyrics_and_mood_are_not_copied_into_prompts() -> None:
    lyrics = [{"id": "l001", "text": "Johnny Doe と夜の海を歩いた", "start": 0.0, "end": 60.0}]
    board = _template(lyrics=lyrics, mood="Johnny Doe 風の切ない夜", n_scenes=3)
    # Everything sent to a video model (summary_ja only echoes the user's mood text in the UI).
    english = {k: v for k, v in board["world_bible"].items() if k.endswith("_en")}
    text = json.dumps([english, [(s["prompt_en"], s["negative_prompt_en"]) for s in board["scenes"]]])
    assert "Johnny" not in text and "Doe" not in text
    assert any("night" in s["prompt_en"] and "bittersweet" in s["prompt_en"] for s in board["scenes"])
    assert any("Evoking" in s["prompt_en"] and "ocean" in s["prompt_en"] for s in board["scenes"])


def test_custom_world_is_translated_into_prompts() -> None:
    board = _template(preset_id="custom", custom_world="雨の夜の東京、ネオンと水たまり")
    assert any("簡易辞書" in w for w in board["warnings"])
    assert "Tokyo" in board["world_bible"]["look_en"] and "neon lights" in board["world_bible"]["look_en"]
    assert all("Setting: rain night Tokyo, neon lights" in s["prompt_en"] for s in board["scenes"])
    empty = _template(preset_id="custom", custom_world=None)
    assert any("カスタム世界観が未入力" in w for w in empty["warnings"])


def test_scene_count_is_clamped() -> None:
    assert len(_template(n_scenes=0)["scenes"]) == 8  # default
    assert len(_template(n_scenes=99)["scenes"]) == 30
    assert len(_template(n_scenes=1)["scenes"]) == 1
    assert len(_template(n_scenes=3)["scenes"]) == 3


# --- Claude ---------------------------------------------------------------------------------------------------


def _schema_objects(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("type") == "object":
            found.append(node)
        for value in node.values():
            found.extend(_schema_objects(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_schema_objects(value))
    return found


def test_storyboard_schema_is_strict() -> None:
    objects = _schema_objects(STORYBOARD_SCHEMA)
    assert len(objects) == 3
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])
    scene = STORYBOARD_SCHEMA["properties"]["scenes"]["items"]["properties"]
    assert scene["scene_type"]["enum"] == list(SCENE_TYPES)


def _claude_storyboard(n: int, *, artist_in_prompt: bool = False) -> dict[str, Any]:
    protagonist = "a fictional woman in her late twenties with short silver hair, wearing a red raincoat"
    scenes = []
    for i in range(n):
        prompt = (
            f"Medium shot of {protagonist}, walking along a rainy harbor at night, neon reflections on wet stones, "
            "slow dolly push-in, 35mm anamorphic lens, vertical 9:16 composition, photorealistic live-action footage, "
            "natural skin texture, realistic hands, subtle natural motion"
        )
        if artist_in_prompt and i == 0:
            prompt = f"{ARTIST} lookalike, " + prompt
        scenes.append(
            {
                "section": "chorus" if i % 2 else "verse",
                "lyric_ref": LYRICS[i % len(LYRICS)]["id"] if i % 3 else "unknown-id",
                "scene_type": "walking",
                "camera_angle": "eye_level",
                "camera_move": "slow_push_in",
                "description_ja": "雨の港を歩く主人公",
                "prompt_en": prompt,
                "negative_prompt_en": "text, watermark, extra fingers",
                "duration_hint": 5,
            }
        )
    return {
        "world_bible": {
            "summary_ja": "雨の港町。赤いレインコートの主人公。",
            "look_en": "neon reflections, 35mm anamorphic, Kodak Vision3 500T",
            "protagonist_en": protagonist,
            "location_en": "a rainy harbor town at night",
            "palette": ["#0b0f2b", "#ff2e88", "#2ee6ff"],
            "consistency_tokens_en": "red raincoat, silver hair, magenta and cyan palette, 500T film look",
        },
        "scenes": scenes,
    }


class _Recorder:
    def __init__(self, respond: Callable[[httpx2.Request], httpx2.Response]):
        self.respond = respond
        self.requests: list[tuple[dict[str, Any], dict[str, str]]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append((json.loads(request.content), dict(request.headers)))
        return self.respond(request)

    def client(self) -> anthropic.Anthropic:
        return anthropic.Anthropic(api_key="test-key", http_client=httpx2.Client(transport=httpx2.MockTransport(self)),
                                   max_retries=0)  # fmt: skip


def _message(text: str | None, *, stop_reason: str = "end_turn", model: str = "claude-opus-5-5") -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "thinking", "thinking": "", "signature": "sig"}]
    if text is not None:
        content.append({"type": "text", "text": text})
    return {
        "id": "msg_test", "type": "message", "role": "assistant", "model": model, "content": content,
        "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 3000, "output_tokens": 5000, "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0},
    }  # fmt: skip


def _claude(recorder: _Recorder, **overrides: Any) -> dict[str, Any]:
    return _template(use_claude=True, client=recorder.client(), **overrides)


def test_claude_success() -> None:
    recorder = _Recorder(
        lambda r: httpx2.Response(200, json=_message(json.dumps(_claude_storyboard(10, artist_in_prompt=True))))
    )
    board = _claude(recorder, n_scenes=6)
    schemas.Storyboard.model_validate(board)
    assert board["source"] == "claude" and board["model"] == "claude-opus-5-5"
    assert board["usage"] == {"input_tokens": 3000, "output_tokens": 5000, "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0}  # fmt: skip
    assert board["cost_usd"] == pytest.approx(3000 * 4 / 1e6 + 5000 * 20 / 1e6)
    assert len(board["scenes"]) == 6, "extra scenes are truncated"
    world = board["world_bible"]
    assert world["palette"] == ["#0B0F2B", "#FF2E88", "#2EE6FF"]
    for scene in board["scenes"]:
        prompt = scene["prompt_en"]
        assert world["consistency_tokens_en"] in prompt and "9:16" in prompt
        assert prompt.count(LIKENESS_SAFETY_CLAUSE) == 1 and prompt.count(NO_TEXT_CLAUSE) == 1
        assert "deformed hands" in scene["negative_prompt_en"] and "extra fingers" in scene["negative_prompt_en"]
        assert scene["lyric_ref"] is None or scene["lyric_ref"].startswith("l0")
    assert "mika" not in board["scenes"][0]["prompt_en"].lower()
    assert "lookalike" not in board["scenes"][0]["prompt_en"].lower()
    assert any("Mika Starlight" in w for w in board["warnings"])

    body, headers = recorder.requests[0]
    assert headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert body["model"] == "claude-opus-5-5" and body["max_tokens"] == 16000 and body["fallbacks"] == "default"
    assert body["output_config"] == {"format": {"type": "json_schema", "schema": STORYBOARD_SCHEMA}, "effort": "low"}
    assert body["system"] == [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
    assert ARTIST not in SYSTEM_PROMPT
    user = json.loads(body["messages"][0]["content"])
    assert user["forbidden_likeness"]["names"] == [ARTIST]
    assert ARTIST not in json.dumps({k: v for k, v in user.items() if k != "forbidden_likeness"}, ensure_ascii=False)
    assert user["task"].startswith("Create a storyboard with exactly 6 scenes")
    assert [line["id"] for line in user["lyrics"]] == [line["id"] for line in LYRICS]
    assert [s["label"] for s in user["song"]["sections"]] == ["intro", "verse", "chorus", "bridge", "chorus"]


def test_claude_receives_artist_name_only_as_forbidden_likeness() -> None:
    recorder = _Recorder(lambda r: httpx2.Response(200, json=_message(json.dumps(_claude_storyboard(3)))))
    lyrics = [*LYRICS, {"id": "l099", "text": f"{ARTIST}と夜の海", "start": 30.0, "end": 33.0}]
    _claude(recorder, n_scenes=3, title=f"{ARTIST}のテーマ", mood=f"{ARTIST}みたいな雰囲気",
            custom_world=f"{ARTIST}本人が海辺に立つ", preset_id="custom", lyrics=lyrics)  # fmt: skip
    user = json.loads(recorder.requests[0][0]["messages"][0]["content"])
    assert user["forbidden_likeness"]["names"] == [ARTIST]
    rest = json.dumps({k: v for k, v in user.items() if k != "forbidden_likeness"}, ensure_ascii=False).lower()
    assert "mika" not in rest and "starlight" not in rest
    assert user["world_preset"]["custom_world_ja"] == "本人が海辺に立つ"


def test_claude_system_prompt_is_stable_across_songs() -> None:
    recorder = _Recorder(lambda r: httpx2.Response(200, json=_message(json.dumps(_claude_storyboard(3)))))
    _claude(recorder, n_scenes=3)
    _claude(recorder, n_scenes=5, title="別の曲", artist_name="Other Artist", preset_id="dreamy", lyrics=[])
    first, second = recorder.requests[0][0], recorder.requests[1][0]
    assert first["system"] == second["system"]
    assert first["messages"] != second["messages"]


def test_claude_fewer_scenes_than_requested_warns() -> None:
    recorder = _Recorder(lambda r: httpx2.Response(200, json=_message(json.dumps(_claude_storyboard(2)))))
    board = _claude(recorder, n_scenes=5)
    assert board["source"] == "claude" and len(board["scenes"]) == 2
    assert any("2 シーン" in w for w in board["warnings"])


def test_claude_refusal_falls_back_to_template() -> None:
    recorder = _Recorder(lambda r: httpx2.Response(200, json=_message(None, stop_reason="refusal")))
    board = _claude(recorder)
    assert board["source"] == "template" and len(board["scenes"]) == 8
    assert any("見送った" in w for w in board["warnings"])
    # Tokens of the declined call are still recorded (pipeline books the LLM cost).
    assert board["model"] == "claude-opus-5-5" and board["usage"]["output_tokens"] == 5000
    assert board["cost_usd"] > 0
    assert board["scenes"] == _template()["scenes"], "fallback equals the deterministic template"


@pytest.mark.parametrize(
    "text", ["not json at all {", json.dumps({"world_bible": {}, "scenes": []}), json.dumps([1, 2])]
)
def test_claude_invalid_json_falls_back_to_template(text: str) -> None:
    recorder = _Recorder(lambda r: httpx2.Response(200, json=_message(text)))
    board = _claude(recorder)
    assert board["source"] == "template"
    assert any("解析できなかった" in w for w in board["warnings"])


@pytest.mark.parametrize(
    ("status", "error_type", "expected"),
    [
        (429, "rate_limit_error", "利用上限"),
        (500, "api_error", "HTTP 500"),
        (529, "overloaded_error", "HTTP 529"),
        (400, "invalid_request_error", "受け付けなかった"),
    ],
)
def test_claude_api_errors_fall_back_to_template(status: int, error_type: str, expected: str) -> None:
    payload = {"type": "error", "error": {"type": error_type, "message": "failure"}}
    recorder = _Recorder(lambda r: httpx2.Response(status, json=payload))
    board = _claude(recorder)
    assert board["source"] == "template" and board["model"] is None and board["cost_usd"] == 0.0
    assert any(expected in w for w in board["warnings"]), board["warnings"]
    assert len(recorder.requests) == 1


def test_claude_connection_error_falls_back_to_template() -> None:
    def fail(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("network unreachable", request=request)

    board = _claude(_Recorder(fail))
    assert board["source"] == "template"
    assert any("接続できなかった" in w for w in board["warnings"])


def test_claude_not_used_when_disabled_or_unconfigured() -> None:
    recorder = _Recorder(lambda r: httpx2.Response(500))
    board = _template(use_claude=False, client=recorder.client())
    assert board["source"] == "template" and recorder.requests == []
    # conftest blanks ANTHROPIC_API_KEY: use_claude=True without a client means template, no warning.
    board = _template(use_claude=True)
    assert board["source"] == "template" and board["warnings"] == []


def test_claude_cost_pricing() -> None:
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert claude_cost_usd("claude-opus-5-5", usage) == pytest.approx(24.0)
    assert claude_cost_usd("claude-opus-5-5", {"cache_read_input_tokens": 1_000_000}) == pytest.approx(0.2)
    assert claude_cost_usd("claude-opus-5-5", {"cache_creation_input_tokens": 1_000_000}) == pytest.approx(5.0)
    assert claude_cost_usd(None, None) == 0.0


# --- real people in free text (likeness guard) -------------------------------------------------------------


def _provider_text(board: dict[str, Any]) -> str:
    """Everything that can reach a video model."""
    english = {k: v for k, v in board["world_bible"].items() if k.endswith("_en")}
    return json.dumps([english, [(s["prompt_en"], s["negative_prompt_en"]) for s in board["scenes"]]])


@pytest.mark.parametrize("aliases", [None, ["Kenshi Yonezu"]])
def test_romanized_artist_name_in_custom_world_never_reaches_prompts(aliases: list[str] | None) -> None:
    board = _template(artist_name="米津玄師", artist_aliases=aliases, preset_id="custom", n_scenes=4,
                      custom_world="Kenshi Yonezu singing on a Tokyo rooftop at night")  # fmt: skip
    text = _provider_text(board)
    assert "Kenshi" not in text and "Yonezu" not in text
    assert all("Setting: singing on a Tokyo rooftop at night" in s["prompt_en"] for s in board["scenes"])
    assert any("カスタム世界観から" in w for w in board["warnings"])


def test_kana_artist_name_is_forbidden_in_romaji_too() -> None:
    board = _template(artist_name="あいみょん", preset_id="custom", custom_world="Aimyon walking by the sea",
                      n_scenes=3)  # fmt: skip
    assert "aimyon" not in _provider_text(board).lower()
    assert any("アーティスト名を削除" in w for w in board["warnings"])


@pytest.mark.parametrize(
    ("custom_world", "names"),
    [
        ("Taylor Swift singing on a neon rooftop in Tokyo", ["Taylor", "Swift"]),
        ("a man who looks exactly like Keanu Reeves walking in rain", ["Keanu", "Reeves"]),
        ("YOASOBI style, Lilas Ikuta dancing in the rain", ["Lilas", "Ikuta"]),
        ("木村拓哉似の男性が雨の東京を歩く", ["trees", "木村"]),  # the glossary used to turn 木 into "trees"
        ("渋谷で Kenshi Yonezu が歩く夜", ["Kenshi", "Yonezu"]),
    ],
)
def test_other_real_people_in_custom_world_are_removed(custom_world: str, names: list[str]) -> None:
    board = _template(artist_name="Someone Else", preset_id="custom", custom_world=custom_world, n_scenes=3)
    text = _provider_text(board)
    assert not any(name in text for name in names), text
    assert any("削除しました" in w for w in board["warnings"])


def test_ascii_mood_with_a_person_is_not_copied_into_prompts() -> None:
    board = _template(mood="Billie Eilish vibes", n_scenes=3)
    assert "Billie" not in _provider_text(board) and "Eilish" not in _provider_text(board)
    assert any("Billie Eilish" in w for w in board["warnings"])
    moody = _template(mood="dark, moody and cinematic", n_scenes=3)
    assert all("dark, moody, cinematic mood" in s["prompt_en"] for s in moody["scenes"])


def test_claude_gets_names_from_free_text_as_forbidden_and_its_detected_people_are_removed() -> None:
    data = _claude_storyboard(3)
    data["detected_real_people"] = ["Kenshi Yonezu", "Taylor Swift"]
    data["scenes"][0]["prompt_en"] = "Taylor Swift on a rooftop, " + data["scenes"][0]["prompt_en"]
    tokens = data["world_bible"]["consistency_tokens_en"]
    data["world_bible"]["consistency_tokens_en"] = f"Kenshi Yonezu concert look, {tokens}"
    recorder = _Recorder(lambda r: httpx2.Response(200, json=_message(json.dumps(data))))
    board = _claude(recorder, n_scenes=3, artist_name="米津玄師", artist_aliases=["Kenshi Yonezu"],
                    mood="Billie Eilish vibes", preset_id="custom",
                    custom_world="a man who looks exactly like Keanu Reeves on a rooftop")  # fmt: skip
    user = json.loads(recorder.requests[0][0]["messages"][0]["content"])
    names = user["forbidden_likeness"]["names"]
    assert names[:2] == ["米津玄師", "Kenshi Yonezu"] and "Billie Eilish" in names and "Keanu Reeves" in names
    rest = json.dumps({k: v for k, v in user.items() if k != "forbidden_likeness"}, ensure_ascii=False)
    assert "Keanu" not in rest and "Billie" not in rest and "Yonezu" not in rest
    text = _provider_text(board)
    assert "Taylor" not in text and "Kenshi" not in text and "Yonezu" not in text
    # The cleaned consistency tokens still appear verbatim in every prompt.
    assert all(board["world_bible"]["consistency_tokens_en"] in s["prompt_en"] for s in board["scenes"])
    assert board["world_bible"]["consistency_tokens_en"].endswith(tokens)
    assert any("Claude が実在の人物名を 1 件検出" in w for w in board["warnings"])
    assert "detected_real_people" in STORYBOARD_SCHEMA["required"]
    assert "detected_real_people" in SYSTEM_PROMPT


# --- emotion / world estimate (感情・世界観推定) ------------------------------------------------------------

_ENERGETIC = {"energy": 0.9, "tempo_class": "fast", "brightness": 0.7, "mode_guess": "major",
              "tags": ["energetic", "upbeat", "bright", "uplifting", "danceable"]}  # fmt: skip
_MELANCHOLIC = {"energy": 0.1, "tempo_class": "slow", "brightness": 0.2, "mode_guess": "minor",
                "tags": ["calm", "slow", "dark", "melancholic"]}  # fmt: skip


def test_estimate_world_maps_mood_to_presets() -> None:
    sad = estimate_world({"mood": _MELANCHOLIC}, [], None, None)
    assert {"melancholic", "calm"} <= set(sad["emotions"])
    assert sad["recommended_presets"][0] in ("emotional", "nostalgic")
    loud = estimate_world({"mood": _ENERGETIC}, [], None, None)
    assert loud["emotions"][0] == "energetic"
    assert set(loud["recommended_presets"][:2]) & {"urban_night", "cinematic"}
    assert loud["rationale_ja"].startswith("音源:") and "おすすめ" in loud["rationale_ja"]
    # Lyrics and the user's input are combined with the audio (the user's words weigh most).
    lyrics = [{"text": "涙の別れ さよなら 思い出の帰り道"}]
    merged = estimate_world({"mood": _ENERGETIC}, lyrics, "懐かしい夏", None)
    assert merged["emotions"][0] == "nostalgic" and merged["recommended_presets"][0] == "nostalgic"
    assert merged["sources"]["lyrics"] and merged["sources"]["user"] == ["nostalgic"]
    assert estimate_world(None, [], None, "ライブツアーの告知")["recommended_presets"] == ["documentary"]
    assert estimate_world(None, [], None, None)["recommended_presets"] == ["cinematic"]


def test_audio_mood_shapes_the_template_when_the_user_gave_no_mood() -> None:
    loud = _template(analysis={**ANALYSIS, "mood": _ENERGETIC}, mood=None)
    sad = _template(analysis={**ANALYSIS, "mood": _MELANCHOLIC}, mood=None)
    assert loud["scenes"] != sad["scenes"]
    assert all(f"{loud['world_estimate']['mood_en']} mood" in s["prompt_en"] for s in loud["scenes"])
    assert "calm" in sad["world_estimate"]["mood_en"] and "推定" in sad["world_bible"]["summary_ja"]
    # An explicit mood still wins over the estimate.
    assert _template(analysis={**ANALYSIS, "mood": _ENERGETIC})["scenes"] == _template()["scenes"]


def test_claude_receives_audio_mood_and_estimate() -> None:
    recorder = _Recorder(lambda r: httpx2.Response(200, json=_message(json.dumps(_claude_storyboard(3)))))
    board = _claude(recorder, n_scenes=3, analysis={**ANALYSIS, "mood": _MELANCHOLIC})
    song = json.loads(recorder.requests[0][0]["messages"][0]["content"])["song"]
    assert song["audio_mood"] == _MELANCHOLIC
    assert song["world_estimate"]["recommended_presets"] == board["world_estimate"]["recommended_presets"]
    schemas.Storyboard.model_validate(board)
