"""Optional Whisper alignment of the user's lyrics to the song (ARCHITECTURE.md §6.2).

faster-whisper transcribes the vocal with word timestamps; the recognized text
is only used as a *clock*. It is aligned character by character to the user's
lyrics (never replacing them):

* both sides are normalized for matching: NFKC, katakana → hiragana, case
  folded, punctuation / symbols / whitespace removed;
* a semi-global Needleman–Wunsch alignment (numpy, row-vectorized) pairs the
  two character sequences; repeated choruses stay on their own occurrences.
  Matched characters take the recognized character's time (a word's time is
  split evenly over its characters); a substituted character (e.g. a
  mis-recognized kanji) takes the time of the character it is aligned with;
  missing characters are interpolated / extrapolated from their neighbours at
  the line's own pace;
* a line whose share of exactly matched characters is below
  ``MIN_MATCH_RATIO`` keeps its previous timing and is reported in a warning.
  If that timing collides with the aligned lines around it (or is missing), it
  is re-spread inside the free gap between them.

Aligned lines get ``source="whisper"``, ``confidence`` = matched ratio and
per-character / per-word ``words``. ``whisper_available()`` never touches the
network (``local_files_only``), so it is fast even where huggingface.co is
unreachable; models must be pre-downloaded or ``WHISPER_MODEL`` must point to
a local CTranslate2 model directory.
"""

from __future__ import annotations

import difflib
import importlib.util
import itertools
import logging
import math
import statistics
import subprocess
import threading
import time
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from app.services.errors import AnalysisError, ServiceError, WhisperUnavailable
from app.services.lyrics_parse import LyricLine, WordTiming, copy_line
from app.services.lyrics_timing import MIN_LINE_SEC, fill_words, line_weight, text_units

__all__ = [
    "MIN_MATCH_RATIO",
    "RecognizedWord",
    "Transcriber",
    "align_lines",
    "normalize_for_matching",
    "reset_whisper_cache",
    "whisper_available",
    "whisper_transcriber",
]

logger = logging.getLogger(__name__)

RecognizedWord = tuple[str, float, float]  # (text, start, end) in absolute song seconds

MIN_MATCH_RATIO = 0.4
MIN_MATCHED_CHARS = 2
DEFAULT_CHAR_SEC = 0.25
CLOSE_GAP_SEC = 0.5
UNMATCHED_CONFIDENCE = 0.1
NEGATIVE_CACHE_SEC = 300.0
WHISPER_SAMPLE_RATE = 16_000
_PROMPT_CHARS = 120
_DECODE_TIMEOUT = 300


class Transcriber(Protocol):
    """Speech recognizer returning word timings in absolute song seconds."""

    def __call__(
        self,
        audio_path: str,
        *,
        language: str,
        start: float | None,
        end: float | None,
        initial_prompt: str | None,
    ) -> Sequence[RecognizedWord]: ...


# --------------------------------------------------------------------------- availability & model


_lock = threading.Lock()
_availability: dict[str, tuple[tuple[bool, str | None], float]] = {}
_models: dict[str, Any] = {}


def _configured_model(model_size: str | None) -> str:
    if model_size:
        return model_size
    from app.config import get_settings

    return get_settings().WHISPER_MODEL


def _locate_model(model: str) -> str:
    """Local directory of a CTranslate2 Whisper model; never downloads."""
    path = Path(model).expanduser()
    if path.is_dir():
        if (path / "model.bin").is_file():
            return str(path)
        raise WhisperUnavailable(
            f"WHISPER_MODEL に指定されたディレクトリに Whisper モデル（model.bin）がありません: {path}"
        )
    if model.startswith(("/", ".", "~")):
        raise WhisperUnavailable(f"WHISPER_MODEL に指定されたディレクトリが見つかりません: {path}")
    if importlib.util.find_spec("faster_whisper") is None:
        raise WhisperUnavailable(
            "faster-whisper がインストールされていないため、Whisper による歌詞合わせは利用できません。"
        )
    try:
        from faster_whisper.utils import download_model
    except Exception as exc:  # broken native dependency (ctranslate2, tokenizers …)
        raise WhisperUnavailable(f"faster-whisper を読み込めませんでした（{type(exc).__name__}）。") from exc
    try:
        return str(download_model(model, local_files_only=True))
    except ValueError as exc:
        raise WhisperUnavailable(
            f"Whisper のモデル名「{model}」が不正です（WHISPER_MODEL を確認してください）。"
        ) from exc
    except Exception as exc:  # LocalEntryNotFoundError and other OSErrors
        raise WhisperUnavailable(
            f"Whisper モデル「{model}」がこのサーバーにありません。"
            "事前にモデルをダウンロードするか、WHISPER_MODEL にモデルのディレクトリを指定してください。"
        ) from exc


def whisper_available(model_size: str | None = None) -> tuple[bool, str | None]:
    """(available, reason). Only looks at local files; cached (failures for 5 minutes)."""
    model = _configured_model(model_size)
    now = time.monotonic()
    with _lock:
        cached = _availability.get(model)
        if cached is not None and (cached[0][0] or now - cached[1] < NEGATIVE_CACHE_SEC):
            return cached[0]
    try:
        _locate_model(model)
        result: tuple[bool, str | None] = (True, None)
    except WhisperUnavailable as exc:
        result = (False, str(exc))
    with _lock:
        _availability[model] = (result, now)
    return result


def reset_whisper_cache() -> None:
    """Forget cached availability results and loaded models (e.g. after installing a model)."""
    with _lock:
        _availability.clear()
        _models.clear()


def _load_model(model: str) -> Any:
    with _lock:
        loaded = _models.get(model)
    if loaded is not None:
        return loaded
    path = _locate_model(model)
    try:
        from faster_whisper import WhisperModel

        loaded = WhisperModel(path, device="auto", compute_type="auto", local_files_only=True)
    except Exception as exc:
        raise WhisperUnavailable(f"Whisper モデル「{model}」を読み込めませんでした（{type(exc).__name__}）。") from exc
    with _lock:
        return _models.setdefault(model, loaded)


def _decode_audio(audio_path: str, start: float | None, end: float | None) -> np.ndarray:
    """16 kHz mono float32 samples of ``[start, end]`` decoded with FFmpeg.

    (faster-whisper's own PyAV-based ``decode_audio`` is incompatible with the
    installed PyAV 19.)
    """
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if start:
        cmd += ["-ss", f"{max(0.0, start):.3f}"]
    cmd += ["-i", str(audio_path)]
    if end is not None:
        cmd += ["-t", f"{max(0.0, end - (start or 0.0)):.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(WHISPER_SAMPLE_RATE), "-f", "f32le", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, check=True, timeout=_DECODE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        stderr = getattr(exc, "stderr", b"") or b""
        logger.warning("audio decode failed: %s", stderr.decode("utf-8", "replace")[-500:])
        raise AnalysisError("音声ファイルをデコードできませんでした。") from exc
    return np.frombuffer(proc.stdout, dtype=np.float32)


def whisper_transcriber(model_size: str | None = None) -> Transcriber:
    """The default transcriber: faster-whisper with word timestamps."""
    model = _configured_model(model_size)

    def transcribe(
        audio_path: str,
        *,
        language: str,
        start: float | None,
        end: float | None,
        initial_prompt: str | None,
    ) -> list[RecognizedWord]:
        whisper = _load_model(model)
        audio = _decode_audio(audio_path, start, end)
        offset = max(0.0, start or 0.0)
        segments, _info = whisper.transcribe(
            audio,
            language=language,
            word_timestamps=True,
            initial_prompt=initial_prompt,
            condition_on_previous_text=False,
            vad_filter=False,
            beam_size=5,
        )
        words: list[RecognizedWord] = []
        for segment in segments:
            for word in segment.words or ():
                words.append((str(word.word), float(word.start) + offset, float(word.end) + offset))
        return words

    return transcribe


# --------------------------------------------------------------------------- normalization


def _fold_kana(ch: str) -> str:
    """Katakana → hiragana (ァ..ヶ and the iteration marks ヽヾ)."""
    if "\u30a1" <= ch <= "\u30f6" or ch in "\u30fd\u30fe":
        return chr(ord(ch) - 0x60)
    return ch


def normalize_for_matching(text: str) -> list[tuple[str, int]]:
    """Matching characters of ``text`` with the index of the source character.

    NFKC (per character cluster, so indices stay valid), katakana → hiragana,
    case folded; only letters and digits are kept.
    """
    out: list[tuple[str, int]] = []
    i, n = 0, len(text)
    while i < n:
        j = i + 1
        while j < n and (unicodedata.category(text[j])[0] == "M" or text[j] in "\uff9e\uff9f"):
            j += 1
        for ch in unicodedata.normalize("NFKC", text[i:j]):
            for folded in _fold_kana(ch).casefold():
                if unicodedata.category(folded)[0] in "LN":
                    out.append((folded, i))
        i = j
    return out


# --------------------------------------------------------------------------- alignment

Span = tuple[float, float]


def _recognized_chars(words: Sequence[RecognizedWord]) -> tuple[str, list[Span]]:
    cleaned: list[RecognizedWord] = []
    for text, start, end in words:
        s, e = float(start), float(end)
        if not (math.isfinite(s) and math.isfinite(e)):
            continue
        cleaned.append((str(text), s, max(s, e)))
    cleaned.sort(key=lambda w: w[1])
    chars: list[str] = []
    times: list[Span] = []
    for text, s, e in cleaned:
        normalized = [c for c, _ in normalize_for_matching(text)]
        count = len(normalized)
        for k, c in enumerate(normalized):
            chars.append(c)
            times.append((s + (e - s) * k / count, s + (e - s) * (k + 1) / count))
    return "".join(chars), times


_MATCH_SCORE, _MISMATCH_SCORE, _GAP_SCORE = 2, -1, -1
_MAX_DP_CELLS = 12_000_000  # ≈ 24 MB of int16 scores


def _alignment(lyric: str, recognized: str) -> list[tuple[int, int, bool]]:
    """(lyric index, recognized index, exact) for every aligned character pair.

    Semi-global Needleman–Wunsch: every lyric character is matched, substituted
    or left out, while unmatched recognized text before/after the lyrics is
    free. Unlike greedy longest-block matching this keeps repeated choruses on
    their own occurrences when one of them was mis-recognized. On ties a lyric
    character is left out rather than substituted, so it gets extrapolated
    from its line instead of borrowing the time of unrelated recognized text.
    """
    n, m = len(lyric), len(recognized)
    if n * m > _MAX_DP_CELLS:  # very long input: greedy block matching, bounded cost
        blocks = difflib.SequenceMatcher(None, lyric, recognized, autojunk=False).get_matching_blocks()
        return [(blk.a + k, blk.b + k, True) for blk in blocks for k in range(blk.size)]
    a = np.fromiter((ord(c) for c in lyric), dtype=np.int64, count=n)
    b = np.fromiter((ord(c) for c in recognized), dtype=np.int64, count=m)
    columns = np.arange(m + 1, dtype=np.int64)
    # |score| ≤ 2·n + m, so int16 suffices for any realistic song
    dtype = np.int16 if _MATCH_SCORE * n + m < np.iinfo(np.int16).max else np.int32
    score = np.zeros((n + 1, m + 1), dtype=dtype)  # row 0: free leading recognized text
    for i in range(1, n + 1):
        previous = score[i - 1].astype(np.int64)
        substitution = np.where(b == a[i - 1], _MATCH_SCORE, _MISMATCH_SCORE)
        best = np.empty(m + 1, dtype=np.int64)
        best[0] = previous[0] + _GAP_SCORE
        best[1:] = np.maximum(previous[:-1] + substitution, previous[1:] + _GAP_SCORE)
        # horizontal gaps: score[i][j] = max_k≤j best[k] + GAP·(j−k)
        score[i] = np.maximum.accumulate(best - _GAP_SCORE * columns) + _GAP_SCORE * columns

    pairs: list[tuple[int, int, bool]] = []
    i, j = n, int(np.argmax(score[n]))  # free trailing recognized text
    while i > 0:
        current = int(score[i, j])
        same = j > 0 and lyric[i - 1] == recognized[j - 1]
        if same and current == int(score[i - 1, j - 1]) + _MATCH_SCORE:
            pairs.append((i - 1, j - 1, True))
            i, j = i - 1, j - 1
        elif current == int(score[i - 1, j]) + _GAP_SCORE:
            i -= 1
        elif j > 0 and not same and current == int(score[i - 1, j - 1]) + _MISMATCH_SCORE:
            pairs.append((i - 1, j - 1, False))
            i, j = i - 1, j - 1
        else:
            j -= 1
    pairs.reverse()
    return pairs


def _match(lyric: str, recognized: str, rec_times: Sequence[Span]) -> tuple[list[Span | None], list[bool]]:
    """Per lyric character: time (or None when left out) and whether it matched exactly.

    A substituted character (e.g. a mis-recognized kanji) takes the time of the
    recognized character it is aligned with.
    """
    times: list[Span | None] = [None] * len(lyric)
    exact = [False] * len(lyric)
    for i, j, is_exact in _alignment(lyric, recognized):
        times[i] = rec_times[j]
        exact[i] = is_exact
    return times, exact


def _fill_line_times(times: list[Span | None], first: int, stop: int, lower: float, upper: float) -> list[Span]:
    """Complete the times of characters ``first..stop-1`` (at least one is known)."""
    known = [k for k in range(first, stop) if times[k] is not None]
    durations = [times[k][1] - times[k][0] for k in known]  # type: ignore[index]
    pace = min(max(statistics.median(durations) if durations else DEFAULT_CHAR_SEC, 0.08), 1.0)
    filled: list[Span | None] = list(times[first:stop])

    head = known[0] - first
    if head:
        anchor = filled[head][0]  # type: ignore[index]
        step = min(pace, max(0.0, anchor - lower) / head)
        for q in range(head):
            filled[q] = (anchor - step * (head - q), anchor - step * (head - q - 1))
    tail_from = known[-1] - first
    if tail_from < len(filled) - 1:
        anchor = filled[tail_from][1]  # type: ignore[index]
        count = len(filled) - 1 - tail_from
        step = min(pace, max(0.0, upper - anchor) / count) if math.isfinite(upper) else pace
        for q in range(count):
            filled[tail_from + 1 + q] = (anchor + step * q, anchor + step * (q + 1))
    index = head
    while index < len(filled):
        if filled[index] is not None:
            index += 1
            continue
        run_end = index
        while filled[run_end] is None:
            run_end += 1
        left = filled[index - 1][1]  # type: ignore[index]
        right = max(left, filled[run_end][0])  # type: ignore[index]
        step = (right - left) / (run_end - index)
        for q in range(run_end - index):
            filled[index + q] = (left + step * q, left + step * (q + 1))
        index = run_end
    return [span for span in filled if span is not None]


def _words_from_chars(text: str, char_indices: Sequence[int], char_times: Sequence[Span]) -> list[WordTiming] | None:
    words: list[WordTiming] = []
    previous_end: float | None = None
    for unit in text_units(text):
        spans = [t for idx, t in zip(char_indices, char_times, strict=True) if unit.start <= idx < unit.end]
        if spans:
            start, end = min(s for s, _ in spans), max(e for _, e in spans)
        elif previous_end is not None:
            start = end = previous_end
        else:
            continue
        if previous_end is not None:
            start = max(start, previous_end)
            end = max(end, start)
        words.append(WordTiming(text=text[unit.start : unit.end], start=round(start, 3), end=round(end, 3)))
        previous_end = end
    return words or None


def _line_list(indices: Sequence[int], limit: int = 8) -> str:
    shown = ", ".join(str(k + 1) for k in indices[:limit])
    return f"{shown} 行目" + (f" ほか {len(indices) - limit} 行" if len(indices) > limit else "")


def _initial_prompt(texts: Sequence[str]) -> str | None:
    prompt = ""
    for text in texts:
        if len(prompt) + len(text) + 1 > _PROMPT_CHARS:
            break
        prompt = f"{prompt} {text}" if prompt else text
    return prompt or None


def align_lines(
    audio_path: str | Path,
    lines: Sequence[Mapping[str, Any]],
    language: str = "ja",
    model_size: str | None = None,
    start: float | None = None,
    end: float | None = None,
    transcriber: Transcriber | None = None,
) -> tuple[list[LyricLine], list[str]]:
    """Align lyric lines to the sung vocal (see the module docstring).

    ``transcriber`` replaces faster-whisper (tests, other engines); it must
    return ``[(word_text, start, end), …]`` in absolute song seconds. With a
    ``[start, end]`` window only lines that are untimed or start inside the window
    are aligned. Raises ``WhisperUnavailable`` when the default model can't be
    loaded. Returns new line dicts (input untouched) and Japanese warnings.
    """
    out = [copy_line(line) for line in lines]
    if transcriber is None:
        model = _configured_model(model_size)
        available, reason = whisper_available(model)
        if not available:
            raise WhisperUnavailable(reason or "Whisper は利用できません。")
        transcriber = whisper_transcriber(model)

    lo = max(0.0, start) if start is not None else 0.0
    hi = end if end is not None else math.inf

    def in_window(line: LyricLine) -> bool:
        return line["start"] is None or lo <= line["start"] < hi

    targets = [k for k, line in enumerate(out) if line["text"].strip() and in_window(line)]
    if not targets:
        return out, ["Whisper で合わせる対象の行がありません。"]

    try:
        recognized_words = transcriber(
            str(audio_path),
            language=language,
            start=start,
            end=end,
            initial_prompt=_initial_prompt([out[k]["text"] for k in targets]),
        )
    except ServiceError:
        raise
    except Exception as exc:
        logger.exception("whisper transcription failed")
        raise AnalysisError("音声の文字起こし（Whisper）に失敗しました。") from exc
    recognized_words = [w for w in recognized_words if float(w[2]) >= lo and float(w[1]) <= hi]
    recognized, rec_times = _recognized_chars(recognized_words)
    if not recognized:
        return out, ["音声から歌声を認識できなかったため、タイミングを変更していません。"]

    # Lyric character sequence: (target position, original char index) per char.
    lyric_chars: list[str] = []
    owners: list[tuple[int, int]] = []
    bounds: list[tuple[int, int]] = []
    for position, k in enumerate(targets):
        first = len(lyric_chars)
        for char, index in normalize_for_matching(out[k]["text"]):
            lyric_chars.append(char)
            owners.append((position, index))
        bounds.append((first, len(lyric_chars)))
    char_times, exact = _match("".join(lyric_chars), recognized, rec_times)

    aligned: dict[int, tuple[float, float, list[WordTiming] | None, float]] = {}  # k -> start, vocal end, words, ratio
    unmatched: list[int] = []
    lower = lo
    for position, k in enumerate(targets):
        first, stop = bounds[position]
        total = stop - first
        if total == 0:
            continue  # nothing pronounceable (e.g. "♪"): leave as is
        matched = sum(exact[first:stop])
        if matched < min(MIN_MATCHED_CHARS, total) or matched / total < MIN_MATCH_RATIO:
            unmatched.append(k)
            continue
        upper = next((t[0] for t in char_times[stop:] if t is not None), math.inf)
        spans = _fill_line_times(char_times, first, stop, lower, upper)
        indices = [owners[c][1] for c in range(first, stop)]
        line_start, vocal_end = spans[0][0], spans[-1][1]
        aligned[k] = (line_start, vocal_end, _words_from_chars(out[k]["text"], indices, spans), matched / total)
        lower = vocal_end

    refitted = _refit_unmatched(out, targets, aligned, unmatched, lo, hi)

    # Starts first, so every end below can see the final start of the lines after it.
    starts = {k: round(max(0.0, value[0]), 3) for k, value in aligned.items()}
    for k, line_start in starts.items():
        out[k]["start"] = line_start
    for position, k in enumerate(targets):
        if k not in aligned:
            continue
        _, vocal_end, words, ratio = aligned[k]
        line_start = starts[k]
        # The end stops at the earliest later start (a kept line may be out of
        # order) and is ≥ MIN_LINE_SEC where possible; tiny gaps are closed.
        limit = hi
        for j in targets[position + 1 :]:
            other = out[j]["start"]
            if other is not None and line_start < other < limit:
                limit = other
        line_end = min(max(vocal_end, line_start + MIN_LINE_SEC), limit)
        if math.isfinite(limit) and limit - line_end < CLOSE_GAP_SEC:
            line_end = limit
        line_end = round(max(line_end, line_start), 3)
        line = out[k]
        line["end"] = line_end
        line["words"] = _clamp_words(words, line_end)
        line["source"] = "whisper"
        line["confidence"] = round(ratio, 3)

    warnings = [f"Whisper で {len(aligned)} / {len(targets)} 行のタイミングを合わせました。"]
    if unmatched:
        message = f"{_line_list(unmatched)}は音声認識の結果と十分に一致しなかったため、Whisper による補正をしていません（元の推定タイミングのままです）。"
        if refitted:
            message += f"そのうち前後の行と重なる、または時刻のない {refitted} 行は、前後の行の間に配置し直しました。"
        warnings.append(message)
    return out, warnings


def _clamp_words(words: list[WordTiming] | None, line_end: float) -> list[WordTiming] | None:
    if not words:
        return words
    clamped: list[WordTiming] = []
    for word in words:
        word_end = min(word["end"], line_end) if word["end"] is not None else line_end
        word_start = min(word["start"], word_end) if word["start"] is not None else word_end
        clamped.append(WordTiming(text=word["text"], start=word_start, end=word_end))
    return clamped


def _refit_unmatched(
    out: list[LyricLine],
    targets: Sequence[int],
    aligned: Mapping[int, tuple[float, float, list[WordTiming] | None, float]],
    unmatched: Sequence[int],
    lo: float,
    hi: float,
) -> int:
    """Place unmatched lines whose timing collides with (or is missing between) aligned lines."""
    unmatched_set = set(unmatched)
    refitted = 0
    run: list[int] = []
    gap_start = lo
    for k in [*targets, None]:
        if k is not None and k in unmatched_set:
            run.append(k)
            continue
        if k is not None and k not in aligned:
            continue
        gap_end = aligned[k][0] if k is not None else (hi if math.isfinite(hi) else None)
        if run:
            refitted += _refit_run(out, run, gap_start, gap_end)
            run = []
        if k is not None:
            gap_start = aligned[k][1]
    return refitted


def _refit_run(out: list[LyricLine], run: Sequence[int], gap_start: float, gap_end: float | None) -> int:
    """Keep the run's timing when it fits the gap; else spread it over the gap (when bounded)."""
    lines = [out[k] for k in run]
    spans: list[Span] = []
    for line in lines:
        line_start, line_end = line["start"], line["end"]
        if line_start is None:
            break
        spans.append((line_start, line_end if line_end is not None else line_start))
    else:
        ordered = all(a[0] <= b[0] for a, b in itertools.pairwise(spans))
        if (
            ordered
            and spans[0][0] >= gap_start - 1e-6
            and (gap_end is None or max(e for _, e in spans) <= gap_end + 1e-6)
        ):
            return 0
    if gap_end is None or gap_end - gap_start < 0.5 * len(lines):
        return 0
    weights = [line_weight(line["text"]) for line in lines]
    total = sum(weights)
    cursor = gap_start
    for line, weight in zip(lines, weights, strict=True):
        length = (gap_end - gap_start) * weight / total
        line["start"] = round(cursor, 3)
        line["end"] = round(cursor + length, 3)
        line["words"] = fill_words(line["text"], cursor, cursor + length)
        line["source"] = "heuristic"
        line["confidence"] = UNMATCHED_CONFIDENCE
        cursor += length
    return len(lines)
