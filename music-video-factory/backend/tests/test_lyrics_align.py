"""Tests for ``app.services.lyrics_align`` (ARCHITECTURE.md §6.2).

The character matching is exercised with an injected fake transcriber (no model
is available here: huggingface.co is unreachable), including recognition
errors: substituted and missing kanji, katakana for hiragana, punctuation,
a wholly mis-recognized line and noise in the instrumental break.
"""

from __future__ import annotations

import copy
import itertools
import shutil
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from app.services import lyrics_align
from app.services.errors import AnalysisError, WhisperUnavailable
from app.services.lyrics_align import (
    MIN_MATCH_RATIO,
    align_lines,
    normalize_for_matching,
    reset_whisper_cache,
    whisper_available,
    whisper_transcriber,
)
from app.services.lyrics_parse import parse_lyrics

DEMO_TEXTS = [
    "夜明け前の街を ひとりで歩いた",
    "消えそうな光を 両手で包んで",
    "名前も知らない 風に吹かれて",
    "それでも僕らは 前を向いていた",
    "叫べ 今この瞬間を",
    "忘れないように 胸に刻んで",
    "走れ 遠く遥か先へ",
    "君と見た空を 信じているから",
    "叫べ 今この瞬間を",
    "忘れないように 胸に刻んで",
]
TRUE_STARTS = [8.0, 12.0, 16.0, 20.0, 24.0, 28.0, 32.0, 36.0, 48.0, 52.0]
PHRASE_SEC = 3.6


@pytest.fixture(autouse=True)
def _fresh_whisper_cache() -> Iterator[None]:
    reset_whisper_cache()
    yield
    reset_whisper_cache()


def recognized_words(phrases: Sequence[tuple[float, float, str]]) -> list[tuple[str, float, float]]:
    """Whisper-like words: each phrase split at spaces, time spread evenly over its characters."""
    words: list[tuple[str, float, float]] = []
    for start, end, text in phrases:
        chunks = text.split()
        total = sum(len(c) for c in chunks)
        position = 0
        for chunk in chunks:
            word_start = start + (end - start) * position / total
            position += len(chunk)
            words.append((" " + chunk, word_start, start + (end - start) * position / total))
    return words


def fake_transcriber(words: list[tuple[str, float, float]], calls: list[dict[str, Any]] | None = None) -> Any:
    def transcribe(audio_path: str, **kwargs: Any) -> list[tuple[str, float, float]]:
        if calls is not None:
            calls.append({"audio_path": audio_path, **kwargs})
        return words

    return transcribe


def demo_lines() -> list[dict[str, Any]]:
    lines, _ = parse_lyrics("\n".join(DEMO_TEXTS), "txt")
    return [dict(line) for line in lines]


# --------------------------------------------------------------------------- normalization


def test_normalize_for_matching() -> None:
    text = "カタカナ、ＡＢＣ\u3000xyz！ ﾊﾟﾝ「が」"
    pairs = normalize_for_matching(text)
    assert "".join(c for c, _ in pairs) == "かたかなabcxyzぱんが"
    assert [i for _, i in pairs] == [0, 1, 2, 3, 5, 6, 7, 9, 10, 11, 14, 16, 18]
    # decomposed dakuten (NFD input) still matches the composed form
    assert "".join(c for c, _ in normalize_for_matching("か\u3099ー")) == "がー"


# --------------------------------------------------------------------------- matching with a fake transcriber


def test_alignment_with_recognition_errors() -> None:
    heard = [
        "世明け前の街を ヒトリで歩いた",  # substituted first kanji, katakana
        "消えそうな光を両手で包んで",
        "名前も知らない、風に吹かれて",  # extra punctuation
        "それでも僕らは 前を向いた",  # missing characters
        "叫べ 今この春感を",  # two substituted kanji
        "忘れないように 胸にきざんで",  # kana instead of kanji
        "ぜんぜん ちがう ことば です",  # nothing in common
        "君と見た空を 信じているから",
        "叫べ 今この瞬間を",
        "忘れないように 胸に刻んで",
    ]
    phrases = [(s, s + PHRASE_SEC, text) for s, text in zip(TRUE_STARTS, heard, strict=True)]
    words = recognized_words([*phrases[:8], (42.0, 43.0, "えー"), *phrases[8:]])  # noise in the bridge
    lines = demo_lines()
    # previous (heuristic) timing of the line that will not match
    lines[6].update(start=32.1, end=35.4, source="heuristic", confidence=0.3)
    before = copy.deepcopy(lines)
    calls: list[dict[str, Any]] = []

    out, warnings = align_lines("song.wav", lines, transcriber=fake_transcriber(words, calls))

    assert lines == before  # input untouched
    assert [line["text"] for line in out] == DEMO_TEXTS  # user text never replaced
    assert calls and calls[0]["language"] == "ja" and calls[0]["start"] is None and calls[0]["end"] is None
    assert calls[0]["initial_prompt"].startswith(DEMO_TEXTS[0])

    for index, (line, truth) in enumerate(zip(out, TRUE_STARTS, strict=True)):
        if index == 6:
            continue
        assert line["source"] == "whisper", line
        assert line["start"] == pytest.approx(truth, abs=0.05), line
        assert line["end"] >= line["start"] + 1.0
        assert line["end"] <= (TRUE_STARTS[index + 1] if index + 1 < len(TRUE_STARTS) else 60.0)
        words_ = line["words"]
        assert words_ and words_[0]["start"] == pytest.approx(line["start"], abs=1e-3)
        assert all(a["end"] <= b["start"] + 1e-6 for a, b in itertools.pairwise(words_))
        assert "".join(w["text"] for w in words_) == line["text"].replace(" ", "")
    assert out[1]["confidence"] == 1.0 and out[7]["confidence"] == 1.0
    assert MIN_MATCH_RATIO <= out[4]["confidence"] < 1.0  # 瞬間 → 春感
    assert out[0]["confidence"] == pytest.approx(13 / 14, abs=1e-3)  # 夜 → 世
    # the substituted kanji takes the time of what was heard in its place
    assert out[4]["words"][5]["text"] == "瞬" and out[4]["words"][5]["start"] > out[4]["words"][4]["start"]

    # unmatched line keeps its previous timing and is reported
    assert out[6] == before[6]
    assert any("7 行目" in w and "一致しなかった" in w for w in warnings)
    assert warnings[0] == "Whisper で 9 / 10 行のタイミングを合わせました。"


def test_missing_first_kanji_is_extrapolated() -> None:
    # 夜 (sung 8.00-8.26) was not recognized at all.
    words = recognized_words(
        [
            (8.0 + PHRASE_SEC / 14, 8.0 + PHRASE_SEC, "明け前の街を ひとりで歩いた"),
            (12.0, 15.6, "消えそうな光を 両手で包んで"),
        ]
    )
    out, _ = align_lines("song.wav", demo_lines()[:2], transcriber=fake_transcriber(words))
    assert out[0]["start"] == pytest.approx(8.0, abs=0.1)
    assert out[0]["words"][0]["text"] == "夜"
    assert out[1]["start"] == pytest.approx(12.0, abs=0.01)


def test_misrecognized_first_chorus_does_not_steal_the_second() -> None:
    # Greedy longest-block matching would map the lyrics' first chorus onto the
    # perfectly recognized second chorus and shift everything in between.
    texts = [
        "叫べ 今この瞬間を",
        "忘れないように 胸に刻んで",
        "走れ 遠く遥か先へ",
        "叫べ 今この瞬間を",
        "忘れないように 胸に刻んで",
    ]
    lines, _ = parse_lyrics("\n".join(texts), "txt")
    heard = [
        (24.0, 27.6, "叫べ 今この春感を"),
        (28.0, 31.6, "忘れないように 胸にきざんで"),
        (32.0, 35.6, "ぜんぜん ちがう ことば"),
        (48.0, 51.6, texts[3]),
        (52.0, 55.6, texts[4]),
    ]
    out, _ = align_lines("song.wav", lines, transcriber=fake_transcriber(recognized_words(heard)))
    aligned = [out[k] for k in (0, 1, 3, 4)]
    assert [line["start"] for line in aligned] == pytest.approx([24.0, 28.0, 48.0, 52.0], abs=0.01)
    assert all(line["source"] == "whisper" for line in aligned)
    assert out[2]["source"] == "heuristic" and out[1]["end"] <= out[2]["start"] < out[2]["end"] <= 48.0


def test_missing_first_kanji_after_intro_noise_is_not_pulled_to_the_noise() -> None:
    char = PHRASE_SEC / 14
    heard = [(2.0, 3.0, "えー"), (8.0 + char, 8.0 + PHRASE_SEC, "明け前の街を ひとりで歩いた")]
    out, _ = align_lines("song.wav", demo_lines()[:1], transcriber=fake_transcriber(recognized_words(heard)))
    assert out[0]["start"] == pytest.approx(8.0, abs=0.1)


def test_repeated_chorus_lines_map_in_order() -> None:
    texts = ["叫べ 今この瞬間を", "走れ 遠く遥か先へ", "叫べ 今この瞬間を"]
    lines, _ = parse_lyrics("\n".join(texts), "txt")
    words = recognized_words([(24.0, 27.6, texts[0]), (32.0, 35.6, texts[1]), (48.0, 51.6, texts[2])])
    out, _ = align_lines("song.wav", lines, transcriber=fake_transcriber(words))
    assert [line["start"] for line in out] == pytest.approx([24.0, 32.0, 48.0], abs=0.01)


def test_unmatched_untimed_line_is_placed_between_neighbours() -> None:
    lines = demo_lines()[:3]
    words = recognized_words(
        [(8.0, 11.6, DEMO_TEXTS[0]), (12.0, 15.6, "まったく べつの ことば"), (16.0, 19.6, DEMO_TEXTS[2])]
    )
    out, warnings = align_lines("song.wav", lines, transcriber=fake_transcriber(words))
    assert out[0]["source"] == out[2]["source"] == "whisper"
    middle = out[1]
    assert middle["source"] == "heuristic" and middle["confidence"] == pytest.approx(0.1)
    assert out[0]["end"] <= middle["start"] < middle["end"] <= out[2]["start"]
    assert middle["words"]
    assert any("配置し直しました" in w for w in warnings)


def test_window_limits_the_lines_that_are_aligned() -> None:
    lines, _ = parse_lyrics(
        "[00:08.00]夜明け前の街を ひとりで歩いた\n[00:24.00]叫べ 今この瞬間を\n[00:48.00]叫べ 今この瞬間を\n"
    )
    words = recognized_words([(24.3, 27.6, "叫べ 今この瞬間を")])
    calls: list[dict[str, Any]] = []
    out, warnings = align_lines("song.wav", lines, start=20.0, end=40.0, transcriber=fake_transcriber(words, calls))
    assert calls[0]["start"] == 20.0 and calls[0]["end"] == 40.0
    assert out[0] == lines[0] and out[2] == lines[2]  # outside the window: untouched, no warning
    assert out[1]["source"] == "whisper" and out[1]["start"] == pytest.approx(24.3, abs=0.01)
    assert warnings == ["Whisper で 1 / 1 行のタイミングを合わせました。"]


def test_nothing_recognized_leaves_lines_unchanged() -> None:
    lines = demo_lines()
    out, warnings = align_lines("song.wav", lines, transcriber=fake_transcriber([]))
    assert out == lines
    assert warnings and "認識できなかった" in warnings[0]


def test_transcriber_failure_becomes_analysis_error() -> None:
    def broken(audio_path: str, **kwargs: Any) -> list[tuple[str, float, float]]:
        raise RuntimeError("boom")

    with pytest.raises(AnalysisError):
        align_lines("song.wav", demo_lines(), transcriber=broken)


# --------------------------------------------------------------------------- availability


def test_whisper_available_is_fast_and_reports_reason() -> None:
    started = time.monotonic()
    available, reason = whisper_available()
    elapsed = time.monotonic() - started
    assert elapsed < 5.0
    # No model can be downloaded here (huggingface.co is blocked) and none is cached.
    assert available is False
    assert isinstance(reason, str) and "Whisper" in reason
    started = time.monotonic()
    assert whisper_available() == (available, reason)
    assert time.monotonic() - started < 0.05  # cached


def test_whisper_available_with_local_directories(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    available, reason = whisper_available(str(missing))
    assert available is False and reason is not None and str(missing) in reason
    available, reason = whisper_available(str(tmp_path))
    assert available is False and reason is not None and "model.bin" in reason
    (tmp_path / "model.bin").write_bytes(b"")
    reset_whisper_cache()
    assert whisper_available(str(tmp_path)) == (True, None)


def test_align_without_model_raises_whisper_unavailable_quickly() -> None:
    started = time.monotonic()
    with pytest.raises(WhisperUnavailable) as excinfo:
        align_lines("song.wav", demo_lines())
    assert time.monotonic() - started < 5.0
    assert excinfo.value.status_code == 503
    assert str(excinfo.value)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not installed")
def test_default_transcriber_decodes_window_and_offsets_word_times(
    demo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: dict[str, Any] = {}

    class FakeModel:
        def transcribe(self, audio: np.ndarray, **kwargs: Any) -> tuple[Any, Any]:
            received["audio"] = audio
            received.update(kwargs)
            word = SimpleNamespace(word=" 叫べ", start=0.5, end=1.0)
            return iter([SimpleNamespace(words=[word])]), SimpleNamespace(language="ja")

    monkeypatch.setattr(lyrics_align, "_load_model", lambda model: FakeModel())
    transcribe = whisper_transcriber("small")
    words = transcribe(str(demo_dir / "demo_song.mp3"), language="ja", start=24.0, end=28.0, initial_prompt="叫べ")
    assert words == [(" 叫べ", 24.5, 25.0)]
    audio = received["audio"]
    assert audio.dtype == np.float32 and abs(len(audio) - 4 * 16_000) <= 1600
    assert float(np.abs(audio).max()) > 0.05  # real audio, not silence
    assert received["word_timestamps"] is True and received["language"] == "ja" and received["initial_prompt"] == "叫べ"


def test_default_transcriber_rejects_undecodable_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lyrics_align, "_load_model", lambda model: SimpleNamespace())
    bogus = tmp_path / "not_audio.wav"
    bogus.write_bytes(b"this is not audio")
    with pytest.raises(AnalysisError):
        whisper_transcriber("small")(str(bogus), language="ja", start=None, end=None, initial_prompt=None)


def test_random_recognition_keeps_invariants() -> None:
    """Seeded fuzz: noisy / missing recognition, kept lines and windows never break the contract."""
    import random

    rng = random.Random(7)
    pool = [
        "夜明け前の街を ひとりで歩いた",
        "叫べ 今この瞬間を",
        "Hello world, don't stop",
        "♪",
        "ラララ ラララ",
        "123 GO!",
        "…",
    ]
    alphabet = "あいうえおかきくけこ夜明前街光"
    for _ in range(150):
        lines = [
            {"id": f"l{i}", "text": rng.choice(pool), "start": None, "end": None} for i in range(rng.randint(1, 25))
        ]
        for line in lines:
            if rng.random() < 0.3:
                s = rng.uniform(0, 200)
                line.update(start=s, end=s + rng.uniform(0.5, 5), source="heuristic")
        words: list[tuple[str, float, float]] = []
        t = rng.uniform(0, 5)
        for line in lines:
            if rng.random() < 0.15:
                continue
            heard = "".join(
                c if rng.random() > 0.25 else rng.choice(alphabet) for c in line["text"] if rng.random() > 0.1
            )
            for chunk in heard.split():
                d = rng.uniform(0.1, 1.2)
                words.append((chunk, t, t + d))
                t += d + rng.choice([0, 0, 0.2, 1.5])
            t += rng.uniform(0.2, 6)
        start = end = None
        if rng.random() < 0.3:
            start = rng.uniform(0, t / 2)
            end = rng.uniform(start + 1, t + 5)
        out, _ = align_lines("song.wav", lines, start=start, end=end, transcriber=fake_transcriber(words))
        assert [line["text"] for line in out] == [line["text"] for line in lines]
        aligned = [line for line in out if line["source"] == "whisper"]
        for a, b in itertools.pairwise(aligned):
            assert a["start"] <= b["start"] and a["end"] <= b["start"] + 1e-6
        for line in aligned:
            assert line["start"] <= line["end"]
            for word in line["words"] or []:
                assert line["start"] - 1e-3 <= word["start"] <= word["end"] <= line["end"] + 1e-3
