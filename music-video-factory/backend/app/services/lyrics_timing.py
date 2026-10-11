"""Heuristic lyric auto-timing from the song analysis (ARCHITECTURE.md §6.2).

``auto_time(lines, analysis, start=None, end=None, overwrite_timed=False)``
places untimed lyric lines on the song timeline without any speech model:

1. Vocal activity (``analysis["vocal_activity"]``, hop 0.1 s; the loudness
   curve is the fallback) is lightly smoothed and thresholded with hysteresis
   relative to the whole song. The resulting runs are cut at pauses of at
   least 0.2 s into *phrase units*; gaps shorter than 0.6 s count as breaths
   inside a vocal region.
2. Each run of consecutive untimed lines (between lines whose timing is kept,
   inside ``[start, end]``) is assigned to the phrase units in its span by a
   small dynamic program: a line covers one or more consecutive units, several
   lines may share one unit (split at the weakest activity near the
   proportional point), and spurious units may be skipped. The cost compares
   each line's voiced duration with its expected duration, which is
   proportional to its text length (spaces and punctuation ignored, kanji ≈ 1.5
   morae, Latin words by syllables), and penalises long gaps inside a line,
   splits and skips. When the DP is not applicable the lines are spread
   proportionally over the vocal regions, and over the whole span (with a
   warning) when there is too little vocal activity.
3. Line starts are snapped to the nearest beat within 0.25 s, then lines are
   made non-overlapping with a minimum duration of 1.0 s. Gaps shorter than
   0.5 s between consecutive lines are closed so subtitles don't flicker.
4. ``words`` are filled per mora character (Japanese) / per word (Latin) by
   splitting the sung part of the line evenly by estimated morae/syllables.

Results are approximations (``source="heuristic"``, ``confidence`` 0.3, or
0.15 for lines spread without vocal cues); the user is expected to review.
The lyric text is never modified.
"""

from __future__ import annotations

import bisect
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from app.services.lyrics_parse import LyricLine, WordTiming, copy_line

__all__ = [
    "BEAT_SNAP_SEC",
    "HEURISTIC_CONFIDENCE",
    "MIN_LINE_SEC",
    "TextUnit",
    "auto_time",
    "fill_words",
    "line_weight",
    "text_units",
]

MIN_LINE_SEC = 1.0
BEAT_SNAP_SEC = 0.25
MERGE_GAP_SEC = 0.6  # gaps shorter than this are breaths inside one vocal region
MIN_PAUSE_SEC = 0.2  # shorter dips are syllable gaps, not phrase boundaries
MIN_UNIT_SEC = 0.3
CLOSE_GAP_SEC = 0.5
HEURISTIC_CONFIDENCE = 0.3
EVEN_CONFIDENCE = 0.15
FALLBACK_LINE_SEC = 4.0  # per line, when the song length is unknown
MIN_VOICED_PER_LINE_SEC = 0.6

# DP costs (dimensionless; a duration off by a factor of e costs 1.0).
_SKIP_BASE = 0.4
_SKIP_PER_SEC = 0.5
_SPLIT_COST = 0.7
_GAP_COST_PER_SEC = 0.4
_MAX_UNITS_PER_LINE = 10
_MAX_LINES_PER_UNIT = 6
_MAX_DP_WORK = 3_000_000


# --------------------------------------------------------------------------- text units

_SPACE, _LATIN, _MORA, _ATTACH_PREV, _ATTACH_NEXT = range(5)
# Small kana extend the preceding mora (きゃ = one mora); small っ is a mora of its own.
_SMALL_KANA = frozenset(
    "ぁぃぅぇぉゃゅょゎァィゥェォャュョヮㇰㇱㇲㇳㇴㇵㇶㇷㇸㇹㇺㇻㇼㇽㇾㇿ"
    "\uff67\uff68\uff69\uff6a\uff6b\uff6c\uff6d\uff6e"  # half-width small kana
    "\uff9e\uff9f"  # half-width (semi-)voiced sound marks
)
_SOKUON = frozenset("っッ\uff6f")
_WORD_JOINERS = frozenset("'’-")


@dataclass(frozen=True)
class TextUnit:
    """A sung unit of a line: ``text[start:end]`` with an estimated length in morae."""

    start: int
    end: int
    weight: float


def _is_kanji(ch: str) -> bool:
    return (
        "\u4e00" <= ch <= "\u9fff"  # CJK unified ideographs
        or "\u3400" <= ch <= "\u4dbf"  # extension A
        or "\uf900" <= ch <= "\ufaff"  # compatibility ideographs
        or "\U00020000" <= ch <= "\U0003134f"  # extensions B-G
        or ch == "々"
    )


def _is_kana(ch: str) -> bool:
    return (
        "\u3040" <= ch <= "\u30ff"  # hiragana, katakana
        or "\u31f0" <= ch <= "\u31ff"  # katakana phonetic extensions
        or "\uff66" <= ch <= "\uff9f"  # half-width katakana
    )


def _char_kind(ch: str) -> int:
    if ch.isspace():
        return _SPACE
    if ch in _SMALL_KANA:
        return _ATTACH_PREV
    category = unicodedata.category(ch)
    if category[0] == "M":
        return _ATTACH_PREV
    if category in ("Ps", "Pi"):
        return _ATTACH_NEXT
    if category[0] in "LN":
        if _is_kana(ch) or unicodedata.east_asian_width(ch) in ("W", "F"):
            return _MORA
        return _LATIN
    return _ATTACH_PREV  # punctuation, symbols


def _mora_weight(ch: str) -> float:
    if _is_kanji(ch):
        return 1.5  # most kanji are read with two morae
    if ch in _SOKUON:
        return 0.5
    return 1.0


def _latin_weight(word: str) -> float:
    plain = "".join(c for c in unicodedata.normalize("NFKD", word) if not unicodedata.combining(c)).lower()
    if plain.isdigit():
        return float(len(plain))
    syllables = len(re.findall(r"[aeiouy]+", plain))
    if syllables > 1 and plain.endswith("e") and not plain.endswith(("le", "ee")):
        syllables -= 1  # silent final e
    return float(max(1, syllables))


def text_units(text: str) -> list[TextUnit]:
    """Split a lyric line into sung units.

    One unit per Japanese character (kana/kanji/full-width), small kana joined to
    the preceding character, one unit per Latin word (``don't`` stays whole).
    Punctuation joins the preceding unit, opening brackets the following one;
    spaces are never part of a unit's edges. Units cover the text in order, so
    every unit's text is a substring of ``text``.
    """
    n = len(text)
    kinds = [_char_kind(ch) for ch in text]
    for i in range(1, n - 1):
        if text[i] in _WORD_JOINERS and kinds[i - 1] == _LATIN and kinds[i + 1] == _LATIN:
            kinds[i] = _LATIN

    spans: list[list[Any]] = []  # [start, end, weight]
    prefix: int | None = None  # start of punctuation waiting for the next unit
    last_solid = -1  # index of the last non-space character
    i = 0
    while i < n:
        kind = kinds[i]
        if kind == _SPACE:
            i += 1
            continue
        if kind == _ATTACH_NEXT or (kind == _ATTACH_PREV and (not spans or prefix is not None)):
            if prefix is None:
                prefix = i
            last_solid = i
            i += 1
            continue
        if kind == _ATTACH_PREV:
            spans[-1][1] = i + 1
            last_solid = i
            i += 1
            continue
        stop = i + 1
        if kind == _LATIN:
            while stop < n and kinds[stop] == _LATIN:
                stop += 1
            weight = _latin_weight(text[i:stop])
        else:
            weight = _mora_weight(text[i])
        spans.append([prefix if prefix is not None else i, stop, weight])
        prefix = None
        last_solid = stop - 1
        i = stop
    if prefix is not None:
        if spans:
            spans[-1][1] = last_solid + 1
        else:
            spans.append([prefix, last_solid + 1, 0.0])
    return [TextUnit(start, end, weight) for start, end, weight in spans]


def line_weight(text: str) -> float:
    """Expected sung length of a line in morae (≥ 1)."""
    return max(1.0, sum(unit.weight for unit in text_units(text)))


def fill_words(text: str, start: float, end: float) -> list[WordTiming] | None:
    """Per-unit word timings spread over ``[start, end]`` in proportion to morae."""
    units = text_units(text)
    if not units or end <= start:
        return None
    weights = [max(unit.weight, 0.25) for unit in units]
    total = sum(weights)
    words: list[WordTiming] = []
    acc = 0.0
    for unit, weight in zip(units, weights, strict=True):
        word_start = start + (end - start) * acc / total
        acc += weight
        word_end = start + (end - start) * acc / total
        words.append(WordTiming(text=text[unit.start : unit.end], start=round(word_start, 3), end=round(word_end, 3)))
    return words


# --------------------------------------------------------------------------- vocal activity

Span = tuple[float, float]


@dataclass
class _Activity:
    hop: float
    values: np.ndarray  # smoothed, 0..1
    units: list[Span]  # phrase units over the whole song
    source: str  # "vocal" | "loudness"

    def units_in(self, lo: float, hi: float) -> list[Span]:
        """Units clipped to [lo, hi]. A unit mostly before ``lo`` is the tail of an earlier
        phrase (before the window or the previous kept line) and is dropped."""
        out: list[Span] = []
        for a, b in self.units:
            if b <= lo or a >= hi:
                continue
            if a < lo and b - lo < 0.5 * (b - a):
                continue
            a, b = max(a, lo), min(b, hi)
            if b - a >= MIN_UNIT_SEC / 2:
                out.append((a, b))
        return out

    def weakest_point(self, t0: float, t1: float) -> float:
        """Time of the lowest activity in ``[t0, t1]`` (the middle when no frame fits)."""
        i0 = max(0, math.ceil(t0 / self.hop))
        i1 = min(len(self.values) - 1, math.floor(t1 / self.hop) - 1)
        if i1 < i0:
            return (t0 + t1) / 2
        index = i0 + int(np.argmin(self.values[i0 : i1 + 1]))
        return min(max((index + 0.5) * self.hop, t0), t1)


def _raw_activity(analysis: Mapping[str, Any]) -> tuple[float, np.ndarray, str] | None:
    vocal = analysis.get("vocal_activity")
    if isinstance(vocal, Mapping):
        hop = _positive(vocal.get("hop"))
        values = vocal.get("values")
        if hop and isinstance(values, Sequence) and len(values) > 0:
            arr = np.nan_to_num(np.asarray(values, dtype=np.float64), nan=0.0)
            return hop, np.clip(arr, 0.0, 1.0), "vocal"
    loudness = analysis.get("loudness")
    if isinstance(loudness, Mapping):
        hop = _positive(loudness.get("hop"))
        values = loudness.get("rms_db")
        if hop and isinstance(values, Sequence) and len(values) > 0:
            db = np.nan_to_num(np.asarray(values, dtype=np.float64), nan=-100.0, neginf=-100.0)
            ref = float(np.percentile(db, 95))
            return hop, np.clip((db - (ref - 30.0)) / 30.0, 0.0, 1.0), "loudness"
    return None


def _build_activity(analysis: Mapping[str, Any]) -> _Activity | None:
    raw = _raw_activity(analysis)
    if raw is None:
        return None
    hop, values, source = raw
    if hop <= 0.15 and len(values) >= 3:
        padded = np.pad(values, 1, mode="edge")
        values = np.convolve(padded, np.ones(3) / 3.0, mode="valid")
    low, high = (float(x) for x in np.percentile(values, [20, 95]))
    if high < 0.05 or high - low < 0.1:
        return _Activity(hop, values, [], source)
    on = low + 0.5 * (high - low)
    off = low + 0.4 * (high - low)

    runs: list[list[int]] = []  # [first frame, end frame) where activity ≥ off and peaks ≥ on
    above = values >= off
    i, n = 0, len(values)
    while i < n:
        if not above[i]:
            i += 1
            continue
        j = i
        while j < n and above[j]:
            j += 1
        if float(values[i:j].max()) >= on:
            runs.append([i, j])
        i = j
    merged: list[list[int]] = []
    for run in runs:
        if merged and (run[0] - merged[-1][1]) * hop < MIN_PAUSE_SEC - 1e-9:
            merged[-1][1] = run[1]
        else:
            merged.append(run)
    units = [(a * hop, b * hop) for a, b in merged if (b - a) * hop >= MIN_UNIT_SEC - 1e-9]
    return _Activity(hop, values, units, source)


def _coarsen(units: list[Span], limit: int) -> list[Span]:
    """Merge units across their smallest gaps until at most ``limit`` remain."""
    if len(units) <= max(1, limit):
        return units
    gaps = sorted(units[k + 1][0] - units[k][1] for k in range(len(units) - 1))
    threshold = gaps[len(units) - max(1, limit) - 1]
    merged = [list(units[0])]
    for a, b in units[1:]:
        if a - merged[-1][1] <= threshold:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


# --------------------------------------------------------------------------- placement


@dataclass(frozen=True)
class _Cover:
    """Line covers units ``first..last`` (inclusive)."""

    first: int
    last: int


@dataclass(frozen=True)
class _Share:
    """``count`` lines share one unit; this line is number ``position`` (0-based)."""

    unit: int
    position: int
    count: int


def _assign(weights: Sequence[float], units: Sequence[Span]) -> list[_Cover | _Share] | None:
    """Optimal ordered assignment of lines to phrase units (None when not applicable)."""
    n, m = len(weights), len(units)
    if m == 0 or n > m * _MAX_LINES_PER_UNIT or n * m * (_MAX_UNITS_PER_LINE + _MAX_LINES_PER_UNIT) > _MAX_DP_WORK:
        return None
    durs = [b - a for a, b in units]
    pre_d = [0.0, *np.cumsum(durs).tolist()]
    excess = [max(0.0, units[k + 1][0] - units[k][1] - MERGE_GAP_SEC) for k in range(m - 1)]
    pre_x = [0.0, *np.cumsum(excess).tolist()] if excess else [0.0]
    pre_w = [0.0, *np.cumsum(weights).tolist()]
    total_w = pre_w[-1]
    rate = pre_d[-1] / total_w  # seconds per mora

    result: list[_Cover | _Share] | None = None
    for _ in range(3):  # re-estimate the singing rate from the units actually used
        inf = math.inf
        cost = [[inf] * (m + 1) for _ in range(n + 1)]
        back: list[list[tuple[int, int, str] | None]] = [[None] * (m + 1) for _ in range(n + 1)]
        cost[0][0] = 0.0
        for i in range(n + 1):
            row = cost[i]
            for j in range(m + 1):
                base = row[j]
                if base == inf:
                    continue
                if j < m:
                    skip = base + _SKIP_BASE + _SKIP_PER_SEC * durs[j]
                    if skip < row[j + 1]:
                        row[j + 1] = skip
                        back[i][j + 1] = (i, j, "skip")
                if i >= n or j >= m:
                    continue
                expected = weights[i] * rate
                next_row = cost[i + 1]
                for k in range(j + 1, min(m, j + _MAX_UNITS_PER_LINE) + 1):
                    voiced = pre_d[k] - pre_d[j]
                    gaps = pre_x[k - 1] - pre_x[j]
                    c = base + math.log(voiced / expected) ** 2 + _GAP_COST_PER_SEC * gaps
                    if c < next_row[k]:
                        next_row[k] = c
                        back[i + 1][k] = (i, j, "cover")
                for q in range(2, min(_MAX_LINES_PER_UNIT, n - i) + 1):
                    ratio = durs[j] / ((pre_w[i + q] - pre_w[i]) * rate)
                    c = base + q * math.log(ratio) ** 2 + _SPLIT_COST * (q - 1)
                    if c < cost[i + q][j + 1]:
                        cost[i + q][j + 1] = c
                        back[i + q][j + 1] = (i, j, "share")
        if cost[n][m] == inf:
            return None
        plan: list[_Cover | _Share] = []
        used = 0.0
        i, j = n, m
        while i > 0 or j > 0:
            step = back[i][j]
            assert step is not None
            pi, pj, kind = step
            if kind == "cover":
                plan.append(_Cover(pj, j - 1))
                used += pre_d[j] - pre_d[pj]
            elif kind == "share":
                count = i - pi
                plan.extend(_Share(pj, position, count) for position in reversed(range(count)))
                used += durs[pj]
            i, j = pi, pj
        plan.reverse()
        new_rate = used / total_w
        if result == plan or new_rate <= 0 or abs(new_rate - rate) <= 0.03 * rate:
            return plan
        result, rate = plan, new_rate
    return result


def _plan_spans(
    plan: Sequence[_Cover | _Share], weights: Sequence[float], units: Sequence[Span], activity: _Activity
) -> list[Span]:
    """(start, vocal end) per line for a DP plan."""
    spans: list[Span] = []
    index = 0
    while index < len(plan):
        item = plan[index]
        if isinstance(item, _Cover):
            spans.append((units[item.first][0], units[item.last][1]))
            index += 1
            continue
        a, b = units[item.unit]
        shares = list(weights[index : index + item.count])
        total = sum(shares)
        bounds = [a]
        acc = 0.0
        for share in shares[:-1]:
            acc += share
            point = a + (b - a) * acc / total
            radius = min(0.8, 0.3 * (b - a) * min(shares) / total)
            point = activity.weakest_point(point - radius, point + radius)
            point = min(max(point, bounds[-1] + 0.1), b - 0.1)
            bounds.append(point)
        bounds.append(b)
        spans.extend((bounds[k], bounds[k + 1]) for k in range(item.count))
        index += item.count
    return spans


def _proportional(weights: Sequence[float], segments: Sequence[Span]) -> list[Span]:
    """Spread lines over the concatenated segments in proportion to their weights."""
    total = sum(b - a for a, b in segments)
    total_w = sum(weights)

    def at(position: float, *, starting: bool) -> float:
        acc = 0.0
        for a, b in segments:
            length = b - a
            if position < acc + length or (not starting and position <= acc + length):
                return a + (position - acc)
            acc += length
        return segments[-1][1]

    spans: list[Span] = []
    acc_w = 0.0
    for weight in weights:
        begin = at(total * acc_w / total_w, starting=True)
        acc_w += weight
        spans.append((begin, max(begin, at(total * acc_w / total_w, starting=False))))
    return spans


def _snap_to_beat(t: float, beats: Sequence[float]) -> float:
    if not beats:
        return t
    k = bisect.bisect_left(beats, t)
    candidates = beats[max(0, k - 1) : k + 1]
    best = min(candidates, key=lambda b: abs(b - t))
    return best if abs(best - t) <= BEAT_SNAP_SEC else t


def _fit(spans: Sequence[Span], lo: float, hi: float) -> tuple[list[tuple[float, float, float]], bool]:
    """(start, end, vocal end) per line: inside [lo, hi], ordered, no overlaps, ≥ MIN_LINE_SEC.

    The second value is True when the span is too short for the minimum
    duration and the lines were packed evenly instead.
    """
    n = len(spans)
    if hi - lo < n * MIN_LINE_SEC:
        step = (hi - lo) / n
        return [(lo + k * step, lo + (k + 1) * step, lo + (k + 1) * step) for k in range(n)], True
    starts = [min(max(s, lo), hi) for s, _ in spans]
    for k in range(1, n):
        starts[k] = max(starts[k], starts[k - 1] + MIN_LINE_SEC)
    starts[-1] = min(starts[-1], hi - MIN_LINE_SEC)
    for k in range(n - 2, -1, -1):
        starts[k] = min(starts[k], starts[k + 1] - MIN_LINE_SEC)
    fitted: list[tuple[float, float, float]] = []
    for k, (start, (_, vocal_end)) in enumerate(zip(starts, spans, strict=True)):
        limit = starts[k + 1] if k + 1 < n else hi
        end = min(max(vocal_end, start + MIN_LINE_SEC), limit)
        if limit - end < CLOSE_GAP_SEC:
            end = limit
        fitted.append((start, end, min(max(vocal_end, start), end)))
    return fitted, False


# --------------------------------------------------------------------------- auto_time


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _song_duration(analysis: Mapping[str, Any]) -> float | None:
    duration = _positive(analysis.get("duration"))
    if duration:
        return duration
    vocal = analysis.get("vocal_activity")
    if isinstance(vocal, Mapping) and isinstance(vocal.get("values"), Sequence):
        hop = _positive(vocal.get("hop"))
        if hop and vocal["values"]:
            return hop * len(vocal["values"])
    return None


def _beats(analysis: Mapping[str, Any]) -> list[float]:
    beats = analysis.get("beats")
    if not isinstance(beats, Sequence):
        return []
    out = []
    for beat in beats:
        try:
            value = float(beat)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            out.append(value)
    return sorted(out)


def _line_range(indices: Sequence[int]) -> str:
    first, last = indices[0] + 1, indices[-1] + 1
    return f"{first} 行目" if first == last else f"{first}〜{last} 行目"


def auto_time(
    lines: Sequence[Mapping[str, Any]],
    analysis: Mapping[str, Any] | None,
    start: float | None = None,
    end: float | None = None,
    overwrite_timed: bool = False,
) -> tuple[list[LyricLine], list[str]]:
    """Heuristically time lyric lines (see the module docstring).

    Lines that already have a start time are kept and act as anchors unless
    ``overwrite_timed``; with a ``[start, end]`` window, ``overwrite_timed``
    only re-times lines whose start lies inside the window. Returns new line
    dicts (the input is not modified) and Japanese warnings.
    """
    out = [copy_line(line) for line in lines]
    warnings: list[str] = []
    analysis = analysis or {}
    duration = _song_duration(analysis)

    lo_window = max(0.0, float(start)) if start is not None else 0.0
    hi_window: float | None = float(end) if end is not None else duration
    if hi_window is not None and duration is not None:
        hi_window = min(hi_window, duration)

    def inside_window(line: LyricLine) -> bool:
        s = line["start"]
        return s is not None and s >= lo_window and (hi_window is None or s < hi_window)

    todo = [
        k
        for k, line in enumerate(out)
        if line["text"].strip() and (line["start"] is None or (overwrite_timed and inside_window(line)))
    ]
    todo_set = set(todo)
    kept = sum(1 for k, line in enumerate(out) if line["start"] is not None and k not in todo_set)
    if not todo:
        warnings.append("自動タイミングの対象となる行がありません（すべての行に時刻が設定済みです）。")
        return out, warnings
    if hi_window is None:
        hi_window = lo_window + FALLBACK_LINE_SEC * len(todo)
        warnings.append(
            "楽曲の長さが分からないため、1行あたり約4秒で仮に配置しました。楽曲解析の完了後にやり直してください。"
        )
    if hi_window - lo_window < MIN_LINE_SEC:
        warnings.append("指定された区間が短すぎるため、自動タイミングを実行できませんでした。")
        return out, warnings

    activity = _build_activity(analysis)
    beats = _beats(analysis)

    # Runs of consecutive lines to place, bounded by the kept lines around them.
    groups: list[tuple[list[int], float, float]] = []
    current: list[int] = []
    lower = lo_window
    for k, line in enumerate(out):
        if k in todo_set:
            current.append(k)
            continue
        if line["start"] is None or not line["text"].strip():
            continue
        if current:
            groups.append((current, lower, min(hi_window, line["start"])))
            current = []
        anchor_end = (
            line["end"] if line["end"] is not None and line["end"] > line["start"] else line["start"] + MIN_LINE_SEC
        )
        lower = max(lo_window, anchor_end)
    if current:
        groups.append((current, lower, hi_window))

    evenly_spread = 0
    for indices, lo, hi in groups:
        if hi - lo < 0.5:
            warnings.append(
                f"{_line_range(indices)}を配置できる時間がないため、時刻を設定できませんでした（前後の行の時刻や指定区間を確認してください）。"
            )
            continue
        weights = [line_weight(out[k]["text"]) for k in indices]
        units = activity.units_in(lo, hi) if activity else []
        voiced = sum(b - a for a, b in units)
        confidence = HEURISTIC_CONFIDENCE
        if units and voiced >= MIN_VOICED_PER_LINE_SEC * len(indices):
            units = _coarsen(units, 3 * len(indices) + 20)
            plan = _assign(weights, units)
            assert activity is not None
            spans = _plan_spans(plan, weights, units, activity) if plan else _proportional(weights, units)
        else:
            spans = _proportional(weights, [(lo, hi)])
            confidence = EVEN_CONFIDENCE
            evenly_spread += len(indices)
        snapped = [(_snap_to_beat(s, beats), e) for s, e in spans]
        fitted, crowded = _fit(snapped, lo, hi)
        if crowded:
            warnings.append(
                f"{lo:.1f}〜{hi:.1f} 秒の区間に {len(indices)} 行を1行1秒以上で配置できないため、等間隔に詰めて配置しました。"
            )
        for k, (line_start, line_end, vocal_end) in zip(indices, fitted, strict=True):
            line = out[k]
            word_end = min(line_end, max(vocal_end, line_start + 0.5))
            line["start"] = round(line_start, 3)
            line["end"] = round(line_end, 3)
            line["words"] = fill_words(line["text"], line_start, word_end)
            line["source"] = "heuristic"
            line["confidence"] = confidence

    if evenly_spread:
        reason = "ボーカルらしい区間を十分に検出できなかった" if activity else "楽曲解析にボーカル区間の情報がない"
        warnings.append(
            f"{reason}ため、{evenly_spread} 行を文字数に応じて区間内に均等に配置しました。"
            "プレビューで確認し、必要に応じて手動で調整してください。"
        )
    if kept:
        warnings.append(f"時刻が設定済みの {kept} 行は変更していません。")
    return out, warnings
