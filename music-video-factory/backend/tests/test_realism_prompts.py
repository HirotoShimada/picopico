"""Realism direction for video prompts ("なんか、映像がAI感強くない？", 2026-10-13).

Prompts must read like candid live-action footage: no stock AI-look vocabulary (cinematic, anamorphic,
bokeh, film grain, halation, 8k …), no palette names or hex codes, and wardrobe only where it is visible —
the sakura sample had "…white canvas sneakers" pasted into a sky shot (s05) and into a face close-up (s06),
and Veo drew a floating sneaker next to her hip. Covers the Claude path (new and legacy answer shapes), stored
storyboards (``upgrade_storyboard``), the template path and the Claude instructions.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx2
import pytest

from app import schemas
from app.services.presets import (
    FRAMING_REGIONS,
    FRAMINGS,
    FRAMINGS_WITH_FACE,
    GARMENT_REGIONS,
    SCENE_TYPES,
    WORLD_PRESETS,
)
from app.services.prompting import (
    LIKENESS_SAFETY_CLAUSE,
    NO_TEXT_CLAUSE,
    strip_ai_cliches,
)
from app.services.storyboard import (
    BASE_NEGATIVE_EN,
    MAX_NEGATIVE_CHARS,
    STORYBOARD_PROMPT_CHARS,
    STORYBOARD_SCHEMA,
    SYSTEM_PROMPT,
    _merge_negative,
    _user_message,
    generate_storyboard,
    upgrade_storyboard,
)

_FOOTWEAR = re.compile(r"\b(?:sneakers?|shoes?|boots?|sandals?|flats|heels|loafers)\b", re.IGNORECASE)
_BOTTOMS = re.compile(r"\b(?:skirt|trousers|jeans|shorts|chinos|pants)\b", re.IGNORECASE)
_LOOK_WORDS = re.compile(
    r"cinematic|anamorphic|bokeh|lens flare|halation|\bgrain\b|film look|\b\d+\s?mm\b|\b8k\b|masterpiece|"
    r"ultra-detailed|#[0-9a-f]{6}|palette|forest green|blossom pink|sky blue",
    re.IGNORECASE,
)


_SAKURA_WORLD: dict[str, Any] = {
    "summary_ja": "満開の桜並木と霞たなびく春の丘陵を、一人の若い女性が歩く。",
    "look_en": (
        "natural spring sunlight, soft morning light and golden hour glow, 24mm wide-angle lens with "
        "natural perspective and deep focus, clean high-dynamic-range cinematic look with natural "
        "colors, gentle fine grain, soft halation on highlights, pale mist drifting over hills, "
        "cherry petals carried by the breeze"
    ),
    "protagonist_en": (
        "a fictional Japanese woman in her mid-20s with shoulder-length straight black hair, wearing "
        "a light beige linen trench coat over a soft white cotton blouse, a long sage-green pleated "
        "skirt and white canvas sneakers"
    ),
    "location_en": (
        "a rolling Japanese countryside of blooming cherry blossom avenues, misty green hills and a "
        "calm river under a wide spring sky"
    ),
    "palette": ["#2F5D3A", "#F3C6D3", "#A8C5DA"],
    "consistency_tokens_en": (
        "beige linen trench coat, white cotton blouse, sage-green pleated skirt, white canvas "
        "sneakers; forest green, blossom pink, sky blue; soft fine grain, gentle halation, spring "
        "mist"
    ),
}
# s04-s07 exactly as stored by the 2026-10-13 sample batch (Claude via OpenRouter, nature preset).
_SAKURA_SCENES: list[dict[str, Any]] = [
    {
        "id": "s04",
        "section": "verse",
        "lyric_ref": "l003",
        "scene_type": "nature_macro",
        "camera_angle": "through_foreground",
        "camera_move": "rack_focus",
        "description_ja": "露をまとった桜の花びらが微風に揺れるマクロ映像。",
        "prompt_en": (
            "Macro shot of cherry blossom petals and tiny dew drops trembling in a light breeze on a "
            "branch, shallow depth of field, soft morning backlight glowing through petals, shot through "
            "out-of-focus foreground blossoms, static camera with gentle rack focus, macro lens, forest "
            "green, blossom pink, sky blue, fine grain, vertical 9:16 composition, photorealistic "
            "live-action footage, subtle natural motion, no people, soft halation, spring mist. beige "
            "linen trench coat, white cotton blouse, sage-green pleated skirt, white canvas sneakers; "
            "forest green, blossom pink, sky blue; soft fine grain, gentle halation, spring mist. "
            "fictional characters only, no real or identifiable people, no celebrities, no text, no "
            "captions, no watermark."
        ),
        "duration_hint": 4.0,
        "negative_prompt_en": "text, watermark, cartoon, CGI",
    },
    {
        "id": "s05",
        "section": "verse",
        "lyric_ref": "l004",
        "scene_type": "sky_drift",
        "camera_angle": "low_angle",
        "camera_move": "tilt_up",
        "description_ja": "桜の枝の先、霞か雲か分からないほど柔らかな春の空がゆっくり流れる。",
        "prompt_en": (
            "Sky above cherry blossom branches slowly shifting, hazy clouds and soft spring mist drifting "
            "together so haze and cloud blend, light gently warming, low-angle camera slow tilt up from "
            "pink branches to pale sky, 24mm lens, forest green, blossom pink, sky blue, fine grain, "
            "vertical 9:16 composition, photorealistic live-action footage, subtle natural motion, no "
            "people, soft halation, spring mist. beige linen trench coat, white cotton blouse, sage-green "
            "pleated skirt, white canvas sneakers; forest green, blossom pink, sky blue; soft fine grain, "
            "gentle halation, spring mist. fictional characters only, no real or identifiable people, no "
            "celebrities, no text, no captions, no watermark."
        ),
        "duration_hint": 4.0,
        "negative_prompt_en": "text, watermark, cartoon, CGI",
    },
    {
        "id": "s06",
        "section": "verse",
        "lyric_ref": "l004",
        "scene_type": "close_portrait",
        "camera_angle": "through_foreground",
        "camera_move": "slow_push_in",
        "description_ja": "桜越しに捉えた主人公の横顔、穏やかな息と遠くを見る眼差し。",
        "prompt_en": (
            "Intimate close-up of a fictional Japanese woman in her mid-20s with shoulder-length straight "
            "black hair, eyes glancing away toward the misty sky, a slow calm breath, petals drifting "
            "past, cherry avenue softly out of focus, warm soft side light, shot through out-of-focus "
            "foreground blossoms, slow dolly push-in, 50mm feel, forest green, blossom pink, sky blue, "
            "fine grain, vertical 9:16 composition, photorealistic live-action footage, natural skin "
            "texture, subtle natural motion, beige linen trench coat, white cotton blouse; soft halation, "
            "spring mist. beige linen trench coat, white cotton blouse, sage-green pleated skirt, white "
            "canvas sneakers; forest green, blossom pink, sky blue; soft fine grain, gentle halation, "
            "spring mist. fictional characters only, no real or identifiable people, no celebrities, no "
            "text, no captions, no watermark."
        ),
        "duration_hint": 4.0,
        "negative_prompt_en": "text, watermark, cartoon, CGI",
    },
    {
        "id": "s07",
        "section": "chorus",
        "lyric_ref": None,
        "scene_type": "running",
        "camera_angle": "eye_level",
        "camera_move": "tracking_side",
        "description_ja": "桜並木の下を主人公が笑顔で駆け抜け、花びらが舞い上がる。",
        "prompt_en": (
            "Full shot of a fictional Japanese woman in her mid-20s with shoulder-length straight black "
            "hair running joyfully along a cherry blossom path by a calm river, hair and coat moving "
            "naturally, petals lifting in her wake, golden hour sunlight, eye-level smooth lateral "
            "tracking shot, 24mm lens, forest green, blossom pink, sky blue, fine grain, vertical 9:16 "
            "composition, photorealistic live-action footage, natural skin texture, realistic hands, "
            "subtle natural motion, beige linen trench coat, white cotton blouse, sage-green pleated "
            "skirt, white canvas sneakers; soft halation, spring mist. beige linen trench coat, white "
            "cotton blouse, sage-green pleated skirt, white canvas sneakers; forest green, blossom pink, "
            "sky blue; soft fine grain, gentle halation, spring mist. fictional characters only, no real "
            "or identifiable people, no celebrities, no text, no captions, no watermark."
        ),
        "duration_hint": 5.0,
        "negative_prompt_en": "text, watermark, cartoon, CGI",
    },
]


def _sakura_board() -> dict[str, Any]:
    return {
        "source": "claude", "route": "openrouter", "model": "anthropic/claude-opus-5.5", "usage": None,
        "cost_usd": 0.05, "world_bible": dict(_SAKURA_WORLD), "scenes": [dict(s) for s in _SAKURA_SCENES],
        "warnings": [], "preset": "nature", "seed": None,
    }  # fmt: skip


def _scene(board: dict[str, Any], scene_id: str) -> dict[str, Any]:
    return next(s for s in board["scenes"] if s["id"] == scene_id)


# --- stored (legacy) storyboards ------------------------------------------------------------------------------------


def test_sakura_sky_shot_and_close_up_lose_the_sneakers() -> None:
    before = _sakura_board()
    assert "white canvas sneakers" in _scene(before, "s05")["prompt_en"], "fixture reproduces the bug"
    assert "white canvas sneakers" in _scene(before, "s06")["prompt_en"]

    board = upgrade_storyboard(before)
    schemas.Storyboard.model_validate(board)
    world = board["world_bible"]
    assert world["identity_en"] == "a fictional Japanese woman in her mid-20s with shoulder-length straight black hair"
    assert [(g["region"], g["item_en"]) for g in world["wardrobe"]] == [
        ("outer", "light beige linen trench coat"),
        ("top", "soft white cotton blouse"),
        ("bottom", "long sage-green pleated skirt"),
        ("feet", "white canvas sneakers"),
    ]
    assert world["palette"] == _SAKURA_WORLD["palette"]  # kept for the app's interface

    sky = _scene(board, "s05")
    assert sky["framing"] == "no_people"
    assert not _FOOTWEAR.search(sky["prompt_en"]) and not _BOTTOMS.search(sky["prompt_en"])
    assert not re.search(r"trench coat|blouse|fictional japanese woman", sky["prompt_en"], re.IGNORECASE)
    assert "no people" in sky["prompt_en"] and "realistic hands" not in sky["prompt_en"]
    assert sky["prompt_en"].startswith("Sky above cherry blossom branches slowly shifting")

    face = _scene(board, "s06")
    assert face["framing"] == "close_up"
    assert not _FOOTWEAR.search(face["prompt_en"]) and not _BOTTOMS.search(face["prompt_en"])
    assert "fictional Japanese woman in her mid-20s" in face["prompt_en"] and "trench coat" in face["prompt_en"]

    macro = _scene(board, "s04")
    assert macro["framing"] == "no_people" and not re.search(r"coat|blouse|skirt|sneakers", macro["prompt_en"])

    run = _scene(board, "s07")
    assert run["framing"] == "full_body"  # "Full shot of …": the whole outfit, shoes included
    assert "white canvas sneakers" in run["prompt_en"] and "sage-green pleated skirt" in run["prompt_en"]
    assert run["prompt_en"].count("white canvas sneakers") == 1, "the blob is not pasted on top"

    for scene in board["scenes"]:
        prompt = scene["prompt_en"]
        assert not _LOOK_WORDS.search(prompt), prompt
        assert "Vertical 9:16" in prompt and len(prompt) <= STORYBOARD_PROMPT_CHARS
        assert prompt.count(LIKENESS_SAFETY_CLAUSE) == 1 and prompt.count(NO_TEXT_CLAUSE) == 1
        assert scene["scene_type"] == next(s for s in _SAKURA_SCENES if s["id"] == scene["id"])["scene_type"]


def test_upgrade_is_idempotent_and_leaves_current_storyboards_alone() -> None:
    once = upgrade_storyboard(_sakura_board())
    assert upgrade_storyboard(once) == once
    template = generate_storyboard(title="桜", artist_name="Someone", mood=None, purpose=None, lyrics=[],
                                   analysis=None, preset_id="nature", custom_world=None, n_scenes=4, seed=3,
                                   use_claude=False)  # fmt: skip
    assert upgrade_storyboard(template) == template
    original = _sakura_board()
    upgrade_storyboard(original)
    assert original == _sakura_board(), "the stored storyboard is not modified in place"


def test_upgrade_handles_a_legacy_world_without_a_wardrobe_description() -> None:
    board = {
        "source": "template",
        "world_bible": {"protagonist_en": "a fictional man in his thirties with a beard",
                        "consistency_tokens_en": "deep teal, warm amber color palette, film grain",
                        "palette": ["#000000", "#111111", "#222222"]},
        "scenes": [{"id": "s01", "scene_type": "close_portrait",
                    "prompt_en": "Cinematic live-action shot: intimate close-up of a fictional man in his thirties "
                                 "with a beard. deep teal, warm amber color palette, film grain."}],
    }  # fmt: skip
    upgraded = upgrade_storyboard(board)
    scene = upgraded["scenes"][0]
    assert upgraded["world_bible"]["wardrobe"] == [] and upgraded["world_bible"]["consistency_tokens_en"] == ""
    assert scene["framing"] == "close_up"
    assert scene["prompt_en"].startswith("Live-action shot: intimate close-up of a fictional man in his thirties")
    assert not _LOOK_WORDS.search(scene["prompt_en"]) and "teal" not in scene["prompt_en"]


# --- Claude (current answer shape) -----------------------------------------------------------------------------------

_WARDROBE = [
    {"region": "outer", "item_en": "faded navy cotton work jacket"},
    {"region": "top", "item_en": "grey crew-neck sweatshirt"},
    {"region": "bottom", "item_en": "black straight-leg jeans"},
    {"region": "feet", "item_en": "scuffed white sneakers"},
    {"region": "head", "item_en": "small silver hoop earrings"},
]
_IDENTITY = "a fictional woman in her late twenties with a short black bob, freckles and an ordinary face"


def _claude_answer(scenes: list[dict[str, Any]], **world: Any) -> dict[str, Any]:
    return {
        "world_bible": {
            "summary_ja": "雨上がりの住宅街で、普段着の女性が一日を過ごす。", "identity_en": _IDENTITY,
            "wardrobe": _WARDROBE, "location_en": "a quiet residential street after rain",
            "look_en": "flat overcast daylight, wet grey asphalt", "palette": ["#2B2D42", "#D9A066", "#EDF2F4"],
            **world,
        },
        "scenes": [
            {"section": "verse", "lyric_ref": None, "camera_angle": "eye_level", "camera_move": "handheld",
             "description_ja": "雨上がりの通り", "negative_prompt_en": "text, watermark", "duration_hint": 5, **scene}
            for scene in scenes
        ],
        "detected_real_people": [],
    }  # fmt: skip


def _claude_board(answer: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    from tests.test_storyboard import _message, _Recorder, _template

    recorder = _Recorder(lambda r: httpx2.Response(200, json=_message(json.dumps(answer))))
    kwargs = {"n_scenes": len(answer["scenes"]), **overrides}
    return _template(use_claude=True, client=recorder.client(), **kwargs)


def test_claude_prompts_get_identity_and_only_the_visible_garments() -> None:
    answer = _claude_answer([
        {"scene_type": "sky_drift", "framing": "no_people",
         "prompt_en": "Wide shot of the sky above wet rooftops and tangled power lines, clouds moving fast, no people"},
        {"scene_type": "close_portrait", "framing": "detail",
         "prompt_en": "Close-up of her hand in the pocket of the faded navy cotton work jacket, keys jingling"},
        {"scene_type": "close_portrait", "framing": "close_up",
         "prompt_en": "Close-up of the woman blowing on a paper cup of coffee under a bus shelter, steam rising"},
        {"scene_type": "train_window", "framing": "medium",
         "prompt_en": "Medium shot of the woman sitting on a station bench, checking the time, shifting her bag"},
        {"scene_type": "walking", "framing": "full_body",
         "prompt_en": "Full shot of the woman crossing a wet street at a zebra crossing, stepping around a puddle"},
    ])  # fmt: skip
    board = _claude_board(answer)
    assert board["source"] == "claude"
    schemas.Storyboard.model_validate(board)
    world = board["world_bible"]
    assert world["identity_en"] == _IDENTITY and world["wardrobe"] == _WARDROBE
    assert world["protagonist_en"].startswith(f"{_IDENTITY}, wearing a faded navy cotton work jacket over")
    assert world["consistency_tokens_en"] == ", ".join(g["item_en"] for g in _WARDROBE)

    by_framing = {scene["framing"]: scene["prompt_en"] for scene in board["scenes"]}
    assert list(by_framing) == ["no_people", "detail", "close_up", "medium", "full_body"]
    assert "fictional" not in by_framing["no_people"].split(LIKENESS_SAFETY_CLAUSE)[0]
    assert not re.search(r"jacket|sweatshirt|jeans|sneakers|earrings", by_framing["no_people"])
    assert "realistic hands" not in by_framing["no_people"]
    # A detail shot keeps the one garment Claude named and gets no identity sentence.
    assert "faded navy cotton work jacket" in by_framing["detail"] and _IDENTITY not in by_framing["detail"]
    assert not _FOOTWEAR.search(by_framing["detail"]) and "realistic hands" in by_framing["detail"]
    for framing in ("close_up", "medium", "full_body"):
        prompt = by_framing[framing]
        assert f"The woman is {_IDENTITY}" in prompt, framing  # bound to the shot's own subject
        for garment in _WARDROBE:
            assert (garment["item_en"] in prompt) == (garment["region"] in FRAMING_REGIONS[framing]), (framing, garment)
    assert "earrings" in by_framing["close_up"] and not _BOTTOMS.search(by_framing["close_up"])
    assert "black straight-leg jeans" in by_framing["medium"] and not _FOOTWEAR.search(by_framing["medium"])
    assert "scuffed white sneakers" in by_framing["full_body"]


def test_claude_rule_breaks_are_cleaned_up() -> None:
    answer = _claude_answer([
        # Wardrobe pasted into a sky shot, a hex code and look words.
        {"scene_type": "sky_drift", "framing": "medium",
         "prompt_en": "Epic sky over the rooftops at dusk, faded navy cotton work jacket, black straight-leg jeans, "
                      "scuffed white sneakers, #2B2D42, 35mm anamorphic lens, soft halation, 8k, masterpiece"},
        # Shoes and jeans in a face close-up, palette words, a dreamy glow.
        {"scene_type": "close_portrait", "framing": "close_up",
         "prompt_en": "Cinematic close-up of the woman laughing at a joke off-screen, wearing a faded navy cotton "
                      "work jacket, black straight-leg jeans and scuffed white sneakers, ethereal dreamy glow, teal "
                      "and orange color palette, shallow depth of field with creamy bokeh"},
        # Unknown framing value: derived from the shot size.
        {"scene_type": "walking", "framing": "panorama",
         "prompt_en": "Medium shot of the woman carrying two grocery bags up a narrow staircase, lens flare"},
    ])  # fmt: skip
    board = _claude_board(answer)
    sky, face, stairs = board["scenes"]
    assert sky["framing"] == "no_people", "no-people scene types never get a person"
    assert not re.search(r"jacket|jeans|sneakers|fictional", sky["prompt_en"].split(LIKENESS_SAFETY_CLAUSE)[0])
    assert sky["prompt_en"].startswith("Sky over the rooftops at dusk") and "no people" in sky["prompt_en"]
    assert face["framing"] == "close_up"
    assert "faded navy cotton work jacket" in face["prompt_en"]
    assert not _FOOTWEAR.search(face["prompt_en"]) and not _BOTTOMS.search(face["prompt_en"])
    assert face["prompt_en"].startswith("Close-up of the woman laughing at a joke off-screen")
    assert stairs["framing"] == "medium" and "black straight-leg jeans" in stairs["prompt_en"]
    assert not _FOOTWEAR.search(stairs["prompt_en"])
    for scene in board["scenes"]:
        assert not _LOOK_WORDS.search(scene["prompt_en"]), scene["prompt_en"]
        assert not re.search(r"\b(?:epic|ethereal|dreamy)\b", scene["prompt_en"], re.IGNORECASE)
    assert board["warnings"] == []  # style words are removed silently


def test_clothing_written_into_the_identity_is_split_off() -> None:
    answer = _claude_answer(
        [{"scene_type": "close_portrait", "framing": "close_up", "prompt_en": "Close-up of the woman yawning"},
         {"scene_type": "walking", "framing": "full_body", "prompt_en": "Full shot of the woman walking a dog"}],
        identity_en=f"{_IDENTITY}, wearing a grey hoodie, black jeans and white sneakers",
        wardrobe=[],
    )  # fmt: skip
    board = _claude_board(answer)
    world = board["world_bible"]
    assert world["identity_en"] == _IDENTITY
    assert [g["region"] for g in world["wardrobe"]] == ["top", "bottom", "feet"]
    close, full = (scene["prompt_en"] for scene in board["scenes"])
    assert "grey hoodie" in close and not _FOOTWEAR.search(close) and not _BOTTOMS.search(close)
    assert "black jeans and white sneakers" in full


def test_claude_negative_keeps_the_base_items_first() -> None:
    answer = _claude_answer([
        {"scene_type": "walking", "framing": "full_body", "prompt_en": "Full shot of the woman walking",
         "negative_prompt_en": ", ".join(f"scene item {i}" for i in range(40))},
    ])  # fmt: skip
    negative = _claude_board(answer)["scenes"][0]["negative_prompt_en"]
    assert len(negative) <= MAX_NEGATIVE_CHARS
    assert negative.startswith(BASE_NEGATIVE_EN) and "celebrity likeness, real person likeness" in negative
    for look in ("lens flare", "bokeh", "film grain"):
        assert look not in BASE_NEGATIVE_EN, "routes that append 'Avoid: …' to the prompt would prime these"
    assert "slow motion" in BASE_NEGATIVE_EN and "airbrushed skin" in BASE_NEGATIVE_EN


# --- instructions -----------------------------------------------------------------------------------------------------


def test_system_prompt_asks_for_candid_specific_footage() -> None:
    for phrase in (
        "ordinary-looking people", "not models", "everyday", "practical and natural light", "plain physical action",
        "partial framings", "gazing at the sky", "slow hair flips", "petals or particles drifting everywhere",
        "drone or crane reveals", "slow push-ins on a face", "glossy beauty close-ups", "lens flares", "bokeh",
        "cinematic, epic, dreamy, ethereal", "real-time speed, not slow motion", "no_people", "close_up",
        "full_body", "identity_en", "wardrobe", "every garment of the protagonist", "never a minor",
        "never name a film stock", "forbidden_likeness", "readable signs", "detected_real_people",
        "never put hex codes or palette", "grain and grading are added in post",
    ):  # fmt: skip
        assert phrase in SYSTEM_PROMPT, phrase
    for stale in ("colour palette and film look", "35mm anamorphic", "grain, halation, colour response",
                  "consistency_tokens_en", "Repeat the full protagonist description", "photorealistic"):  # fmt: skip
        assert stale not in SYSTEM_PROMPT, stale


def test_schema_carries_structured_wardrobe_and_framing() -> None:
    world = STORYBOARD_SCHEMA["properties"]["world_bible"]
    assert world["required"] == ["summary_ja", "identity_en", "wardrobe", "location_en", "look_en", "palette"]
    garment = world["properties"]["wardrobe"]["items"]
    assert garment["properties"]["region"]["enum"] == list(GARMENT_REGIONS)
    assert garment["required"] == ["region", "item_en"] and garment["additionalProperties"] is False
    scene = STORYBOARD_SCHEMA["properties"]["scenes"]["items"]
    assert scene["properties"]["framing"]["enum"] == list(FRAMINGS) and "framing" in scene["required"]
    assert "no lens, film-look, grain" in scene["properties"]["prompt_en"]["description"]
    # The frontend reads the output storyboard, not this schema: those keys are still produced.
    board = _claude_board(_claude_answer([{"scene_type": "walking", "framing": "full_body", "prompt_en": "x"}]))
    assert set(schemas.WorldBible.model_fields) <= set(board["world_bible"])


def test_claude_is_not_given_palette_names() -> None:
    preset = WORLD_PRESETS["nature"]
    payload = json.loads(_user_message(title="t", mood=None, purpose=None, lyrics=[], analysis=None, preset=preset,
                                       custom_world=None, n_scenes=3, forbidden=[]))  # fmt: skip
    assert "palette_en" not in payload["world_preset"] and payload["world_preset"]["palette"] == preset["palette"]
    assert not _LOOK_WORDS.search(payload["world_preset"]["look_en"])


# --- template path ----------------------------------------------------------------------------------------------------

_ALL_CHORUS = {  # like the sakura sample: three loud sections, no quiet intro
    "duration": 60.0,
    "sections": [{"start": 0.0, "end": 20.0, "label": "chorus", "energy": 0.71},
                 {"start": 20.0, "end": 40.0, "label": "verse", "energy": 0.69},
                 {"start": 40.0, "end": 60.0, "label": "chorus", "energy": 0.76}],
}  # fmt: skip


@pytest.mark.parametrize("preset_id", list(WORLD_PRESETS))
@pytest.mark.parametrize("analysis", ["demo", "all_chorus", None])
def test_template_prompts_follow_the_realism_rules(preset_id: str, analysis: str | None) -> None:
    from tests.test_storyboard import ANALYSIS, LYRICS

    song = {"demo": ANALYSIS, "all_chorus": _ALL_CHORUS, None: None}[analysis]
    board = generate_storyboard(title="桜", artist_name="Someone", mood="春、桜、儚さ、幻想的", purpose=None,
                                lyrics=LYRICS, analysis=song, preset_id=preset_id,
                                custom_world="雨の夜の東京" if preset_id == "custom" else None, n_scenes=12,
                                seed=7, use_claude=False)  # fmt: skip
    world = board["world_bible"]
    for scene in board["scenes"]:
        prompt, framing = scene["prompt_en"], scene["framing"]
        assert strip_ai_cliches(prompt) == (prompt, []) and not _LOOK_WORDS.search(prompt), prompt
        assert "drone" not in prompt and "dolly" not in prompt and "dreamlike" not in prompt
        if framing != "full_body":
            assert not _FOOTWEAR.search(prompt), (framing, prompt)
        if framing not in FRAMINGS_WITH_FACE:
            assert world["identity_en"].lower() not in prompt.lower()
            assert not any(g["item_en"] in prompt for g in world["wardrobe"])
        else:
            assert world["identity_en"].lower() in prompt.lower()
        assert ("realistic hands" in prompt) == (framing != "no_people")
        assert not scene["description_ja"].isascii()
    if analysis == "demo":
        # Environment shots between the protagonist shots: ~30 % of 12, one per distinct B-roll scene type.
        no_people = [s["scene_type"] for s in board["scenes"] if s["framing"] == "no_people"]
        offered = [t for t in WORLD_PRESETS[preset_id]["scene_types"] if not SCENE_TYPES[t]["has_subject"]]
        assert len(no_people) >= min(3, len(offered)), no_people


def test_template_face_shots_avoid_the_beauty_push_in() -> None:
    from tests.test_storyboard import ANALYSIS

    for seed in range(12):
        board = generate_storyboard(title="t", artist_name="a", mood=None, purpose=None, lyrics=[],
                                    analysis=ANALYSIS, preset_id="emotional", custom_world=None, n_scenes=10,
                                    seed=seed, use_claude=False)  # fmt: skip
        for scene in board["scenes"]:
            if scene["scene_type"] in ("close_portrait", "profile_window"):
                assert scene["camera_move"] not in ("slow_push_in", "orbit", "fast_push"), scene


def test_every_scene_type_has_a_framing() -> None:
    assert all(scene["framing"] in FRAMINGS for scene in SCENE_TYPES.values())
    assert {scene["framing"] for scene in SCENE_TYPES.values()} >= {"no_people", "close_up", "medium", "full_body"}


def test_base_negative_fits() -> None:
    assert len(_merge_negative(BASE_NEGATIVE_EN, *(p["negative_en"] for p in WORLD_PRESETS.values()))) <= 700
    assert len(BASE_NEGATIVE_EN) < MAX_NEGATIVE_CHARS - 60, "room for scene- and preset-specific items"


# --- review fixes (2026-10-13 second pass) -------------------------------------------------------------------------

_LEGACY_TAIL = " fictional characters only, no real or identifiable people, no celebrities, no text, no captions, no watermark."
# Stored Claude storyboards from the dev database (prompts and world bibles verbatim, ids are batch prefixes). Claude
# repeated the outfit in its own shortened words, which an exact item match missed: boots, loafers and trousers
# stayed in face close-ups and boots in medium shots.
_STORED: dict[str, dict[str, Any]] = {
    "b7c2cbf4": {
        "world": {
            "protagonist_en": ("a fictional a woman in her late 20s with shoulder-length dark brown wavy hair, wearing a "
            "long charcoal wool coat over a cream knit sweater, dark grey trousers and worn brown leather ankle boots"),
            "consistency_tokens_en": ("charcoal wool coat, cream knit sweater, dark grey trousers, brown leather ankle "
            "boots; deep teal, warm amber, charcoal palette; fine grain, soft halation, haze"),
        },
        "scenes": [
            ("s03", "close_portrait", ("Intimate close-up of a woman in her late 20s with shoulder-length dark brown wavy "
             "hair, charcoal wool coat over cream knit sweater, eyes glancing down away from camera, taking a slow calm "
             "breath, candlelit stone chapel interior softly out of focus behind, warm amber candle key light with deep "
             "shadows, eye-level slow dolly push-in, 35mm anamorphic lens, oval bokeh, deep teal and amber, fine grain, "
             "vertical 9:16 composition, photorealistic live-action footage, natural skin texture, subtle natural "
             "motion, charcoal wool coat, cream sweater, grey trousers, brown boots. charcoal wool coat, cream knit "
             "sweater, dark grey trousers, brown leather ankle boots; deep teal, warm amber, charcoal palette; fine "
             "grain, soft halation, haze.")),
            ("s05", "walking", ("Medium-wide shot of a woman in her late 20s with shoulder-length dark brown wavy hair, "
             "long charcoal wool coat, cream knit sweater, dark grey trousers, brown leather ankle boots, walking slowly "
             "along a grassy coastal path, coat hem moving gently with each step, sea below, soft warm dawn sidelight "
             "through haze, eye-level smooth lateral tracking shot, 35mm anamorphic lens, deep teal and amber palette, "
             "fine grain, vertical 9:16 composition, photorealistic live-action footage, natural skin texture, subtle "
             "natural motion, charcoal coat, cream sweater, grey trousers, brown boots. charcoal wool coat, cream knit "
             "sweater, dark grey trousers, brown leather ankle boots; deep teal, warm amber, charcoal palette; fine "
             "grain, soft halation, haze.")),
        ],
    },
    "397ea217": {
        "world": {
            "protagonist_en": ("a fictional a woman in her late 20s with shoulder-length wavy chestnut-brown hair, "
            "wearing a long cream wool coat, a blush-pink chunky knit sweater, a soft periwinkle knit scarf, dark grey "
            "wool trousers and brown leather lace-up winter boots"),
            "consistency_tokens_en": ("cream wool coat, blush-pink knit sweater, periwinkle scarf, grey wool trousers, "
            "brown leather boots; blush pink, periwinkle, pearl white; soft pastel film, bloom, falling snow"),
        },
        "scenes": [
            ("s06", "close_portrait", ("Intimate close-up of a woman in her late 20s with shoulder-length wavy chestnut "
             "hair, long cream wool coat, blush-pink knit sweater and periwinkle scarf, also grey wool trousers and "
             "brown leather boots, eyes glancing away, a slow calm breath visible as soft mist, snowflakes in her hair, "
             "blurred glowing chapel behind, warm candlelight on her cheek, eye-level slow dolly push-in, 85mm lens "
             "with diffusion, pastel film look, vertical 9:16 composition, photorealistic live-action footage, natural "
             "skin texture, subtle natural motion, cream wool coat, blush-pink knit sweater, periwinkle scarf; soft "
             "pastel film, bloom, falling snow. cream wool coat, blush-pink knit sweater, periwinkle scarf, grey wool "
             "trousers, brown leather boots; blush pink, periwinkle, pearl white; soft pastel film, bloom, falling "
             "snow.")),
        ],
    },
    "377cf397": {
        "world": {
            "protagonist_en": ("a fictional a man in his late 20s with short wavy dark-brown hair and light stubble, "
            "wearing a charcoal wool overcoat, a cream cable-knit wool sweater, dark olive corduroy trousers and worn "
            "brown leather lace-up boots"),
            "consistency_tokens_en": ("charcoal wool overcoat, cream cable-knit sweater, olive corduroy trousers, brown "
            "leather boots; slate, beige, olive palette; muted low-contrast film grain, misty light"),
        },
        "scenes": [
            ("s07", "close_portrait", ("Intimate close-up of a man in his late 20s with short wavy dark-brown hair and "
             "light stubble, collar of a charcoal wool overcoat over a cream cable-knit sweater, olive corduroy "
             "trousers and brown leather boots out of frame, eyes glancing away, a slow calm breath, misty glen softly "
             "out of focus. Soft overcast light. Eye-level subtle handheld. 28mm lens. Muted slate, beige, olive, film "
             "grain. Vertical 9:16 composition. Photorealistic live-action footage, natural skin texture, realistic "
             "hands, subtle natural motion. muted low-contrast film grain. charcoal wool overcoat, cream cable-knit "
             "sweater, olive corduroy trousers, brown leather boots; slate, beige, olive palette; muted low-contrast "
             "film grain, misty light.")),
        ],
    },
    "fd17b194": {
        "world": {
            "protagonist_en": ("a fictional a woman in her mid-20s with shoulder-length black hair, wearing a long camel "
            "wool coat over a cream knit sweater, dark navy pleated wool trousers and brown leather loafers"),
            "consistency_tokens_en": ("camel wool coat, cream knit sweater, navy pleated trousers, brown loafers; peach, "
            "dusty blue, soft cream palette; warm fine grain, soft halation, nostalgic glow"),
        },
        "scenes": [
            ("s06", "close_portrait", ("Intimate close-up of a woman in her mid-20s with shoulder-length black hair, "
             "camel wool coat over cream knit sweater, seated at a wooden desk, gently resting a hand on a closed old "
             "notebook, eyes glancing away, a slow calm breath. Empty classroom softly out of focus behind. Golden "
             "backlight, warm bounce on face. Eye-level locked-off shot. 50mm lens f/1.4. Peach, dusty blue, cream "
             "palette, fine grain. Vertical 9:16 composition. Photorealistic live-action footage, natural skin "
             "texture, realistic hands, subtle natural motion. navy pleated trousers, brown loafers. camel wool coat, "
             "cream knit sweater, navy pleated trousers, brown loafers; peach, dusty blue, soft cream palette; warm "
             "fine grain, soft halation, nostalgic glow.")),
        ],
    },
    "2bcc97d8": {
        "world": {
            "protagonist_en": ("a fictional man in his late 20s with short wavy auburn hair and light stubble, wearing "
            "a worn olive wool tweed jacket, a cream knit sweater, dark brown corduroy trousers and scuffed brown "
            "leather boots"),
            "consistency_tokens_en": ("olive tweed jacket, cream knit sweater, brown corduroy trousers, brown leather "
            "boots; faded mustard, sea-glass green, terracotta; 16mm grain, soft halation"),
        },
        "scenes": [
            ("s03", "walking", ("Medium shot of a man in his late 20s with short wavy auburn hair and stubble, olive "
             "tweed jacket, cream knit sweater, brown corduroys and leather boots walking slowly along a stone-walled "
             "country lane, jacket moving with each step, warm low sun and mist, eye-level smooth lateral tracking "
             "shot, 16mm-style lens, faded mustard and terracotta, visible grain, vertical 9:16 composition, "
             "photorealistic live-action footage, natural skin texture, realistic hands, subtle natural motion, olive "
             "tweed jacket, cream knit sweater, brown corduroy trousers, brown leather boots; 16mm grain. olive tweed "
             "jacket, cream knit sweater, brown corduroy trousers, brown leather boots; faded mustard, sea-glass "
             "green, terracotta; 16mm grain, soft halation.")),
        ],
    },
}  # fmt: skip


def _stored_board(batch: str) -> dict[str, Any]:
    stored = _STORED[batch]
    return {
        "source": "claude", "world_bible": {**stored["world"], "palette": ["#111111", "#222222", "#333333"]},
        "scenes": [{"id": sid, "section": "verse", "scene_type": stype, "prompt_en": prompt + _LEGACY_TAIL,
                    "negative_prompt_en": "text"} for sid, stype, prompt in stored["scenes"]],
    }  # fmt: skip


@pytest.mark.parametrize("batch", list(_STORED))
def test_upgrade_drops_shortened_hidden_garments_of_stored_storyboards(batch: str) -> None:
    board = upgrade_storyboard(_stored_board(batch))
    world = board["world_bible"]
    assert world["identity_en"].startswith("a fictional ") and "fictional a " not in world["identity_en"]
    for scene in board["scenes"]:
        body = scene["prompt_en"].split(LIKENESS_SAFETY_CLAUSE)[0]
        if scene["framing"] == "close_up":
            assert not _FOOTWEAR.search(body) and not _BOTTOMS.search(body), (batch, scene["id"], body)
            assert not re.search(r"corduroys?\b", body), body
        if scene["framing"] == "medium":
            assert not _FOOTWEAR.search(body), (batch, scene["id"], body)
        assert not _LOOK_WORDS.search(body), body
        assert not re.search(r"\bfictional an?\b|\bA fictional\b", body), "one description of the person, bound"
        assert not re.search(r",\s*(?:long|also)\s*,|\b(?:of|on|and)\s*[,.]", body), body  # no stubs left behind
    if batch == "b7c2cbf4":
        face = board["scenes"][0]
        assert face["framing"] == "close_up" and "charcoal wool coat over cream knit sweater" in face["prompt_en"]
        walk = board["scenes"][1]
        # Claude's own description stays whole (only the shoes go); the repeated short list at the end is gone.
        assert "long charcoal wool coat, cream knit sweater, dark grey trousers, walking slowly" in walk["prompt_en"]
        assert "charcoal coat, cream sweater" not in walk["prompt_en"]
    if batch == "2bcc97d8":
        assert "brown corduroys walking slowly along a stone-walled country lane" in board["scenes"][0]["prompt_en"]


def test_detail_shots_on_environment_scene_types_keep_their_person() -> None:
    """Regression: a detail shot on a no-people scene type got "no people" appended — the floating sneaker again."""
    answer = _claude_answer([
        {"scene_type": "nature_macro", "framing": "detail",
         "prompt_en": "Close-up of the woman's scuffed white sneakers stepping through wet weeds by a fence"},
        {"scene_type": "ambient_detail", "framing": "detail",
         "prompt_en": "Close-up of her hand in the pocket of the faded navy cotton work jacket at the bus stop"},
        # No framing given: a close-up of shoes in the text is a detail shot too.
        {"scene_type": "nature_macro", "framing": None,
         "prompt_en": "Close-up of the woman's sneakers in the wet grass"},
        # A face framing on an environment scene type still becomes no_people.
        {"scene_type": "sky_drift", "framing": "close_up", "prompt_en": "Clouds over the rooftops at dusk"},
        {"scene_type": "nature_macro", "framing": "detail", "prompt_en": "Close-up of moss on a wall, no people"},
    ])  # fmt: skip
    sneakers, pocket, untagged, sky, moss = _claude_board(answer)["scenes"]
    for scene, garment in ((sneakers, "scuffed white sneakers"), (pocket, "faded navy cotton work jacket"),
                           (untagged, "sneakers")):  # fmt: skip
        assert scene["framing"] == "detail", scene
        assert "no people" not in scene["prompt_en"] and garment in scene["prompt_en"]
        assert _IDENTITY not in scene["prompt_en"]
    assert sky["framing"] == "no_people" and "no people" in sky["prompt_en"]
    assert moss["framing"] == "no_people"
    assert "ambient_detail, sky_drift, light_abstract, nature_macro and street_flow are environment" in SYSTEM_PROMPT
    assert "for no_people scenes only" in SYSTEM_PROMPT


def test_identity_article_is_not_doubled() -> None:
    """Regression: "a fictional a woman …" (6 of 9 stored world bibles) and its copy in every face prompt."""
    answer = _claude_answer(
        [{"scene_type": "close_portrait", "framing": "close_up", "prompt_en": "Close-up of the woman yawning"}],
        identity_en="A woman in her 20s with a short black bob and freckles",
    )
    board = _claude_board(answer)
    assert board["world_bible"]["identity_en"] == "a fictional woman in her 20s with a short black bob and freckles"
    prompt = board["scenes"][0]["prompt_en"]
    assert "fictional a woman" not in prompt
    assert prompt.startswith("Close-up of the woman yawning. The woman is a fictional woman in her 20s with a short")
    legacy = upgrade_storyboard({"world_bible": {"protagonist_en": "a fictional a man in his 30s, wearing a grey "
                                                 "hoodie"}, "scenes": []})  # fmt: skip
    assert legacy["world_bible"]["identity_en"] == "a fictional man in his 30s"


def test_identity_without_article_gets_one() -> None:
    """Regression (live run 2026-10-13): Claude wrote "fictional woman in her late 20s …" and the prompt read
    "The woman is fictional woman …"."""
    answer = _claude_answer(
        [{"scene_type": "close_portrait", "framing": "close_up", "prompt_en": "Close-up of the woman blinking"}],
        identity_en="fictional woman in her late 20s, round everyday face with light freckles",
    )
    board = _claude_board(answer)
    assert board["world_bible"]["identity_en"] == "a fictional woman in her late 20s, round everyday face with light freckles"
    assert "The woman is a fictional woman in her late 20s" in board["scenes"][0]["prompt_en"]


@pytest.mark.parametrize(
    "identity",
    [
        f"{_IDENTITY}, in a yellow raincoat and red rubber boots",
        f"{_IDENTITY} who always wears a yellow raincoat and red rubber boots",
        f"{_IDENTITY}; yellow raincoat, red rubber boots",
    ],
)
def test_clothing_in_the_identity_without_wearing_is_split_off(identity: str) -> None:
    """Regression: boots and a second outer layer written into identity_en reached every close-up."""
    answer = _claude_answer(
        [{"scene_type": "close_portrait", "framing": "close_up", "prompt_en": "Close-up of the woman yawning"}],
        identity_en=identity,
    )
    board = _claude_board(answer)
    assert board["world_bible"]["identity_en"] == _IDENTITY
    assert board["world_bible"]["wardrobe"] == _WARDROBE  # covered regions: the listed wardrobe wins
    prompt = board["scenes"][0]["prompt_en"]
    assert "raincoat" not in prompt and not _FOOTWEAR.search(prompt)
    # Without a wardrobe list the split-off garments become the wardrobe.
    bare = _claude_board(_claude_answer(
        [{"scene_type": "walking", "framing": "full_body", "prompt_en": "Full shot of the woman walking a dog"}],
        identity_en=identity, wardrobe=[],
    ))  # fmt: skip
    assert [(g["region"], g["item_en"]) for g in bare["world_bible"]["wardrobe"]] == [
        ("outer", "yellow raincoat"), ("feet", "red rubber boots")
    ]


def test_subject_sentence_is_bound_to_the_shot_and_never_doubles_a_described_person() -> None:
    answer = _claude_answer([
        {"scene_type": "close_portrait", "framing": "close_up",
         "prompt_en": "Close-up of the woman blowing on a paper cup of coffee, steam rising"},
        {"scene_type": "close_portrait", "framing": "close_up",
         "prompt_en": "Close-up of a woman in her late twenties with a short black bob, laughing at a bus stop"},
        {"scene_type": "close_portrait", "framing": "close_up", "prompt_en": "Close-up of her face as the bus passes"},
    ])  # fmt: skip
    bound, described, pronoun = (s["prompt_en"] for s in _claude_board(answer)["scenes"])
    assert f"steam rising. The woman is {_IDENTITY}, wearing a faded navy cotton work jacket" in bound
    assert bound.count("a fictional woman") == 1 and "A fictional" not in bound  # no second, unbound person
    assert "fictional" not in described.split(LIKENESS_SAFETY_CLAUSE)[0]
    assert "at a bus stop. She is wearing a faded navy cotton work jacket" in described
    assert f"bus passes. She is {_IDENTITY}" in pronoun


@pytest.mark.parametrize(
    ("framing", "shot", "kept"),
    [
        ("medium", ("Medium shot of the woman on a station bench, bending down to tie the laces of her scuffed white "
                    "sneakers"), "tie the laces of her scuffed white sneakers"),
        ("close_up", "Close-up of the woman adjusting the strap of her brown leather tote bag at the bus stop",
         "the strap of her brown leather tote bag at the bus stop"),
        ("close_up", "Close-up of the woman wiping her hands on her black jeans, then buttoning her jacket",
         "wiping her hands on her black jeans, then buttoning"),
        ("medium", "Medium shot of her hand reaching into the pocket of her black jeans for a ticket",
         "the pocket of her black jeans for a ticket"),
    ],
)  # fmt: skip
def test_a_garment_the_action_uses_is_kept(framing: str, shot: str, kept: str) -> None:
    """Regression: "tie the laces." / "the strap of at the bus stop" — hidden garments were cut out of actions."""
    answer = _claude_answer([{"scene_type": "close_portrait", "framing": framing, "prompt_en": shot}])
    prompt = _claude_board(answer)["scenes"][0]["prompt_en"]
    assert kept in prompt, prompt
    assert not re.search(r"\b(?:of|on|into)\s*[,.]|\bof at\b", prompt)


def test_clothing_lists_still_lose_the_hidden_garments() -> None:
    answer = _claude_answer([
        {"scene_type": "close_portrait", "framing": "close_up",
         "prompt_en": "Close-up of the woman wearing black jeans and white sneakers, laughing"},
        {"scene_type": "walking", "framing": "medium",
         "prompt_en": "Medium shot of the woman in her faded navy cotton work jacket and black straight-leg jeans and "
                      "scuffed white sneakers walking"},
        {"scene_type": "close_portrait", "framing": "close_up",
         "prompt_en": "Close-up of the woman in her work jacket over black jeans, leaning over the railing, laughing"},
    ])  # fmt: skip
    face, walk, railing = (s["prompt_en"] for s in _claude_board(answer)["scenes"])
    assert railing.startswith("Close-up of the woman in her work jacket, leaning over the railing, laughing.")
    assert face.startswith("Close-up of the woman, laughing.") and not _BOTTOMS.search(face)
    assert walk.startswith("Medium shot of the woman in her faded navy cotton work jacket and black straight-leg jeans "
                           "walking.")  # fmt: skip
    assert not _FOOTWEAR.search(walk)


def test_template_custom_world_with_a_paint_palette_survives() -> None:
    """Regression: "a painter's attic studio with a paint palette" was deleted from every template prompt."""
    board = generate_storyboard(title="t", artist_name="a", mood=None, purpose=None, lyrics=[], analysis=None,
                                preset_id="custom", custom_world="a painter's attic studio with a paint palette and "
                                "canvases", n_scenes=4, seed=1, use_claude=False)  # fmt: skip
    for scene in board["scenes"]:
        assert "painter's attic studio with a paint palette and canvases" in scene["prompt_en"], scene["prompt_en"]
    assert not any("映像の質感" in w for w in board["warnings"])


@pytest.mark.parametrize(("mood", "plain"), [("幻想的", "hazy"), ("壮大", "vast"), ("dreamy", "hazy")])
def test_template_mood_style_words_become_plain_words(mood: str, plain: str) -> None:
    """Regression: 幻想的 / 壮大 / dreamy were stripped and left "… wet pavement mood." with no mood at all."""
    board = generate_storyboard(title="t", artist_name="a", mood=mood, purpose=None, lyrics=[], analysis=None,
                                preset_id="urban_night", custom_world=None, n_scenes=3, seed=1,
                                use_claude=False)  # fmt: skip
    for scene in board["scenes"]:
        prompt = scene["prompt_en"]
        assert f"{plain} mood" in prompt and not re.search(r"(?:pavement|,)\s+mood\b", prompt), prompt
        assert strip_ai_cliches(prompt) == (prompt, [])


def test_template_drops_pure_look_moods_with_a_note() -> None:
    board = generate_storyboard(title="t", artist_name="a", mood="シネマティック", purpose=None, lyrics=[],
                                analysis=None, preset_id="urban_night", custom_world=None, n_scenes=2, seed=1,
                                use_claude=False)  # fmt: skip
    assert any(w.startswith("雰囲気の指定の「cinematic」は映像の質感を指す言葉") for w in board["warnings"])
    for scene in board["scenes"]:
        assert "cinematic" not in scene["prompt_en"].lower() and " mood." not in scene["prompt_en"]
