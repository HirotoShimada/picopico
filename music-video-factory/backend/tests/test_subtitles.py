"""Unit tests for ASS lyric generation (app.services.subtitles)."""

from __future__ import annotations

import itertools
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from app.services.subtitles import (
    _NO_LINE_END,
    _NO_LINE_START,
    LYRIC_STYLE_IDS,
    build_ass,
    escape_ass_text,
    layout_text,
    resolve_text_style,
    safe_area,
    wrap_text,
)

DIALOGUE = re.compile(
    r"^Dialogue: (?P<layer>\d+),(?P<start>[\d:.]+),(?P<end>[\d:.]+),(?P<style>[^,]*),"
    r"[^,]*,\d+,\d+,\d+,[^,]*,(?P<text>.*)$"
)
POS = re.compile(r"\\(?:pos|move)\(([-\d.]+),([-\d.]+)")
KARAOKE = re.compile(r"\\(kf|k)(\d+)")


def ass_seconds(stamp: str) -> float:
    hours, minutes, seconds = stamp.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def dialogues(ass: str) -> list[dict[str, Any]]:
    events = []
    for line in ass.splitlines():
        match = DIALOGUE.match(line)
        if match:
            item = match.groupdict()
            item["start_s"] = ass_seconds(item["start"])
            item["end_s"] = ass_seconds(item["end"])
            item["layer"] = int(item["layer"])
            events.append(item)
    return events


def main_events(ass: str) -> list[dict[str, Any]]:
    """Layer-1 events (the visible text, not soft-shadow copies)."""
    return [e for e in dialogues(ass) if e["layer"] == 1]


def visible_text(event_text: str) -> str:
    """Event text without override blocks; escaped braces/backslashes restored."""
    text = re.sub(r"(?<!\\)\{[^}]*\}", "", event_text)
    return text.replace("\\{", "{").replace("\\}", "}").replace("\\\u2060", "\\")


LINES = [
    {"id": "a", "text": "夜明け前の街を ひとりで歩いた", "start": 20.0, "end": 23.6},
    {"id": "b", "text": "叫べ 今この瞬間を", "start": 24.0, "end": 28.0},
    {"id": "c", "text": "忘れないように 胸に刻んで", "start": 28.0, "end": 32.0},
    {"id": "d", "text": "走れ 遠く遥か先へ", "start": 39.0, "end": 41.5},
    {"id": "e", "text": "君と見た空を 信じているから", "start": 50.0, "end": 54.0},
]


# --------------------------------------------------------------------------- timing


def test_times_are_relative_to_segment_and_clipped() -> None:
    ass = build_ass(LINES, style_id="simple", text_style={}, segment_start=24.0, duration=16.0)
    events = main_events(ass)
    texts = {visible_text(e["text"]) for e in events}
    assert "叫べ 今この瞬間を" in texts
    assert not any("夜明け" in t for t in texts), "line ending before the segment must be dropped"
    assert not any("君と見た空" in t for t in texts), "line after the segment must be dropped"
    by_text = {visible_text(e["text"]): e for e in events}
    first = by_text["叫べ 今この瞬間を"]
    assert (first["start_s"], first["end_s"]) == (0.0, 4.0)
    second = by_text["忘れないように 胸に刻んで"]
    assert (second["start_s"], second["end_s"]) == (4.0, 8.0)
    clipped = [e for e in events if "走れ" in visible_text(e["text"])]
    assert clipped and clipped[0]["start_s"] == 15.0 and clipped[0]["end_s"] == 16.0  # clipped at the end


def test_line_overlapping_segment_start_is_clipped_to_zero() -> None:
    ass = build_ass(LINES, style_id="fade", text_style={}, segment_start=22.0, duration=6.0)
    events = main_events(ass)
    first = min(events, key=lambda e: e["start_s"])
    assert first["start_s"] == 0.0
    assert first["end_s"] == pytest.approx(1.6, abs=0.011)
    assert "\\fad(0," in first["text"], "a line already on screen must not fade in"


def test_untimed_lines_skipped_and_missing_end_uses_next_start() -> None:
    lines = [
        {"text": "時間なし", "start": None, "end": None},
        {"text": "終わりなし", "start": 1.0, "end": None},
        {"text": "次の行", "start": 3.5, "end": 5.0},
        {"text": "   ", "start": 6.0, "end": 7.0},
    ]
    events = main_events(build_ass(lines, style_id="simple", text_style={}, segment_start=0.0, duration=10.0))
    spans = {visible_text(e["text"]): (e["start_s"], e["end_s"]) for e in events}
    assert "時間なし" not in spans
    assert spans["終わりなし"] == (1.0, 3.5)
    assert spans["次の行"] == (3.5, 5.0)
    assert len(spans) == 2


def test_overlapping_lines_are_trimmed_to_avoid_collisions() -> None:
    lines = [
        {"text": "一行目", "start": 0.0, "end": 5.0},
        {"text": "二行目", "start": 3.0, "end": 6.0},
    ]
    events = main_events(build_ass(lines, style_id="simple", text_style={}, segment_start=0.0, duration=10.0))
    spans = sorted((e["start_s"], e["end_s"]) for e in events)
    assert spans == [(0.0, 3.0), (3.0, 6.0)]


# --------------------------------------------------------------------------- escaping


def test_escape_ass_text_neutralises_override_and_escape_sequences() -> None:
    assert escape_ass_text("a{b}c") == "a\\{b\\}c"
    assert escape_ass_text("x\\Ny") == "x\\\u2060Ny"
    assert escape_ass_text("改行\nあり") == "改行 あり"
    assert escape_ass_text("{\\b1}") == "\\{\\\u2060b1\\}"


def test_build_ass_escapes_user_text_and_keeps_one_event_per_line() -> None:
    nasty = "危険{\\pos(0,0)\\fs200}文字\\N列\n改行"
    ass = build_ass(
        [{"text": nasty, "start": 0.0, "end": 2.0}], style_id="simple", text_style={}, segment_start=0.0, duration=2.0
    )
    events = main_events(ass)
    assert events, ass
    for event in events:
        # Only our own leading override block may exist; everything else is literal text.
        blocks = re.findall(r"(?<!\\)\{[^}]*\}", event["text"])
        assert len(blocks) == 1 and event["text"].startswith(blocks[0])
        assert "\\fs200" not in blocks[0]
    joined = "".join(visible_text(e["text"]) for e in events)
    assert joined.replace(" ", "") == nasty.replace("\n", "").replace(" ", "")
    assert all(
        line.startswith(
            ("Dialogue:", "[", "Format:", "Style:", ";", "Title:", "Script", "Play", "Wrap", "Scaled", "YCbCr")
        )
        or not line
        for line in ass.splitlines()
    )


# --------------------------------------------------------------------------- wrapping

_NO_START, _NO_END = _NO_LINE_START, _NO_LINE_END


@pytest.mark.parametrize(
    "text",
    [
        "夜明け前の街を ひとりで歩いた",
        "消えそうな光を両手で包んで、それでも前を向いていた。",
        "「さよなら」って言えなかったんだ ずっと",
        "チョコレートみたいなキャンディーをちょうだいっ！",
        "名前も知らない風に吹かれてもっと遠くへ行こうよ",
        "ラララ　ラララ　僕らは歌う",
        "君がいたから。僕は、ここにいる！？",
    ],
)
@pytest.mark.parametrize("max_chars", [6, 8, 10, 14])
def test_wrap_respects_kinsoku_and_two_lines(text: str, max_chars: int) -> None:
    lines, spans = wrap_text(text, max_width=max_chars * 0.7, font_size=1.0)
    assert 1 <= len(lines) <= 2
    for line in lines:
        assert line == line.strip()
        assert line[0] not in _NO_START, f"line starts with a forbidden character: {lines}"
        assert line[-1] not in _NO_END, f"line ends with an opening bracket: {lines}"
    assert "".join(lines).replace(" ", "").replace("\u3000", "") == text.replace(" ", "").replace("\u3000", "")
    for (a, b), line in zip(spans, lines, strict=True):
        assert text[a:b] == line


def test_wrap_prefers_space_and_balances() -> None:
    lines, _ = wrap_text("夜明け前の街を ひとりで歩いた", max_width=14 * 0.7, font_size=1.0)
    assert lines == ["夜明け前の街を", "ひとりで歩いた"]
    lines, _ = wrap_text("それでも僕らは 前を向いていた", max_width=10 * 0.7, font_size=1.0)
    assert lines == ["それでも僕らは", "前を向いていた"]


def test_wrap_short_text_stays_on_one_line() -> None:
    lines, _ = wrap_text("叫べ 今この瞬間を", max_width=14 * 0.7, font_size=1.0)
    assert lines == ["叫べ 今この瞬間を"]


def test_wrap_prefers_bunsetsu_boundary_without_spaces() -> None:
    lines, _ = wrap_text("名前も知らない風に吹かれて", max_width=8 * 0.7, font_size=1.0)
    assert len(lines) == 2
    # never split okurigana / compounds: 吹|かれ or 名|前 would be wrong
    assert not lines[1].startswith(("かれて", "前")), lines
    assert lines[0][-1] in "もいにてをはがのへと" or lines[1][0] in "知風吹", lines


def test_wrap_never_breaks_inside_latin_words() -> None:
    text = "Hello wonderful world of music and dreams"
    lines, _ = wrap_text(text, max_width=12 * 0.7, font_size=1.0)
    assert len(lines) == 2
    words = set(text.split())
    for line in lines:
        assert set(line.split()) <= words
    mixed = "今夜はDancing in the moonlight"
    lines, _ = wrap_text(mixed, max_width=9 * 0.7, font_size=1.0)
    assert all(w in mixed.split() or w.startswith("今夜は") for line in lines for w in line.split())
    assert "Danc" not in lines[0] or "Dancing" in lines[0]


def test_layout_shrinks_overlong_text_instead_of_overflowing() -> None:
    spec = resolve_text_style("simple", {})
    text = "とても長い歌詞の行がここにあって画面の幅に収まりきらないほど続いていくのでどうしよう"
    layout = layout_text(text, spec)
    assert len(layout.lines) == 2
    assert layout.font_size < spec.font_size
    area = safe_area()
    for line in layout.lines:
        width = sum(0.7 if ord(ch) > 0x2E80 else 0.45 for ch in line) * layout.font_size
        assert width <= area.right - area.left


# --------------------------------------------------------------------------- karaoke


def _kara(text: str) -> tuple[int, int]:
    """(sum of \\kf, total of all karaoke durations) in centiseconds."""
    sweeps = sum(int(v) for tag, v in KARAOKE.findall(text) if tag == "kf")
    total = sum(int(v) for _, v in KARAOKE.findall(text))
    return sweeps, total


def test_karaoke_even_split_sums_to_line_duration() -> None:
    lines = [{"text": "叫べ 今この瞬間を", "start": 24.0, "end": 28.0}]
    events = main_events(build_ass(lines, style_id="karaoke", text_style={}, segment_start=24.0, duration=15.0))
    assert len(events) == 1
    sweeps, total = _kara(events[0]["text"])
    assert sweeps == pytest.approx(400, abs=2)
    assert total == pytest.approx(400, abs=2)
    assert events[0]["text"].count("\\kf") == 8  # one syllable per CJK character


def test_karaoke_two_visual_lines_continue_the_sweep() -> None:
    lines = [{"text": "夜明け前の街を ひとりで歩いた", "start": 10.0, "end": 13.6}]
    events = sorted(
        main_events(build_ass(lines, style_id="karaoke", text_style={}, segment_start=10.0, duration=15.0)),
        key=lambda e: float(POS.search(e["text"]).group(2)),
    )
    assert len(events) == 2
    first_sweep, first_total = _kara(events[0]["text"])
    second_text = events[1]["text"]
    lead = re.search(r"\}\{\\k(\d+)\}", second_text)
    assert lead and int(lead.group(1)) == first_total
    second_sweep, _ = _kara(second_text)
    assert first_sweep + second_sweep == pytest.approx(360, abs=2)


def test_karaoke_uses_word_timings_when_they_match() -> None:
    words = [
        {"text": "叫べ", "start": 24.2, "end": 24.9},
        {"text": "今", "start": 25.1, "end": 25.5},
        {"text": "この", "start": 25.5, "end": 26.0},
        {"text": "瞬間を", "start": 26.0, "end": 27.5},
    ]
    lines = [{"text": "叫べ 今この瞬間を", "start": 24.0, "end": 28.0, "words": words}]
    text = main_events(build_ass(lines, style_id="karaoke", text_style={}, segment_start=24.0, duration=15.0))[0][
        "text"
    ]
    values = [(tag, int(v)) for tag, v in KARAOKE.findall(text)]
    assert values == [("k", 20), ("kf", 70), ("k", 20), ("kf", 40), ("kf", 50), ("kf", 150)]
    assert visible_text(text) == "叫べ 今この瞬間を"


def test_karaoke_line_started_before_segment_is_partially_sung() -> None:
    lines = [{"text": "あいうえお", "start": 10.0, "end": 15.0}]
    text = main_events(build_ass(lines, style_id="karaoke", text_style={}, segment_start=12.0, duration=10.0))[0][
        "text"
    ]
    values = [int(v) for tag, v in KARAOKE.findall(text) if tag == "kf"]
    assert values[:2] == [0, 0]  # already sung before the clip starts
    assert sum(values) == pytest.approx(300, abs=2)


# --------------------------------------------------------------------------- fade


def test_fade_aligns_to_beats_when_given() -> None:
    lines = [{"text": "叫べ 今この瞬間を", "start": 24.0, "end": 28.0}]
    beats = [i * 0.5 for i in range(120)]  # 120 BPM: half-beat grid gives 0.25 s
    with_beats = main_events(
        build_ass(lines, style_id="fade", text_style={}, segment_start=24.0, duration=15.0, beats=beats)
    )[0]["text"]
    assert "\\fad(250,250)" in with_beats
    beats_90 = [i * (60 / 90) for i in range(100)]  # 90 BPM: half-beat = 0.333 s
    lines_90 = [{"text": "テスト", "start": beats_90[12], "end": beats_90[18]}]
    text_90 = main_events(
        build_ass(lines_90, style_id="fade", text_style={}, segment_start=0.0, duration=30.0, beats=beats_90)
    )[0]["text"]
    assert "\\fad(333,333)" in text_90
    without = main_events(build_ass(lines, style_id="fade", text_style={}, segment_start=24.0, duration=15.0))[0]
    assert "\\fad(300,350)" in without["text"]


# --------------------------------------------------------------------------- styles


def test_styles_are_distinct_and_horizontal() -> None:
    style_lines = {}
    for style in LYRIC_STYLE_IDS:
        ass = build_ass(LINES, style_id=style, text_style={}, segment_start=24.0, duration=15.0)
        style_lines[style] = next(line for line in ass.splitlines() if line.startswith("Style: "))
        for token in ("\\frz", "\\frx", "\\fry", "\\fr", "Vertical"):
            assert token not in ass
        fontname = style_lines[style].split(",")[1]
        assert not fontname.startswith("@")  # '@' fonts render vertically
    assert len(set(style_lines.values())) == len(LYRIC_STYLE_IDS)
    assert "Noto Serif CJK JP" in style_lines["cinematic"]
    cinematic = build_ass(LINES, style_id="cinematic", text_style={}, segment_start=24.0, duration=15.0)
    assert any(line.startswith("Style: LyricBox,") and line.split(",")[15] == "3" for line in cinematic.splitlines())
    assert "Noto Sans CJK JP Light" in style_lines["minimal"]
    assert style_lines["karaoke"].split(",")[3] == "&H00FFA34D"  # highlight (#4DA3FF) is the sweep colour


def test_text_style_overrides_apply() -> None:
    ass = build_ass(
        LINES,
        style_id="simple",
        segment_start=24.0,
        duration=4.0,
        text_style={
            "font_size": 80,
            "primary_color": "#FFEE00",
            "font_weight": "light",
            "position": "top",
            "letter_spacing": 4,
            "outline_width": 2,
            "bg_opacity": 0.5,
        },
    )
    style = next(line for line in ass.splitlines() if line.startswith("Style: ")).split(",")
    assert style[1] == "Noto Sans CJK JP Light"
    assert style[2] == "80"
    assert style[3] == "&H0000EEFF"
    assert style[13] == "4" and style[15] == "1" and style[16] == "2"  # text keeps its outline in box mode
    box = next(line for line in ass.splitlines() if line.startswith("Style: LyricBox,")).split(",")
    assert box[15] == "3" and box[5] == "&H80000000"  # 50 % black box under the text
    y = float(POS.search(main_events(ass)[0]["text"]).group(2))
    assert y < 600  # top position


def test_invalid_overrides_fall_back_to_defaults() -> None:
    spec = resolve_text_style(
        "karaoke",
        {
            "font_size": "huge",
            "primary_color": "red",
            "position": "left",
            "max_chars_per_line": 1000,
            "font_family": "Bad,{Font}\\",
        },
    )
    default = resolve_text_style("karaoke", {})
    assert spec.font_size == default.font_size
    assert spec.primary_color == default.primary_color
    assert spec.position == default.position
    assert spec.max_chars_per_line == 40
    assert "," not in spec.font_family and "{" not in spec.font_family and "\\" not in spec.font_family


# --------------------------------------------------------------------------- safe area

LONG_LINE = {"text": "君と見た空を 信じているから", "start": 0.0, "end": 3.0}
SHORT_LINE = {"text": "叫べ", "start": 0.0, "end": 3.0}
MAX_LINE = {"text": "とても長い歌詞の行がここにあって画面の幅に収まりきらないほど続いていく", "start": 0.0, "end": 3.0}


def _block_bounds(ass: str, style_id: str, text_style: dict[str, Any], height: int) -> tuple[float, float]:
    """Top/bottom of the drawn block from event positions (line box + effect extents)."""
    from app.services.subtitles import _ui_scale, _vertical_extents  # type: ignore[attr-defined]

    spec = resolve_text_style(style_id, text_style)
    width = int(re.search(r"PlayResX: (\d+)", ass).group(1))
    above, below = _vertical_extents(spec, _ui_scale(width, height))
    tops, bottoms = [], []
    for event in dialogues(ass):
        match = POS.search(event["text"])
        y = float(match.group(2))
        fs_match = re.search(r"\\fs([\d.]+)", event["text"])
        font_size = float(fs_match.group(1)) if fs_match else spec.font_size * _ui_scale(width, height)
        tops.append(y)
        bottoms.append(y + font_size)
    return min(tops) - above, max(bottoms) + below


@pytest.mark.parametrize("style_id", LYRIC_STYLE_IDS)
@pytest.mark.parametrize("position", ["top", "center", "bottom"])
@pytest.mark.parametrize("line", [SHORT_LINE, LONG_LINE, MAX_LINE], ids=["short", "two_lines", "overlong"])
@pytest.mark.parametrize("size", [(1080, 1920), (720, 1280)], ids=["1080p", "720p"])
def test_geometry_inside_safe_area(style_id: str, position: str, line: dict[str, Any], size: tuple[int, int]) -> None:
    width, height = size
    ass = build_ass(
        [line],
        style_id=style_id,
        text_style={"position": position},
        segment_start=0.0,
        duration=3.0,
        width=width,
        height=height,
    )
    area = safe_area(width, height)
    top, bottom = _block_bounds(ass, style_id, {"position": position}, height)
    assert top >= area.top - 0.5, (top, area)
    assert bottom <= area.bottom + 0.5, (bottom, area)


def _render_ass_frame(ass: str, tmp_path: Path, width: int, height: int, at: float) -> np.ndarray:
    ass_path = tmp_path / "t.ass"
    ass_path.write_text(ass, encoding="utf-8")
    cmd = [
        shutil.which("ffmpeg") or "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={width}x{height}:r=10:d={at + 0.5}",
        "-vf",
        f"subtitles=filename={ass_path}",
        "-ss",
        f"{at}",
        "-frames:v",
        "1",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-",
    ]
    raw = subprocess.run(cmd, capture_output=True, check=True, timeout=60).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(height, width)


@pytest.mark.parametrize("style_id", LYRIC_STYLE_IDS)
@pytest.mark.parametrize("position", ["top", "center", "bottom"])
def test_rendered_pixels_inside_safe_area(style_id: str, position: str, tmp_path: Path) -> None:
    """Rasterise with libass (white text on black) and check every lit pixel row."""
    width, height = 1080, 1920
    lines = [{"text": "夜明け前の街を ひとりで歩いた", "start": 0.0, "end": 3.0}]
    text_style: dict[str, Any] = {
        "position": position,
        "primary_color": "#FFFFFF",
        "outline_color": "#FFFFFF",
        "highlight_color": "#FFFFFF",
    }
    ass = build_ass(lines, style_id=style_id, text_style=text_style, segment_start=0.0, duration=3.0)
    frame = _render_ass_frame(ass, tmp_path, width, height, at=1.5)
    rows = np.nonzero(frame.max(axis=1) > 8)[0]
    cols = np.nonzero(frame.max(axis=0) > 8)[0]
    assert rows.size, "nothing rendered"
    area = safe_area(width, height)
    assert rows.min() >= area.top and rows.max() <= area.bottom, (rows.min(), rows.max())
    assert cols.min() >= 0.04 * width and cols.max() <= 0.96 * width


def test_rendered_pixels_inside_safe_area_small_canvas(tmp_path: Path) -> None:
    width, height = 720, 1280
    ass = build_ass(
        [LONG_LINE],
        style_id="cinematic",
        text_style={"position": "bottom", "bg_opacity": 1.0},
        segment_start=0.0,
        duration=3.0,
        width=width,
        height=height,
    )
    frame = _render_ass_frame(ass.replace("&H00000000", "&H00FFFFFF"), tmp_path, width, height, at=1.5)
    rows = np.nonzero(frame.max(axis=1) > 8)[0]
    area = safe_area(width, height)
    assert area.top == 180 and area.bottom == 1280 - 253
    assert rows.min() >= area.top and rows.max() <= area.bottom


# --------------------------------------------------------------------------- same-time lines (bilingual / duet LRC)

BILINGUAL = [
    {"id": "l001", "text": "叫べ 今この瞬間を", "start": 0.5, "end": 2.5},
    {"id": "l002", "text": "Shout out this very moment", "start": 0.5, "end": 2.5},
    {"id": "l003", "text": "忘れないように 胸に刻んで", "start": 2.5, "end": 6.5},
    {"id": "l004", "text": "So that we never forget it", "start": 2.52, "end": 6.5},
]


@pytest.mark.parametrize("style_id", LYRIC_STYLE_IDS)
def test_same_start_lines_are_stacked_not_overprinted(style_id: str) -> None:
    ass = build_ass(BILINGUAL, style_id=style_id, text_style={}, segment_start=0.0, duration=8.0)
    events = main_events(ass)
    spec = resolve_text_style(style_id, {})
    placed = [(e["start_s"], e["end_s"], POS.search(e["text"]).groups()) for e in events]
    assert len(placed) == len(set(placed)), "two events share both a position and a time range"
    for start, end in ((0.5, 2.5), (2.5, 6.5)):
        group = [e for e in events if (e["start_s"], e["end_s"]) == (start, end)]
        assert len(group) >= 2, (start, [(e["start_s"], e["end_s"]) for e in events])
        ys = sorted(float(POS.search(e["text"]).group(2)) for e in group)
        sizes = [float(m.group(1)) for e in group if (m := re.search(r"\\fs([\d.]+)", e["text"]))]
        font_size = min(sizes) if sizes else spec.font_size
        assert all(b - a >= font_size - 0.5 for a, b in itertools.pairwise(ys)), ys
    shown = " ".join(visible_text(e["text"]) for e in events if e["start_s"] == 0.5)
    assert "叫べ" in shown and "Shout" in shown and "moment" in shown
    # The stacked block still respects the Stories safe area.
    area = safe_area(1080, 1920)
    top, bottom = _block_bounds(ass, style_id, {}, 1920)
    assert top >= area.top - 0.5 and bottom <= area.bottom + 0.5


def test_same_start_karaoke_sweeps_only_the_first_line() -> None:
    lines = [*BILINGUAL[:2]]
    ass = build_ass(lines, style_id="karaoke", text_style={"primary_color": "#DDEEFF"}, segment_start=0.0,
                    duration=3.0)  # fmt: skip
    events = sorted(main_events(ass), key=lambda e: float(POS.search(e["text"]).group(2)))
    first = [e for e in events if "叫" in visible_text(e["text"]) or "瞬間" in visible_text(e["text"])]
    second = [e for e in events if e not in first]
    assert first and second
    assert all(KARAOKE.search(e["text"]) for e in first)
    for event in second:
        assert not KARAOKE.search(event["text"])
        assert "\\1c&HFFEEDD&" in event["text"]  # static, in the not-yet-sung colour


# --------------------------------------------------------------------------- background box


def test_background_box_is_black_and_text_keeps_its_outline() -> None:
    ass = build_ass(LINES, style_id="cinematic", text_style={"outline_color": "#FFFFFF", "outline_width": 3},
                    segment_start=24.0, duration=4.0)  # fmt: skip
    styles = {line.split(",")[0][len("Style: "):]: line.split(",") for line in ass.splitlines()
              if line.startswith("Style: ")}  # fmt: skip
    box, text = styles["LyricBox"], styles["Lyric"]
    assert box[15] == "3" and box[5].endswith("000000"), box  # box colour: black at bg_opacity
    assert box[3].startswith("&HFF")  # glyph fill invisible: the box event draws only the box
    assert text[15] == "1" and text[16] == "3" and text[5].endswith("FFFFFF"), text  # user's outline kept
    boxes = [e for e in dialogues(ass) if e["style"] == "LyricBox"]
    texts = main_events(ass)
    assert boxes and len(boxes) == len(texts)
    for box_event, text_event in zip(boxes, texts, strict=True):
        assert box_event["layer"] == 0 and text_event["layer"] == 1
        assert POS.search(box_event["text"]).groups() == POS.search(text_event["text"]).groups()
        assert (box_event["start_s"], box_event["end_s"]) == (text_event["start_s"], text_event["end_s"])
        assert visible_text(box_event["text"]) == visible_text(text_event["text"])


def test_rendered_background_box_is_dark_with_a_coloured_outline(tmp_path: Path) -> None:
    width, height = 540, 960
    lines = [{"text": "叫べ 今この瞬間を", "start": 0.0, "end": 3.0}]
    ass = build_ass(lines, style_id="cinematic", text_style={"outline_color": "#FF0000", "outline_width": 4},
                    segment_start=0.0, duration=3.0, width=width, height=height)  # fmt: skip
    ass_path = tmp_path / "box.ass"
    ass_path.write_text(ass, encoding="utf-8")
    cmd = [shutil.which("ffmpeg") or "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
           f"color=c=0x808080:s={width}x{height}:r=10:d=2", "-vf", f"subtitles=filename={ass_path}",
           "-ss", "1.5", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]  # fmt: skip
    raw = subprocess.run(cmd, capture_output=True, check=True, timeout=60).stdout
    frame = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3).astype(int)
    y = float(POS.search(main_events(ass)[0]["text"]).group(2))
    band = frame[int(y) + 4 : int(y) + 10]
    changed = np.nonzero(np.abs(band - 128).max(axis=2).max(axis=0) > 20)[0]
    assert changed.size, "nothing drawn"
    inside = band[:, changed.min() + 2]  # just inside the box's left edge, left of the first glyph
    assert inside.max() < 70, inside  # black box at 55 %, not the (red) outline colour
    red = (frame[..., 0] > 150) & (frame[..., 1] < 90) & (frame[..., 2] < 90)
    assert red.sum() > 100  # the text outline is drawn inside the box
