"""Tests for ``app.services.lyrics_timing`` (ARCHITECTURE.md §6.2): heuristic auto-timing
from a synthetic analysis (known vocal regions) and from the real demo song."""

from __future__ import annotations

import copy
import importlib.util
import itertools
import math
import statistics
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from app.services.lyrics_parse import parse_lyrics
from app.services.lyrics_timing import (
    BEAT_SNAP_SEC,
    MIN_LINE_SEC,
    auto_time,
    fill_words,
    line_weight,
    text_units,
)

DEMO_PHRASE_STARTS = [8.0, 12.0, 16.0, 20.0, 24.0, 28.0, 32.0, 36.0, 48.0, 52.0]


def make_analysis(
    duration: float,
    regions: Sequence[tuple[float, float]],
    *,
    hop: float = 0.1,
    floor: float = 0.05,
    dips: Sequence[float] = (),
    bpm: float = 120.0,
    beat_offset: float = 0.02,
) -> dict[str, Any]:
    """Synthetic AudioAnalysis: vocal activity 0.6-1.0 (syllable-like ripple) inside
    ``regions``, ``floor`` elsewhere, a 0.3 s dip to 0.35 at each time in ``dips``."""
    n = round(duration / hop)
    values = [floor] * n
    for a, b in regions:
        for i in range(round(a / hop), min(n, round(b / hop))):
            values[i] = 0.8 + 0.2 * math.sin(i * 1.7)
    for t in dips:
        for i in range(round(t / hop) - 1, round(t / hop) + 2):
            values[i] = 0.35
    period = 60.0 / bpm
    beats = [round(beat_offset + k * period, 3) for k in range(int((duration - beat_offset) / period) + 1)]
    return {
        "version": 1,
        "duration": duration,
        "bpm": bpm,
        "beats": beats,
        "downbeats": beats[::4],
        "vocal_activity": {"hop": hop, "values": [round(v, 3) for v in values]},
    }


def untimed(*texts: str) -> list[dict[str, Any]]:
    lines, _ = parse_lyrics("\n".join(texts), "txt")
    return [dict(line) for line in lines]


def assert_well_formed(lines: Sequence[dict[str, Any]], lo: float, hi: float) -> None:
    """Ordered, non-overlapping, inside [lo, hi], ≥ MIN_LINE_SEC, words consistent with the text."""
    previous_end = lo
    for line in lines:
        start, end = line["start"], line["end"]
        assert start is not None and end is not None, line
        assert lo - 1e-6 <= start and end <= hi + 1e-6, line
        assert start >= previous_end - 1e-6, (line, previous_end)
        assert end - start >= MIN_LINE_SEC - 1e-6, line
        previous_end = end
        words = line["words"]
        assert words, line
        cursor = 0
        for word in words:
            found = line["text"].find(word["text"], cursor)
            assert found >= 0, (word, line["text"])  # word texts appear in order in the line text
            cursor = found + len(word["text"])
        assert words[0]["start"] == pytest.approx(start, abs=1e-3)
        assert all(a["end"] == pytest.approx(b["start"], abs=1e-3) for a, b in itertools.pairwise(words))
        assert all(w["start"] <= w["end"] for w in words)
        assert words[-1]["end"] <= end + 1e-3


# --------------------------------------------------------------------------- text units


@pytest.mark.parametrize(
    ("text", "units"),
    [
        ("叫べ、今", ["叫", "べ、", "今"]),
        ("きゃりー", ["きゃ", "り", "ー"]),
        ("「夜」 Hello, world! don't", ["「夜」", "Hello,", "world!", "don't"]),
        ("ﾊﾟﾝ", ["ﾊﾟ", "ﾝ"]),
        ("夜明け前の街を ひとりで", ["夜", "明", "け", "前", "の", "街", "を", "ひ", "と", "り", "で"]),
        ("♪", ["♪"]),
        ("…あ", ["…あ"]),
        ("", []),
    ],
)
def test_text_units(text: str, units: list[str]) -> None:
    assert [text[u.start : u.end] for u in text_units(text)] == units


def test_line_weight_counts_morae_not_spaces() -> None:
    assert line_weight("夜明け") == pytest.approx(4.0)  # kanji ≈ 1.5 morae
    assert line_weight("よ あ け") == pytest.approx(3.0)
    assert line_weight("beautiful time") == pytest.approx(4.0)  # 3 + 1 syllables
    assert line_weight("♪") == 1.0  # floor


def test_fill_words_even_split_by_morae() -> None:
    words = fill_words("叫べ 今", 10.0, 14.0)
    assert words == [
        {"text": "叫", "start": 10.0, "end": 11.5},
        {"text": "べ", "start": 11.5, "end": 12.5},
        {"text": "今", "start": 12.5, "end": 14.0},
    ]
    assert fill_words("", 0.0, 1.0) is None


# --------------------------------------------------------------------------- synthetic analysis

PHRASES = [(5.0, 8.5), (9.0, 12.5), (15.0, 18.0), (18.4, 22.0), (28.0, 31.5)]
PHRASE_TEXTS = [
    "夜明け前の街を ひとりで歩いた",
    "消えそうな光を 両手で包んで",
    "叫べ 今この瞬間を",
    "忘れないように 胸に刻んで",
    "君と見た空を 信じているから",
]


def test_lines_follow_known_vocal_regions() -> None:
    analysis = make_analysis(40.0, PHRASES)
    lines = untimed(*PHRASE_TEXTS)
    before = copy.deepcopy(lines)
    out, warnings = auto_time(lines, analysis)
    assert lines == before  # input untouched
    assert warnings == []
    assert [line["text"] for line in out] == PHRASE_TEXTS
    assert [line["id"] for line in out] == [line["id"] for line in lines]
    assert_well_formed(out, 0.0, 40.0)
    for line, (a, b) in zip(out, PHRASES, strict=True):
        assert abs(line["start"] - a) <= BEAT_SNAP_SEC, (line, a)
        assert line["start"] in analysis["beats"]  # snapped (beats at x.02 / x.52)
        assert line["end"] >= b - 0.25
        assert line["source"] == "heuristic"
        assert line["confidence"] == pytest.approx(0.3)


def test_two_lines_share_one_long_phrase_split_at_the_dip() -> None:
    # Three 8 s phrases without pauses, each with a short dip after its first line.
    phrases = [(4.0, 12.0), (16.0, 24.0), (28.0, 36.0)]
    analysis = make_analysis(40.0, phrases, dips=(8.3, 20.3, 32.3))
    lines = untimed(
        "夜明け前の街を", "ひとりで歩いた", "消えそうな光を", "両手で包んで", "名前も知らない", "風に吹かれて"
    )
    out, _ = auto_time(lines, analysis)
    assert_well_formed(out, 0.0, 40.0)
    expected_starts = [4.0, 8.3, 16.0, 20.3, 28.0, 32.3]
    for line, expected in zip(out, expected_starts, strict=True):
        assert abs(line["start"] - expected) <= 0.4, (line, expected)


def test_one_line_spans_several_breaths() -> None:
    # Two lines, each sung as two phrases separated by a short breath.
    phrases = [(5.0, 7.5), (7.9, 10.0), (14.0, 16.5), (16.9, 19.0)]
    out, _ = auto_time(
        untimed("夜明け前の街を ひとりで歩いた", "消えそうな光を 両手で包んで"), make_analysis(25.0, phrases)
    )
    assert_well_formed(out, 0.0, 25.0)
    assert abs(out[0]["start"] - 5.0) <= BEAT_SNAP_SEC and abs(out[1]["start"] - 14.0) <= BEAT_SNAP_SEC
    assert out[0]["end"] == pytest.approx(10.0, abs=0.15)


def test_timed_lines_are_kept_as_anchors() -> None:
    analysis = make_analysis(40.0, [(5.0, 9.0), (20.0, 24.0), (30.0, 34.0)])
    lines = untimed("一行目の歌詞です", "二行目の歌詞です", "三行目の歌詞です")
    lines[1].update(start=20.5, end=23.5, source="lrc")
    anchor = copy.deepcopy(lines[1])
    out, warnings = auto_time(lines, analysis)
    assert out[1] == anchor
    assert abs(out[0]["start"] - 5.0) <= BEAT_SNAP_SEC and out[0]["end"] <= 20.5
    assert abs(out[2]["start"] - 30.0) <= BEAT_SNAP_SEC
    assert any("1 行は変更していません" in w for w in warnings)

    retimed, _ = auto_time(lines, analysis, overwrite_timed=True)
    assert all(line["source"] == "heuristic" for line in retimed)
    assert abs(retimed[1]["start"] - 20.0) <= BEAT_SNAP_SEC


def test_window_limits_placement() -> None:
    analysis = make_analysis(60.0, [(a, a + 3.5) for a in range(2, 58, 4)])
    out, _ = auto_time(untimed("一つ目の行", "二つ目の行", "三つ目の行"), analysis, start=25.0, end=40.0)
    assert_well_formed(out, 25.0, 40.0)
    assert abs(out[0]["start"] - 26.0) <= BEAT_SNAP_SEC  # first phrase inside the window


def test_overwrite_with_window_only_retimes_lines_inside_it() -> None:
    analysis = make_analysis(60.0, [(5.0, 9.0), (26.0, 30.0), (50.0, 54.0)])
    lines = untimed("最初", "真ん中", "最後")
    for line, (s, e) in zip(lines, [(5.0, 9.0), (33.0, 36.0), (50.0, 54.0)], strict=True):
        line.update(start=s, end=e, source="manual")
    out, _ = auto_time(lines, analysis, start=20.0, end=45.0, overwrite_timed=True)
    assert out[0] == lines[0] and out[2] == lines[2]
    assert out[1]["source"] == "heuristic"
    assert abs(out[1]["start"] - 26.0) <= BEAT_SNAP_SEC


def test_no_vocal_activity_spreads_evenly_with_warning() -> None:
    analysis = make_analysis(30.0, [])
    analysis["vocal_activity"]["values"] = [0.0] * 300
    out, warnings = auto_time(untimed("あいう", "かきくけこさしすせそ", "たちつ"), analysis, start=10.0, end=26.0)
    assert_well_formed(out, 10.0, 26.0)
    assert out[0]["start"] == pytest.approx(10.02, abs=1e-3)  # span start snapped to the beat
    durations = [line["end"] - line["start"] for line in out]
    assert durations[1] > 2.5 * durations[0]  # proportional to text length
    assert all(line["confidence"] == pytest.approx(0.15) for line in out)
    assert any("均等" in w for w in warnings)


def test_loudness_is_used_when_vocal_activity_is_missing() -> None:
    analysis = make_analysis(30.0, [])
    del analysis["vocal_activity"]
    analysis["loudness"] = {
        "hop": 0.5,
        "rms_db": [-40.0 if not (10 <= i * 0.5 < 14 or 20 <= i * 0.5 < 24) else -8.0 for i in range(60)],
    }
    out, warnings = auto_time(untimed("一行目の歌詞", "二行目の歌詞"), analysis)
    assert warnings == []
    assert abs(out[0]["start"] - 10.0) <= BEAT_SNAP_SEC and abs(out[1]["start"] - 20.0) <= BEAT_SNAP_SEC


def test_without_analysis_lines_get_provisional_times() -> None:
    out, warnings = auto_time(untimed("一", "二", "三"), None)
    assert_well_formed(out, 0.0, 12.0)
    assert any("楽曲の長さが分からない" in w for w in warnings)


def test_crowded_span_packs_lines_and_warns() -> None:
    analysis = make_analysis(20.0, [(5.0, 9.0)])
    out, warnings = auto_time(untimed(*[f"行{k}" for k in range(8)]), analysis, start=5.0, end=9.0)
    starts = [line["start"] for line in out]
    assert starts == sorted(starts) and starts[0] == pytest.approx(5.0)
    assert all(a["end"] <= b["start"] + 1e-6 for a, b in itertools.pairwise(out))
    assert out[-1]["end"] == pytest.approx(9.0)
    assert any("詰めて配置" in w for w in warnings)


def test_latin_lines_are_weighted_by_syllables() -> None:
    analysis = make_analysis(20.0, [(2.0, 14.0)])
    out, _ = auto_time(untimed("I love you", "Everybody wants to rule the world tonight"), analysis)
    assert_well_formed(out, 0.0, 20.0)
    assert (out[1]["end"] - out[1]["start"]) > 2 * (out[0]["end"] - out[0]["start"])
    assert [w["text"] for w in out[0]["words"]] == ["I", "love", "you"]


def test_nothing_to_do_when_all_lines_are_timed() -> None:
    lines, _ = parse_lyrics("[00:01.00]一\n[00:03.00]二\n")
    out, warnings = auto_time(lines, make_analysis(10.0, [(1.0, 5.0)]))
    assert out == lines
    assert warnings and "対象となる行がありません" in warnings[0]


# --------------------------------------------------------------------------- real demo song


@pytest.fixture(scope="module")
def demo_analysis(demo_dir: Path) -> dict[str, Any]:
    if importlib.util.find_spec("app.services.audio_analysis") is None:
        pytest.skip("app.services.audio_analysis is not implemented yet")
    from app.services.audio_analysis import analyze_audio

    return analyze_audio(str(demo_dir / "demo_song.wav"))


@pytest.mark.slow
def test_demo_song_auto_time_matches_phrase_starts(demo_dir: Path, demo_analysis: dict[str, Any]) -> None:
    lines, _ = parse_lyrics((demo_dir / "demo_lyrics_ja.txt").read_text(encoding="utf-8"))
    out, warnings = auto_time(lines, demo_analysis)
    assert warnings == []
    assert_well_formed(out, 0.0, demo_analysis["duration"])
    errors = [abs(line["start"] - truth) for line, truth in zip(out, DEMO_PHRASE_STARTS, strict=True)]
    mae = statistics.mean(errors)
    print(f"\ndemo song heuristic auto-time: mean |start error| = {mae:.3f} s, max = {max(errors):.3f} s")
    assert mae < 0.3, errors
    assert max(errors) < 1.0, errors


def test_random_inputs_keep_invariants() -> None:
    """Seeded fuzz: random activity, windows, anchors and texts never break the contract."""
    import random

    rng = random.Random(1)
    pool = ["夜明け前の街を", "ひとりで", "Hello world", "叫べ", "♪", "君と見た空を 信じているから", "Everybody wants"]
    for _ in range(150):
        duration = rng.uniform(5, 180)
        n = round(duration / 0.1)
        values = [rng.random() * 0.3 for _ in range(n)]
        t = rng.uniform(0, 5)
        while t < duration:
            length = rng.uniform(0.2, 9)
            for i in range(int(t / 0.1), min(n, int((t + length) / 0.1))):
                values[i] = 0.5 + 0.5 * rng.random()
            t += length + rng.choice([0.1, 0.3, 0.5, 1.0, 3.0, 8.0])
        analysis = {"duration": duration, "beats": [0.03 + k * 0.5 for k in range(int(duration / 0.5))],
                    "vocal_activity": {"hop": 0.1, "values": values}}  # fmt: skip
        lines = [
            {"id": f"l{i}", "text": rng.choice(pool), "start": None, "end": None} for i in range(rng.randint(1, 40))
        ]
        for line in lines:
            if rng.random() < 0.15:
                s = rng.uniform(0, duration)
                line.update(start=s, end=s + rng.uniform(0.5, 4), source="manual")
        start = end = None
        if rng.random() < 0.4:
            start = rng.uniform(0, duration * 0.6)
            end = rng.uniform(start + 0.5, duration)
        out, warnings = auto_time(lines, analysis, start=start, end=end, overwrite_timed=rng.random() < 0.3)
        assert [line["text"] for line in out] == [line["text"] for line in lines]
        crowded = any("詰めて" in w for w in warnings)
        for a, b in itertools.pairwise(out):
            if a["source"] == b["source"] == "heuristic":
                assert a["end"] <= b["start"] + 1e-6
        for line in out:
            if line["source"] != "heuristic":
                continue
            assert 0 <= line["start"] <= line["end"] <= duration + 1e-3  # times are rounded to ms
            assert crowded or line["end"] - line["start"] >= MIN_LINE_SEC - 1e-6
            if line["words"]:
                assert (
                    line["start"] - 1e-3 <= line["words"][0]["start"] and line["words"][-1]["end"] <= line["end"] + 1e-3
                )
