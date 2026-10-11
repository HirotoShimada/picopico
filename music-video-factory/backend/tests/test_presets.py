"""World presets, creative vocabulary and colour grades (ARCHITECTURE.md §6.3)."""

from __future__ import annotations

import colorsys
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
    LYRIC_STYLES,
    SCENE_TYPES,
    WORLD_PRESETS,
)

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
    # look_en carries lighting, lens and film-stock vocabulary.
    assert "mm" in preset["lens_en"] and "lens" in preset["lens_en"]
    assert "film" in preset["film_en"] or "cinema" in preset["film_en"]
    for part in ("lighting_en", "lens_en", "film_en", "atmosphere_en"):
        assert preset[part] in preset["look_en"]
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
    for vocab in (CAMERA_ANGLES, CAMERA_MOVES):
        for entry_id, entry in vocab.items():
            assert entry["prompt_en"].isascii() and not entry["name_ja"].isascii(), entry_id
    assert {m.get("energy") for m in CAMERA_MOVES.values()} == {"low", "mid", "high"}


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
