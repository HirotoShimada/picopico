"""Tests for ``app.services.audio_analysis`` (ARCHITECTURE.md §6.1).

The demo song (``scripts/make_demo_assets.py``) is 60 s at 120 BPM:
intro 0-8 (quiet), verse 8-24, chorus 24-40 (loud), bridge 40-48 (quiet),
chorus 48-60; vocal phrases from 8 s on. Edge cases are synthesized into
``tmp_path`` with numpy/soundfile/FFmpeg.
"""

from __future__ import annotations

import itertools
import json
import math
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import soundfile as sf

from app.services import audio_analysis
from app.services.audio_analysis import analyze_audio
from app.services.errors import AnalysisError

TOP_LEVEL_KEYS = {
    "version", "duration", "sample_rate", "channels", "codec", "bit_rate",
    "bpm", "bpm_confidence", "beats", "downbeats", "loudness", "waveform",
    "sections", "chorus_candidates", "highlights", "quiet_parts",
    "suggested_segments", "vocal_activity", "mood",
}  # fmt: skip
SECTION_LABELS = {"intro", "verse", "chorus", "bridge", "outro", "break"}
JSON_TYPES = (dict, list, str, int, float, bool, type(None))

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not installed")


# --- helpers -------------------------------------------------------------------------------------


def overlap(a: dict[str, Any] | tuple[float, float], b: tuple[float, float]) -> float:
    start, end = (a["start"], a["end"]) if isinstance(a, dict) else a
    return max(0.0, min(end, b[1]) - max(start, b[0]))


def mean_between(values: list[float], hop: float, start: float, end: float) -> float:
    return float(np.mean(values[round(start / hop) : round(end / hop)]))


def assert_plain_json(obj: Any, path: str = "$") -> None:
    """Only JSON-native Python types (numpy scalars subclass float/int, so compare exact types)."""
    assert type(obj) in JSON_TYPES, f"{path}: {type(obj).__name__}"
    if isinstance(obj, float):
        assert math.isfinite(obj), path
    if isinstance(obj, dict):
        for key, value in obj.items():
            assert type(key) is str
            assert_plain_json(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            assert_plain_json(value, f"{path}[{i}]")


def assert_valid_analysis(a: dict[str, Any]) -> None:
    """Shape and internal consistency of an AudioAnalysis dict."""
    assert set(a) == TOP_LEVEL_KEYS
    assert_plain_json(a)
    json.dumps(a)

    duration = a["duration"]
    assert a["version"] == 1
    assert duration > 0
    assert a["bpm"] > 0
    assert 0.0 <= a["bpm_confidence"] <= 1.0

    beats, downbeats = a["beats"], a["downbeats"]
    assert all(0.0 <= t < duration for t in beats)
    assert all(b > a_ for a_, b in itertools.pairwise(beats)), "beats must be strictly increasing"
    assert set(downbeats) <= set(beats)

    loud = a["loudness"]
    assert loud["hop"] == 0.5
    assert len(loud["rms_db"]) == math.ceil(duration / 0.5 - 1e-9)
    assert all(-100.0 <= v <= 0.5 for v in loud["rms_db"])

    wave = a["waveform"]
    assert wave["points"] == 1200 and len(wave["peaks"]) == 1200
    assert all(0.0 <= p <= 1.0 for p in wave["peaks"])

    vocal = a["vocal_activity"]
    assert vocal["hop"] == 0.1
    assert len(vocal["values"]) == math.ceil(duration / 0.1 - 1e-9)
    assert all(0.0 <= v <= 1.0 for v in vocal["values"])

    sections = a["sections"]
    assert sections and sections[0]["start"] == 0.0 and sections[-1]["end"] == duration
    for prev, cur in itertools.pairwise(sections):
        assert prev["end"] == cur["start"], "sections must be contiguous"
    for s in sections:
        assert s["end"] > s["start"]
        assert s["label"] in SECTION_LABELS
        assert 0.0 <= s["energy"] <= 1.0
        assert isinstance(s["is_chorus_candidate"], bool)
        assert set(s) == {"start", "end", "label", "energy", "is_chorus_candidate"}

    chorus = a["chorus_candidates"]
    for c in chorus:
        assert set(c) == {"start", "end", "score"}
        assert 0.0 <= c["start"] < c["end"] <= duration and 0.0 <= c["score"] <= 1.0
    rounded = [round(c["score"], 2) for c in chorus]
    assert rounded == sorted(rounded, reverse=True), "best chorus first"

    for h in a["highlights"]:
        assert set(h) == {"start", "end", "energy"}
        assert 0.0 <= h["start"] < h["end"] <= duration and 0.0 <= h["energy"] <= 1.0
    for q in a["quiet_parts"]:
        assert set(q) == {"start", "end"}
        assert 0.0 <= q["start"] < q["end"] <= duration

    segments = a["suggested_segments"]
    assert [s["duration"] for s in segments] == [d for d in (15, 30, 60) if d <= duration + 0.05]
    for seg in segments:
        assert set(seg) == {"start", "end", "duration", "score", "reason"}
        assert seg["start"] >= 0.0 and seg["end"] <= duration
        assert seg["end"] - seg["start"] == pytest.approx(seg["duration"], abs=0.06)
        assert seg["start"] == 0.0 or seg["start"] in downbeats or (not downbeats and seg["start"] in beats)
        assert 0.0 <= seg["score"] <= 1.0
        assert isinstance(seg["reason"], str) and any(ord(ch) > 0x3000 for ch in seg["reason"]), "Japanese reason"

    mood = a["mood"]
    assert set(mood) == {"energy", "tempo_class", "brightness", "mode_guess", "tags"}
    assert 0.0 <= mood["energy"] <= 1.0 and 0.0 <= mood["brightness"] <= 1.0
    assert mood["tempo_class"] in {"slow", "mid", "fast"}
    assert mood["mode_guess"] in {"major", "minor"}
    assert all(isinstance(t, str) for t in mood["tags"])


def write_wav(path: Path, samples: np.ndarray, sr: int) -> Path:
    sf.write(str(path), samples, sr, subtype="PCM_16")
    return path


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args], check=True, timeout=120)


def click_track(bpm: float, seconds: float = 30.0, sr: int = 44100) -> np.ndarray:
    """1 kHz clicks on every beat, accented on the bar."""
    out = np.zeros(int(seconds * sr))
    length = int(0.03 * sr)
    t = np.arange(length) / sr
    click = np.sin(2 * np.pi * 1000.0 * t) * np.exp(-t / 0.01)
    for k in range(int(seconds * bpm / 60.0) + 1):
        i = int(k * 60.0 / bpm * sr)
        n = min(length, out.size - i)
        if n > 0:
            out[i : i + n] += (1.0 if k % 4 == 0 else 0.6) * click[:n]
    return 0.8 * out


# --- demo song (wav and mp3) -----------------------------------------------------------------


@pytest.fixture(scope="module", params=["demo_song.wav", "demo_song.mp3"])
def demo(request: pytest.FixtureRequest, demo_dir: Path) -> dict[str, Any]:
    return analyze_audio(demo_dir / request.param)


def test_demo_shape_and_json(demo: dict[str, Any]) -> None:
    assert_valid_analysis(demo)
    assert demo["duration"] == pytest.approx(60.0, abs=0.1)
    assert json.loads(json.dumps(demo)) == demo


def test_demo_metadata(demo_dir: Path) -> None:
    wav = analyze_audio(str(demo_dir / "demo_song.wav"))
    assert (wav["sample_rate"], wav["channels"], wav["codec"], wav["bit_rate"]) == (44100, 2, "pcm_s16le", 1411200)
    mp3 = analyze_audio(demo_dir / "demo_song.mp3")
    assert (mp3["sample_rate"], mp3["channels"], mp3["codec"]) == (44100, 2, "mp3")
    assert mp3["bit_rate"] and mp3["bit_rate"] > 64000


def test_demo_tempo(demo: dict[str, Any]) -> None:
    assert 115.0 <= demo["bpm"] <= 125.0
    assert demo["bpm_confidence"] >= 0.5
    # ~2 beats per second over the whole song, including the quiet intro.
    assert 110 <= len(demo["beats"]) <= 130
    assert demo["beats"][0] < 1.0


def test_demo_downbeats_on_bar_lines(demo: dict[str, Any]) -> None:
    downbeats = demo["downbeats"]
    assert 25 <= len(downbeats) <= 35
    # Bars are 2 s long and the song starts on a bar.
    assert all(abs(t / 2.0 - round(t / 2.0)) * 2.0 < 0.1 for t in downbeats)


def test_demo_chorus_candidates(demo: dict[str, Any]) -> None:
    chorus = demo["chorus_candidates"]
    assert chorus
    assert any(overlap(c, (24, 40)) >= 6 or overlap(c, (48, 60)) >= 6 for c in chorus)
    best = chorus[0]
    assert overlap(best, (24, 40)) >= 8 or overlap(best, (48, 60)) >= 8
    assert not any(overlap(c, (0, 8)) > 2 or overlap(c, (40, 48)) > 2 for c in chorus), "quiet parts are no chorus"


def test_demo_sections(demo: dict[str, Any]) -> None:
    sections = demo["sections"]
    assert sections[0]["label"] == "intro" and sections[0]["end"] == pytest.approx(8.0, abs=0.6)
    chorus_sections = [s for s in sections if s["label"] == "chorus"]
    assert any(overlap(s, (24, 40)) >= 12 for s in chorus_sections)
    middle = [s for s in sections if overlap(s, (40, 48)) >= 6]
    assert middle and middle[0]["label"] in {"bridge", "break"}
    by_label = {s["label"]: s["energy"] for s in sections}
    assert by_label["chorus"] > by_label["intro"] + 0.2


def test_demo_quiet_parts_and_highlights(demo: dict[str, Any]) -> None:
    quiet = demo["quiet_parts"]
    assert any(overlap(q, (0, 8)) >= 4 for q in quiet)
    assert any(overlap(q, (40, 48)) >= 4 for q in quiet)
    assert not any(overlap(q, (24, 40)) > 1 for q in quiet)
    highlights = demo["highlights"]
    assert highlights
    assert all(overlap(h, (24, 40)) + overlap(h, (48, 60)) >= 0.8 * (h["end"] - h["start"]) for h in highlights)


def test_demo_suggested_segments_start_at_chorus(demo: dict[str, Any]) -> None:
    segments = {s["duration"]: s for s in demo["suggested_segments"]}
    assert set(segments) == {15, 30, 60}
    seg15 = segments[15]
    # Starts at, or at most one bar (2 s) before, a chorus candidate.
    assert any(-0.1 <= c["start"] - seg15["start"] <= 2.1 for c in demo["chorus_candidates"]), seg15
    assert overlap(seg15, (24, 40)) >= 12 or overlap(seg15, (48, 60)) >= 10
    assert "サビ" in seg15["reason"]
    assert segments[60]["start"] == 0.0 and segments[60]["end"] == pytest.approx(60.0, abs=0.06)


def test_demo_vocal_activity(demo: dict[str, Any]) -> None:
    values = demo["vocal_activity"]["values"]
    intro, verse = mean_between(values, 0.1, 0, 8), mean_between(values, 0.1, 8, 24)
    chorus, bridge = mean_between(values, 0.1, 24, 40), mean_between(values, 0.1, 40, 48)
    assert verse > intro + 0.2
    assert chorus > bridge + 0.2


def test_demo_mood(demo: dict[str, Any]) -> None:
    mood = demo["mood"]
    assert mood["tempo_class"] == "mid"
    assert "dynamic" in mood["tags"]


# --- edge cases ------------------------------------------------------------------------------------


def test_six_second_file(tmp_path: Path, demo_dir: Path) -> None:
    y, sr = sf.read(str(demo_dir / "demo_song.wav"))
    path = write_wav(tmp_path / "six.wav", y[24 * sr : 30 * sr], sr)
    a = analyze_audio(path)
    assert_valid_analysis(a)
    assert a["duration"] == pytest.approx(6.0, abs=0.01)
    assert a["suggested_segments"] == []
    assert len(a["sections"]) == 1
    # Too short for librosa's 8 s tempo window alone; the tempo check recovers 120 BPM.
    assert 115.0 <= a["bpm"] <= 125.0


def test_pure_silence(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "silence.wav", np.zeros((20 * 44100, 2)), 44100)
    a = analyze_audio(path)
    assert_valid_analysis(a)
    assert a["bpm"] == 120.0 and a["bpm_confidence"] == 0.0
    assert a["beats"] == [] and a["downbeats"] == []
    assert a["quiet_parts"] == [{"start": 0.0, "end": 20.0}]
    assert a["highlights"] == [] and a["chorus_candidates"] == []
    assert set(a["vocal_activity"]["values"]) == {0.0}
    assert set(a["waveform"]["peaks"]) == {0.0}
    assert [s["label"] for s in a["sections"]] == ["break"]
    assert [(s["start"], s["duration"]) for s in a["suggested_segments"]] == [(0.0, 15)]
    assert a["mood"]["tags"] == ["silent"]


@needs_ffmpeg
def test_mono_48k_file(tmp_path: Path, demo_dir: Path) -> None:
    path = tmp_path / "mono48k.wav"
    ffmpeg("-i", str(demo_dir / "demo_song.wav"), "-ac", "1", "-ar", "48000", str(path))
    a = analyze_audio(path)
    assert_valid_analysis(a)
    assert (a["sample_rate"], a["channels"]) == (48000, 1)
    assert 115.0 <= a["bpm"] <= 125.0
    assert any(overlap(q, (0, 8)) >= 4 for q in a["quiet_parts"])


@needs_ffmpeg
def test_m4a_decodes_through_ffmpeg(tmp_path: Path, demo_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "song.m4a"
    ffmpeg("-i", str(demo_dir / "demo_song.wav"), "-c:a", "aac", "-b:a", "128k", str(path))
    calls: list[Path] = []
    original = audio_analysis._decode_with_ffmpeg

    def spy(p: Path) -> np.ndarray:
        calls.append(p)
        return original(p)

    monkeypatch.setattr(audio_analysis, "_decode_with_ffmpeg", spy)
    a = analyze_audio(path)
    assert calls == [path], "libsndfile cannot read AAC; the FFmpeg fallback must be used"
    assert_valid_analysis(a)
    assert a["codec"] == "aac" and a["sample_rate"] == 44100 and a["channels"] == 2
    assert a["duration"] == pytest.approx(60.0, abs=0.1)
    assert 115.0 <= a["bpm"] <= 125.0
    assert any(overlap(c, (24, 40)) >= 6 or overlap(c, (48, 60)) >= 6 for c in a["chorus_candidates"])


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("random.mp3", np.random.default_rng(1).bytes(50_000)),
        ("broken.wav", b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 64),
        ("empty.m4a", b""),
        ("text.flac", "これは音声ではありません".encode()),
    ],
    # Explicit ids: the test id ends up in PYTEST_CURRENT_TEST, and 50 kB of
    # bytes there would overflow the environment of the ffmpeg subprocess.
    ids=["random-bytes-mp3", "truncated-wav-header", "empty-m4a", "text-as-flac"],
)
def test_corrupt_file_raises_analysis_error(tmp_path: Path, name: str, content: bytes) -> None:
    path = tmp_path / name
    path.write_bytes(content)
    with pytest.raises(AnalysisError):
        analyze_audio(path)


def test_missing_file_raises_analysis_error(tmp_path: Path) -> None:
    with pytest.raises(AnalysisError, match="見つかりません"):
        analyze_audio(tmp_path / "missing.wav")


@needs_ffmpeg
def test_video_without_audio_raises_analysis_error(demo_dir: Path) -> None:
    with pytest.raises(AnalysisError, match="音声トラック"):
        analyze_audio(demo_dir / "clip_01.mp4")


def test_too_short_file_raises_analysis_error(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "blip.wav", 0.1 * np.ones(22050 // 2), 22050)
    with pytest.raises(AnalysisError, match="短すぎる"):
        analyze_audio(path)


@pytest.mark.parametrize("bpm", [90.0, 100.0, 140.0])
def test_click_track_tempo(tmp_path: Path, bpm: float) -> None:
    a = analyze_audio(write_wav(tmp_path / f"click{bpm:.0f}.wav", click_track(bpm), 44100))
    assert_valid_analysis(a)
    # Hop-quantised tracker tempi (e.g. 117.45 for 120) are refined from the inter-beat intervals.
    assert a["bpm"] == pytest.approx(bpm, abs=1.0)
    assert a["bpm_confidence"] >= 0.6


def test_white_noise_has_low_tempo_confidence(tmp_path: Path) -> None:
    noise = 0.3 * np.random.default_rng(0).standard_normal(20 * 44100)
    a = analyze_audio(write_wav(tmp_path / "noise.wav", noise, 44100))
    assert_valid_analysis(a)
    assert a["bpm_confidence"] < 0.3


@pytest.mark.slow
def test_four_minute_song_analyzes_quickly(tmp_path: Path, demo_dir: Path) -> None:
    y, sr = sf.read(str(demo_dir / "demo_song.wav"))
    path = write_wav(tmp_path / "four_minutes.wav", np.concatenate([y] * 4), sr)
    analyze_audio(demo_dir / "demo_song.wav")  # warm-up: numba JIT / caches
    started = time.perf_counter()
    a = analyze_audio(path)
    elapsed = time.perf_counter() - started
    print(f"4-minute analysis: {elapsed:.2f} s")
    assert elapsed < 30.0
    assert_valid_analysis(a)
    assert a["duration"] == pytest.approx(240.0, abs=0.1)
    assert 115.0 <= a["bpm"] <= 125.0
    assert len([s for s in a["sections"] if s["label"] == "chorus"]) >= 4


# --- helpers in isolation ----------------------------------------------------------------------------


def test_regions_bridge_small_gaps_and_drop_short_runs() -> None:
    mask = np.array([1, 1, 1, 1, 0, 1, 1, 0, 0, 0, 0, 1, 1], dtype=bool)
    assert audio_analysis._regions(mask, 0.5, min_len=2.0, max_gap=0.5) == [(0.0, 3.5)]
    assert audio_analysis._regions(mask, 0.5, min_len=0.5, max_gap=0.5) == [(0.0, 3.5), (5.5, 6.5)]


def test_steady_period_undoes_frame_quantisation() -> None:
    # 120 BPM tracked at 512/22050 s frames alternates 21/22-frame intervals.
    frames = np.round(np.arange(60) * 0.5 / audio_analysis.FRAME_SEC).astype(np.int64)
    assert 60.0 / audio_analysis._steady_period(frames) == pytest.approx(120.0, abs=0.2)


def test_plain_converts_numpy_types() -> None:
    value = audio_analysis._plain(
        {"a": np.float32(0.5), "b": np.int64(3), "c": np.array([1.0, np.nan]), "d": np.bool_(True), "_x": 1}
    )
    assert value == {"a": 0.5, "b": 3, "c": [1.0, 0.0], "d": True}
    assert_plain_json(value)
