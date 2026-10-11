from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import (
    AutoTimeRequest,
    GenerationSettings,
    LyricLine,
    LyricLinesUpdate,
    RightsUpdate,
    SegmentUpdate,
    TextStyle,
    VideoEdit,
)


def test_generation_settings_defaults() -> None:
    settings = GenerationSettings()
    assert settings.model_dump() == {
        "preset": "cinematic",
        "custom_world": None,
        "provider": "mock",
        "model": None,
        "duration_sec": 15,
        "count": 3,
        "lyric_style": "simple",
        "text_style": None,
        "color_grade": "auto",
        "start_strategy": "segment",
        "start_sec": None,
        "reuse": "balanced",
        "max_new_clips": None,
        "budget_limit_usd": None,
        "seed": None,
        "new_storyboard": False,
        "allow_mirror": False,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"count": 0},
        {"count": 51},
        {"duration_sec": 4.9},
        {"duration_sec": 90.5},
        {"provider": "sora"},
        {"preset": "anime"},
        {"lyric_style": "bounce"},
        {"reuse": "all"},
        {"start_strategy": "custom"},
        {"start_strategy": "custom", "start_sec": -1},
        {"color_grade": "Teal Orange;"},
        {"max_new_clips": -1},
        {"budget_limit_usd": -0.01},
        {"seed": -1},
        {"text_style": {"primary_color": "white"}},
    ],
)
def test_generation_settings_rejects(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        GenerationSettings(**overrides)


def test_generation_settings_accepts_bounds() -> None:
    assert GenerationSettings(count=1, duration_sec=5).count == 1
    assert GenerationSettings(count=50, duration_sec=90).duration_sec == 90
    custom = GenerationSettings(start_strategy="custom", start_sec=24.0, color_grade="teal_orange", custom_world="  ")
    assert custom.start_sec == 24.0 and custom.custom_world is None


def test_text_style_validation() -> None:
    style = TextStyle(primary_color="#fff", highlight_color="#4da3ff", outline_color="#000000")
    assert (style.primary_color, style.highlight_color, style.outline_color) == ("#FFFFFF", "#4DA3FF", "#000000")
    assert TextStyle().model_dump(exclude_none=True) == {}
    for bad in (
        {"primary_color": "#ffff"},
        {"primary_color": "rgb(0,0,0)"},
        {"font_size": 8},
        {"font_size": 400},
        {"bg_opacity": 1.5},
        {"position": "left"},
        {"font_weight": "heavy"},
        {"max_chars_per_line": 2},
        {"font_family": "Noto{\\b1}"},
        {"font_family": "A,B"},
    ):
        with pytest.raises(ValidationError):
            TextStyle(**bad)
    assert TextStyle(font_family="Noto Serif CJK JP").font_family == "Noto Serif CJK JP"


def test_lyric_models() -> None:
    line = LyricLine(id="l1", text="  そのまま  ", start=8.0, end=11.6, source="lrc")
    assert line.text == "  そのまま  "  # text is never rewritten
    with pytest.raises(ValidationError):
        LyricLine(id="l1", text="x", start=-1)
    with pytest.raises(ValidationError):
        LyricLine(id="l1", text="x", source="ai")
    with pytest.raises(ValidationError):
        LyricLinesUpdate(lines=[{"id": "a", "text": "x", "start": 5, "end": 4}])
    with pytest.raises(ValidationError):
        LyricLinesUpdate(lines=[{"id": "a", "text": "x"}, {"id": "a", "text": "y"}])
    ok = LyricLinesUpdate(
        lines=[{"id": "a", "text": "x", "start": 1, "end": 2, "words": [{"text": "x", "start": 1, "end": 2}]}]
    )
    assert ok.lines[0].words[0].text == "x"
    with pytest.raises(ValidationError):
        AutoTimeRequest(start=10, end=5)
    assert AutoTimeRequest().method == "heuristic"


def test_segment_rights_and_video_edit() -> None:
    assert SegmentUpdate(start=24, end=39).end == 39
    with pytest.raises(ValidationError):
        SegmentUpdate(start=10, end=12.5)
    with pytest.raises(ValidationError):
        RightsUpdate(items={"bogus": True})
    assert RightsUpdate(items={"audio_rights": True}).items == {"audio_rights": True}

    edit = VideoEdit(start=30, lyric_offsets={"l1": {"start": 1, "end": 2}}, shots=[{"index": 0, "clip_id": "c" * 32}])
    assert edit.shots[0].src_in is None
    with pytest.raises(ValidationError):
        VideoEdit(shots=[{"index": 0, "clip_id": "a"}, {"index": 0, "clip_id": "b"}])
    with pytest.raises(ValidationError):
        VideoEdit(lyric_offsets={"l1": {"start": 3, "end": 2}})
    with pytest.raises(ValidationError):
        VideoEdit(lyric_style="wobble")
