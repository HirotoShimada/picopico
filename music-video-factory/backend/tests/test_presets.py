"""World presets, creative vocabulary and colour grades (ARCHITECTURE.md §6.3)."""

from __future__ import annotations

import colorsys
import itertools
import re
import shutil
import subprocess
import typing

import numpy as np
import pytest

from app import schemas
from app.services import presets
from app.services.presets import (
    CAMERA_ANGLES,
    CAMERA_MOVES,
    COLOR_GRADES,
    FRAMING_REGIONS,
    FRAMINGS,
    FRAMINGS_WITH_FACE,
    GARMENT_REGIONS,
    LYRIC_STYLES,
    SCENE_TYPES,
    WORLD_PRESETS,
    garment_region,
    visible_garments,
    wardrobe_phrase,
)
from app.services.prompting import strip_ai_cliches

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")

CONTRACT_GRADES = {
    "teal_orange",
    "warm_film",
    "cool_night",
    "bleach_bypass",
    "faded_pastel",
    "mono_noir",
    "natural",
    "vivid",
}
_HEX = re.compile(r"^#[0-9A-F]{6}$")


def test_preset_ids_match_api_enum() -> None:
    assert list(WORLD_PRESETS) == list(typing.get_args(schemas.PresetIdT))
    assert tuple(WORLD_PRESETS) == presets.PRESET_IDS
    for preset_id, preset in WORLD_PRESETS.items():
        assert preset["id"] == preset_id


@pytest.mark.parametrize("preset_id", list(WORLD_PRESETS))
def test_preset_is_complete(preset_id: str) -> None:
    preset = WORLD_PRESETS[preset_id]
    assert not preset["name_ja"].isascii() and preset["name_en"]
    assert len(preset["description_ja"]) >= 15 and not preset["description_ja"].isascii()
    # look_en carries lighting, camera feel, colour response and atmosphere — in plain words since 2026-10-13
    # (intentional change: no focal lengths, anamorphic lenses, film looks or grain; grain is added in post).
    for part in ("lighting_en", "lens_en", "film_en", "atmosphere_en"):
        assert preset[part] in preset["look_en"]
        assert strip_ai_cliches(preset[part]) == (preset[part], []), (preset_id, part)
    assert not re.search(r"\d\s?mm|anamorphic|\blens\b|grain|halation|bokeh|flare|film look", preset["look_en"])
    assert "colour" in preset["film_en"]
    assert len(preset["palette"]) == 3 and all(_HEX.match(c) for c in preset["palette"])
    assert preset["palette_en"] and not preset["palette_ja"].isascii()

    assert len(preset["scene_types"]) >= 8 and set(preset["scene_types"]) <= set(SCENE_TYPES)
    assert len(set(preset["scene_types"])) == len(preset["scene_types"])
    with_subject = [s for s in preset["scene_types"] if SCENE_TYPES[s]["has_subject"]]
    without_subject = [s for s in preset["scene_types"] if not SCENE_TYPES[s]["has_subject"]]
    assert with_subject and without_subject, "presets mix protagonist scenes with atmosphere B-roll"
    energies = {SCENE_TYPES[s]["energy"] for s in preset["scene_types"]}
    assert {"low", "high"} <= energies or {"low", "mid"} <= energies

    assert len(preset["camera_angles"]) >= 4 and set(preset["camera_angles"]) <= set(CAMERA_ANGLES)
    assert len(preset["camera_moves"]) >= 6 and set(preset["camera_moves"]) <= set(CAMERA_MOVES)
    assert len(preset["color_grades"]) >= 3 and set(preset["color_grades"]) <= set(COLOR_GRADES)
    assert preset["lyric_styles"] and set(preset["lyric_styles"]) <= set(LYRIC_STYLES)

    assert len(preset["protagonists"]) >= 2 and preset["locations"]
    for protagonist in preset["protagonists"]:
        assert protagonist["en"].startswith("a fictional ")
        assert re.search(r"twenties|thirties", protagonist["en"]), "protagonists are adults"
        assert "架空" in protagonist["ja"]
        # Structured wardrobe: identity without clothing + every garment with its body region.
        assert protagonist["identity_en"].startswith("a fictional ") and "wearing" not in protagonist["identity_en"]
        assert protagonist["en"] == f"{protagonist['identity_en']}, wearing {wardrobe_phrase(protagonist['wardrobe'])}"
        regions = [g["region"] for g in protagonist["wardrobe"]]
        assert set(regions) <= set(GARMENT_REGIONS) and "top" in regions and "feet" in regions
        is_dress = any(re.search(r"dress", g["item_en"]) for g in protagonist["wardrobe"])
        assert "bottom" in regions or is_dress, "no clip may drop the trousers"
        for garment in protagonist["wardrobe"]:
            assert garment_region(garment["item_en"]) == garment["region"], garment
    for location in preset["locations"]:
        assert location["en"].isascii() and not location["ja"].isascii()


def test_scene_vocabulary_templates() -> None:
    for scene_id, scene in SCENE_TYPES.items():
        assert scene["energy"] in ("low", "mid", "high"), scene_id
        assert not scene["name_ja"].isascii()
        filled = scene["prompt_en"].format(subject="SUBJECT", place="PLACE")
        assert "PLACE" in filled, scene_id
        assert ("SUBJECT" in filled) == scene["has_subject"], scene_id
        if not scene["has_subject"]:
            assert re.search(r"no people|no pedestrians", filled), scene_id
        assert "{" not in filled and filled.isascii()
        desc = scene["desc_ja"].format(subject="主人公", place="街")
        assert "街" in desc and "{" not in desc
        assert scene["framing"] in FRAMINGS
        assert (scene["framing"] == "no_people") == (not scene["has_subject"]), scene_id
        assert scene["has_subject"] == (scene["framing"] in FRAMINGS_WITH_FACE), scene_id
        assert strip_ai_cliches(filled) == (filled, []), scene_id
        assert not re.search(r"bokeh|flare|gazing|drifting petals|slow-motion|slow dolly", filled), scene_id
    for vocab in (CAMERA_ANGLES, CAMERA_MOVES):
        for entry_id, entry in vocab.items():
            assert entry["prompt_en"].isascii() and not entry["name_ja"].isascii(), entry_id
            assert not re.search(r"dolly|gimbal|drone|crane shot|aerial", entry["prompt_en"]), entry_id
    assert {m.get("energy") for m in CAMERA_MOVES.values()} == {"low", "mid", "high"}
    # Drone reveals read as stock AI footage: the id stays (enum, stored storyboards) but no preset picks it.
    assert not any("drone_rise" in preset["camera_moves"] for preset in WORLD_PRESETS.values())


def test_wardrobe_helpers() -> None:
    garments = [
        {"region": "outer", "item_en": "beige linen trench coat"},
        {"region": "top", "item_en": "white cotton blouse"},
        {"region": "bottom", "item_en": "sage-green pleated skirt"},
        {"region": "feet", "item_en": "white canvas sneakers"},
        {"region": "head", "item_en": "silver hoop earrings"},
        {"region": "carry", "item_en": "canvas tote bag"},
    ]
    phrase = {framing: wardrobe_phrase(visible_garments(garments, framing)) for framing in FRAMINGS}
    assert phrase["no_people"] == phrase["detail"] == ""
    assert phrase["close_up"] == "a beige linen trench coat over a white cotton blouse, with silver hoop earrings"
    assert phrase["medium"] == (
        "a beige linen trench coat over a white cotton blouse and a sage-green pleated skirt, with silver hoop "
        "earrings and a canvas tote bag"
    )
    assert phrase["full_body"].endswith("a sage-green pleated skirt and white canvas sneakers, with silver hoop "
                                        "earrings and a canvas tote bag")  # fmt: skip
    assert "feet" not in FRAMING_REGIONS["close_up"] | FRAMING_REGIONS["medium"]
    assert "bottom" not in FRAMING_REGIONS["close_up"] and "bottom" in FRAMING_REGIONS["medium"]
    assert wardrobe_phrase([{"region": "top", "item_en": "oversized white shirt"}]) == "an oversized white shirt"
    for text, region in [("white canvas sneakers", "feet"), ("long sage-green pleated skirt", "bottom"),
                         ("light beige linen trench coat", "outer"), ("sweater dress", "top"),
                         ("canvas tote bag", "carry"), ("knit beanie", "head"), ("forest green", None),
                         ("soft fine grain", None), ("silver hair", None)]:  # fmt: skip
        assert garment_region(text) == region, text


def test_lyric_styles_match_contract_and_subtitles() -> None:
    from app.services.subtitles import LYRIC_STYLE_IDS

    assert list(LYRIC_STYLES) == list(typing.get_args(schemas.LyricStyleT)) == list(LYRIC_STYLE_IDS)
    for style in LYRIC_STYLES.values():
        assert not style["name_ja"].isascii() and not style["description"].isascii()
        assert style["default_position"] in ("bottom", "center", "top")


def test_color_grade_ids_cover_contract_and_renderer_fallbacks() -> None:
    from app.services.render import FALLBACK_COLOR_GRADES

    assert set(COLOR_GRADES) >= CONTRACT_GRADES
    assert set(FALLBACK_COLOR_GRADES) <= set(COLOR_GRADES)
    for grade_id, grade in COLOR_GRADES.items():
        assert re.match(r"^[a-z0-9_]{1,40}$", grade_id), "ids must satisfy the GenerationSettings pattern"
        assert not grade["name"].isascii() and grade["name_en"] and grade["description_ja"]
        # Safe to append to a filter chain: no labels, no chain separators, no runtime-escaped commas.
        assert not re.search(r"[\[\];]", grade["ffmpeg_filter"])


@needs_ffmpeg
@pytest.mark.parametrize("grade_id", list(COLOR_GRADES))
def test_color_grade_filter_runs_in_ffmpeg(grade_id: str) -> None:
    graph = COLOR_GRADES[grade_id]["ffmpeg_filter"]
    cmd = [FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x568:d=1", "-vf", graph, "-f", "null", "-"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    # Same probe the renderer uses (planar RGB chain).
    cmd = [FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=32x32:d=0.1:r=10",
           "-vf", f"format=gbrp,{graph},format=gbrp", "-f", "null", "-"]  # fmt: skip
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def _test_card() -> np.ndarray:
    card = np.zeros((96, 64, 3), np.uint8)
    card[0:16] = np.linspace(0, 255, 64).astype(np.uint8)[None, :, None]  # grey ramp
    patches = [(224, 172, 140), (141, 85, 60), (120, 170, 220), (60, 110, 60), (240, 200, 120), (200, 60, 90)]
    for i, rgb in enumerate(patches):
        card[16 + i * 13 : 16 + (i + 1) * 13] = rgb
    return card


def _grade_frame(graph: str, card: np.ndarray) -> np.ndarray:
    h, w, _ = card.shape
    cmd = [FFMPEG, "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-i", "-",
           "-vf", f"{graph},format=rgb24", "-frames:v", "1", "-f", "rawvideo", "-"]  # fmt: skip
    out = subprocess.run(cmd, input=card.tobytes(), capture_output=True, check=True, timeout=60).stdout
    return np.frombuffer(out, np.uint8).reshape(h, w, 3)


@needs_ffmpeg
def test_color_grades_look_sane() -> None:
    """Grades keep colour (FFmpeg 6.1 colorbalance pl=1 collapsed bright colours to grey) and differ visibly."""
    card = _test_card()
    graded = {gid: _grade_frame(g["ffmpeg_filter"], card) for gid, g in COLOR_GRADES.items()}
    patch_rows = [16 + i * 13 + 6 for i in range(6)]
    for grade_id, frame in graded.items():
        mean = frame.reshape(-1, 3).mean(axis=0)
        assert 60 < mean.mean() < 200, f"{grade_id} crushes or blows out the image"
        sats = [colorsys.rgb_to_hsv(*(frame[row, 32] / 255.0))[1] for row in patch_rows]
        if grade_id == "mono_noir":
            assert max(sats) < 0.02
        else:
            assert min(sats[i] for i in (0, 2, 3, 4, 5)) > 0.15, f"{grade_id} turns saturated colours grey: {sats}"
    ids = list(graded)
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            diff = np.abs(graded[a].astype(int) - graded[b].astype(int)).mean()
            assert diff > 1.5, f"{a} and {b} look identical"


@needs_ffmpeg
def test_renderer_uses_preset_grades() -> None:
    from app.services.render import color_grade_filter

    for grade_id, grade in COLOR_GRADES.items():
        assert color_grade_filter(grade_id) == grade["ffmpeg_filter"]


def test_capability_helpers_validate_against_api_models() -> None:
    preset_models = [schemas.WorldPresetInfo.model_validate(p) for p in presets.preset_infos()]
    assert [p.id for p in preset_models] == list(WORLD_PRESETS)
    grades = [schemas.ColorGradeInfo.model_validate(g) for g in presets.color_grade_infos()]
    assert {g.id for g in grades} == set(COLOR_GRADES)
    styles = [schemas.LyricStyleInfo.model_validate(s) for s in presets.lyric_style_infos()]
    assert [s.id for s in styles] == list(LYRIC_STYLES)


def test_get_preset_falls_back_to_cinematic() -> None:
    assert presets.get_preset("dreamy")["id"] == "dreamy"
    assert presets.get_preset("nope")["id"] == "cinematic"
    assert presets.get_preset(None)["id"] == "cinematic"


# --------------------------------------------------------------------------- shadow detail & look

_SHADOW_PATCHES = [(224, 172, 140), (141, 85, 60), (120, 170, 220), (60, 110, 60), (240, 200, 120), (200, 60, 90)]


def _ramp_card() -> np.ndarray:
    """Row 0: grey ramp 0..255; rows 1..: the colour patches."""
    card = np.zeros((1 + len(_SHADOW_PATCHES), 256, 3), np.uint8)
    card[0] = np.arange(256, dtype=np.uint8)[:, None]
    for i, rgb in enumerate(_SHADOW_PATCHES):
        card[1 + i] = rgb
    return card


def _graded_rgb(chain: str, card: np.ndarray) -> np.ndarray:
    """``chain`` applied the way the renderer does: in planar RGB."""
    h, w, _ = card.shape
    cmd = [FFMPEG, "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-i", "-",
           "-vf", f"format=gbrp,{chain},format=gbrp,format=rgb24", "-frames:v", "1", "-f", "rawvideo", "-"]  # fmt: skip
    out = subprocess.run(cmd, input=card.tobytes(), capture_output=True, check=True, timeout=60).stdout
    return np.frombuffer(out, np.uint8).reshape(h, w, 3).astype(float)


def _longest_flat_run(values: list[int]) -> int:
    best = run = 1
    for a, b in itertools.pairwise(values):
        run = run + 1 if a == b else 1
        best = max(best, run)
    return best


def _assert_shadow_detail(name: str, ramp: np.ndarray) -> None:
    luma = np.round(0.2126 * ramp[:, 0] + 0.7152 * ramp[:, 1] + 0.0722 * ramp[:, 2]).astype(int)
    shadows = luma[:41].tolist()
    assert _longest_flat_run(shadows) <= 3, f"{name} merges dark grey steps: {shadows}"
    for channel in range(3):
        values = ramp[:41, channel].astype(int).tolist()
        assert _longest_flat_run(values) <= 4, f"{name} crushes channel {'RGB'[channel]}: {values}"
    assert luma[40] - luma[0] >= 18, f"{name} compresses the shadows: {shadows}"


def _grade_ids_and_chains() -> list[tuple[str, str]]:
    from app.services.render import FALLBACK_COLOR_GRADES

    chains = [(grade_id, grade["ffmpeg_filter"]) for grade_id, grade in COLOR_GRADES.items()]
    chains += [(f"fallback:{grade_id}", grade["ffmpeg_filter"]) for grade_id, grade in FALLBACK_COLOR_GRADES.items()]
    return chains


@needs_ffmpeg
@pytest.mark.parametrize(("grade_id", "chain"), _grade_ids_and_chains(), ids=[g for g, _ in _grade_ids_and_chains()])
def test_color_grades_keep_shadow_detail(grade_id: str, chain: str) -> None:
    # Regression: e.g. bleach_bypass mapped grey 0–21 to black (eq contrast around mid-grey
    # + an S-curve), teal_orange 0–9, vivid 0–7. Adjacent dark steps must stay distinct.
    _assert_shadow_detail(grade_id, _graded_rgb(chain, _ramp_card())[0])


@needs_ffmpeg
@pytest.mark.parametrize("grade_id", list(COLOR_GRADES))
def test_rendered_grades_keep_shadow_detail(grade_id: str, tmp_path: typing.Any) -> None:
    # The renderer's own grade stage (expensive runs baked into a 3D LUT) must keep them too.
    from app.services import render

    steps = render._grade_steps(grade_id, tmp_path)
    chain = ",".join(step.value for step in steps if step.kind == "filters")
    _assert_shadow_detail(f"rendered {grade_id}", _graded_rgb(chain, _ramp_card())[0])


# Each grade's character, measured on the grey ramp (Y = luma, R−G / B−G = tint) and the
# colour patches (saturation relative to the original). The pre-retune grades had the
# same character; the retune only changed how the shadows are reached.
_LOOKS: dict[str, dict[str, tuple[float, float]]] = {
    "natural": {"sat": (1.0, 1.15), "contrast": (1.03, 1.2), "rg128": (-3, 3), "bg128": (-3, 3)},
    "teal_orange": {"sat": (1.1, 1.4), "contrast": (1.15, 1.45), "rg40": (-20, -3), "bg40": (3, 20),
                    "rg128": (6, 30), "bg128": (-40, -12)},
    "warm_film": {"black": (6, 20), "white": (225, 245), "rg128": (4, 20), "bg128": (-30, -8), "bg220": (-40, -15)},
    "cool_night": {"sat": (0.75, 0.95), "rg128": (-15, -2), "bg128": (8, 30), "bg220": (8, 30)},
    "bleach_bypass": {"sat": (0.6, 0.8), "contrast": (1.4, 1.8), "rg128": (-6, 6)},
    "faded_pastel": {"sat": (0.55, 0.75), "black": (18, 35), "white": (230, 245), "contrast": (0.75, 0.95)},
    "mono_noir": {"sat": (0.0, 0.02), "contrast": (1.3, 1.6)},
    "vivid": {"sat": (1.25, 1.5), "contrast": (1.1, 1.3)},
    "golden_hour": {"sat": (1.0, 1.25), "rg128": (8, 25), "bg128": (-35, -15), "rg220": (12, 30)},
    "neon_night": {"sat": (1.1, 1.4), "contrast": (1.25, 1.55), "rg40": (5, 25), "bg40": (8, 30)},
    "forest_moody": {"black": (3, 15), "rg40": (-20, -5), "bg128": (-12, -2)},
    "lavender_haze": {"sat": (0.6, 0.8), "black": (15, 30), "rg128": (1, 10), "bg128": (3, 12)},
}  # fmt: skip


def _look(frame: np.ndarray) -> dict[str, float]:
    ramp = frame[0]
    luma = 0.2126 * ramp[:, 0] + 0.7152 * ramp[:, 1] + 0.0722 * ramp[:, 2]
    look = {"black": luma[0], "white": luma[255], "contrast": (luma[170] - luma[85]) / 85}
    for code in (40, 128, 220):
        look[f"rg{code}"] = ramp[code, 0] - ramp[code, 1]
        look[f"bg{code}"] = ramp[code, 2] - ramp[code, 1]
    ratios = [
        colorsys.rgb_to_hsv(*(frame[1 + i, 128] / 255.0))[1] / colorsys.rgb_to_hsv(*(np.array(rgb) / 255.0))[1]
        for i, rgb in enumerate(_SHADOW_PATCHES)
    ]
    look["sat"] = float(np.mean(ratios))
    return look


@needs_ffmpeg
@pytest.mark.parametrize("grade_id", list(COLOR_GRADES))
def test_color_grades_keep_their_look(grade_id: str) -> None:
    look = _look(_graded_rgb(COLOR_GRADES[grade_id]["ffmpeg_filter"], _ramp_card()))
    assert set(_LOOKS) == set(COLOR_GRADES)
    for key, (low, high) in _LOOKS[grade_id].items():
        assert low <= look[key] <= high, f"{grade_id}: {key}={look[key]:.3f} not in [{low}, {high}]"


def test_presets_and_claude_instructions_avoid_product_names() -> None:
    # Video models draw product names as visible text (live Veo run, 2026-10-13: "CineStill 800T").
    import json as _json

    from app.services.prompting import rewrite_brand_names
    from app.services.storyboard import SYSTEM_PROMPT

    dumped = _json.dumps(WORLD_PRESETS, ensure_ascii=False, default=str)
    assert rewrite_brand_names(dumped)[1] == []
    assert "never name a film stock" in SYSTEM_PROMPT
