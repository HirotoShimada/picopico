"""Batch variation planner (ARCHITECTURE.md §6.4).

``plan_batch`` turns one storyboard into a shared *clip pool* plus one
``VideoPlan`` per output video:

* **Clip pool** — generation specs for storyboard scenes, allocated in
  proportion to how much of the planned videos falls into each scene's song
  section (extra takes get a new seed and a small framing hint), up to the
  pool size of the ``reuse`` policy. Ready clips with the same ``cache_key``
  are reused for free; ``max_new_clips`` caps paid generations. In
  ``library`` mode the pool is the uploaded clips and nothing is generated.
* **Shots** — cut points land on beats (1–2 bars per shot, clamped to
  1.5–6 s, pattern varies per video and with section energy) and sum exactly
  to the video duration (millisecond grid).
* **Fit to the pool** — a generated video never shows the same clip twice
  while the pool can cover its shots: when its beat cuts outnumber the pool,
  it is re-cut on beats into fewer, longer shots (≤ 8 s, played at ≥ 0.75x,
  reserving the renderer's crossfade frames). Only when even that cannot
  fill the video does a clip come back (never adjacent unless the pool has
  one clip, the source window that overlaps its earlier ones least, a
  punch-in framing); such repeats are counted and warned about. Fitted
  videos that share a start get the same, most even cuts and differ in clip
  order (the next most even split once every order is taken).
* **Variation** — videos differ by start time, clip order, in-points,
  Ken Burns zoom/pan, speed, colour grade and transition (horizontal flips
  only with ``settings["allow_mirror"]``: mirroring turns any text in the
  footage into mirror writing); clip usage is balanced so every newly
  generated clip is used, and footage that comes back gets a different
  camera move (or a flip, when allowed). Generated clips already carry the
  model's own camera move, so their synthetic move is only a slight centred
  zoom (≤ ``GENERATED_MAX_ZOOM_DELTA``, no pan); footage back from an earlier
  video gets a *static* crop (``GENERATED_CROP_ZOOM``), a clip shown again in
  the same video a *static* punch-in (alternating with the crop when it comes
  back more than once); library uploads keep the full moves.

Everything is deterministic for a given ``seed``; the budget is not an input
(the estimate, not the plan, decides whether a batch is affordable).
"""

from __future__ import annotations

import bisect
import functools
import hashlib
import heapq
import itertools
import math
import random
import statistics
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.services.errors import ServiceError
from app.services.presets import COLOR_GRADES, LYRIC_STYLES, get_preset
from app.services.prompting import sanitize_prompt

__all__ = [
    "ASPECT_RATIO",
    "FIT_MIN_SPEED",
    "FIT_SLOW_WEIGHT",
    "FRAME_SLACK_SEC",
    "GENERATED_CROP_ZOOM",
    "GENERATED_MAX_ZOOM_DELTA",
    "MAX_FIT_SHOT_SEC",
    "MAX_POOL",
    "MAX_SHOT_SEC",
    "MIN_SHOT_SEC",
    "NEAR_NATURAL_SPEED",
    "PUNCH_IN_ZOOM",
    "REPEAT_GAP_SEC",
    "SCENE_ADJACENT_COST",
    "SCENE_REPEAT_COST",
    "SRC_IN_BUCKET_SEC",
    "VARIETY_WARNING_THRESHOLD",
    "XFADE_PAD_SEC",
    "fit_clip_floor",
    "make_cache_key",
    "plan_batch",
    "pool_target_size",
]

ASPECT_RATIO = "9:16"
MAX_POOL = 60
MIN_SHOT_SEC = 1.5
MAX_SHOT_SEC = 6.0
SRC_IN_BUCKET_SEC = 1.0  # in-points closer than this count as the same footage for variety_score
VARIETY_WARNING_THRESHOLD = 0.35
# First sentence of every variety warning; the rest names the cause and what to change.
VARIETY_WARNING_JA = "素材の再利用率が高いため、動画間の映像バリエーションが限定的です。"
FEW_SCENES = 3  # storyboards with at most this many scenes repeat the same scenes as alternate takes
MIN_NATURAL_SPEED = 0.6  # slower than this looks like obvious slow motion
GENERATED_EDGE_MARGIN = 0.1  # generated clips often have weak first/last frames
UPLOAD_EDGE_MARGIN = 0.05
# Fitting a video's cuts to a small pool (fewer, longer shots instead of repeated clips):
FIT_MIN_SPEED = 0.75  # slowest playback a fitted shot may get
NEAR_NATURAL_SPEED = 0.9  # pool floor: enough clips to fit a video at about this speed
MAX_FIT_SHOT_SEC = 8.0  # longest fitted shot (beat shots stay ≤ MAX_SHOT_SEC)
XFADE_PAD_SEC = 0.3  # must equal render.CROSSFADE_SEC (not imported: the planner does not depend on render)
FRAME_SLACK_SEC = 0.04  # rounding slack of the renderer's frame grid
REPEAT_GAP_SEC = 0.5  # source windows of one clip closer than this count as the same footage
SCENE_REPEAT_COST = 3.0  # per earlier shot of the same storyboard scene in the video
SCENE_ADJACENT_COST = 3.0  # the previous shot shows the same scene
FIT_SLOW_WEIGHT = 10.0  # fit DP: weight of slow motion against uneven shot lengths
_ORDER_TAKEN_COST = 20.0  # retry of an identical edit: per earlier video whose clip order a pick would follow
_MAX_REFITS = 4  # next most even splits tried for a fitted video whose every clip order is taken
# Ken Burns: library uploads get a zoom of 0.03–0.12 and pans; a generated clip already has the
# model's own camera move, and a synthetic glide on top of it reads as floaty, so it only gets a
# centred zoom of at most this much over the shot (no pan).
GENERATED_MAX_ZOOM_DELTA = 0.02
_LIBRARY_MAX_ZOOM_DELTA = 0.12
PUNCH_IN_ZOOM = (1.12, 1.19)  # a clip shown again in the same video is framed this much tighter
# A generated clip's second visible framing besides the full frame and the punch-in: a static crop
# between the two (footage back from an earlier video, and every other repeat of a clip that has to
# come back more than once in a video, so two uses in a row never share a framing).
GENERATED_CROP_ZOOM = 1.08

_MIN_MS = int(MIN_SHOT_SEC * 1000)
_MAX_MS = int(MAX_SHOT_SEC * 1000)
_MAX_FIT_MS = int(MAX_FIT_SHOT_SEC * 1000)
_BAR_PATTERNS: tuple[tuple[int, ...], ...] = ((1,), (2,), (1, 2), (2, 1), (1, 1, 2), (2, 1, 1), (2, 2, 1), (1, 2, 2))
_VARIANT_HINTS: tuple[str, ...] = (
    "Alternate take with tighter framing.",
    "Alternate take with a wider view of the surroundings.",
    "Alternate take capturing a different moment of the same action.",
    "Alternate take from a slightly different camera position.",
)
_REUSE_DIVISOR = {"max_variety": 1, "balanced": 3, "economy": 6}
_SOFT_TRANSITION_PRESETS = frozenset({"dreamy", "emotional", "nostalgic", "nature"})
_PANS = ("left", "right", "up", "down")
# 'varied' starts: a video should open on singing, not on a quiet intro/bridge.
_QUIET_SECTIONS = frozenset({"intro", "bridge", "break", "outro"})
_VARIED_MIN_COVERAGE = 0.5  # share of the video with lyrics on screen (or vocals)
_VARIED_OPENING_SEC = 3.0  # the first seconds must not be mostly intro/bridge
_SUGGESTED_DURATION_TOLERANCE = 0.25  # suggested segments within ±25 % of the video length


# --- helpers ---------------------------------------------------------------------------------


def _stable_int(*parts: object) -> int:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _floor3(value: float) -> float:
    return math.floor(value * 1000 + 1e-6) / 1000


def make_cache_key(
    provider: str,
    model: str | None,
    prompt: str,
    negative: str,
    duration: float,
    aspect: str,
    seed: int | None,
) -> str:
    """``sha256(provider|model|prompt|negative|duration|aspect|seed)`` as hex (§6.4).

    The duration is formatted with three decimals so ``5`` and ``5.0`` give the same key.
    """
    raw = "|".join(
        [provider, model or "", prompt, negative, f"{float(duration):.3f}", aspect, "" if seed is None else str(seed)]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def pool_target_size(reuse: str, shots_total: int, shots_per_video: int, n_scenes: int, fit_clips: int = 0) -> int:
    """Clip pool size K for a reuse policy (before ``max_new_clips`` and cache reuse).

    At least one clip per scene (or ``fit_clips``, see :func:`fit_clip_floor`) up to the
    shots of one video, so an uncapped video never has to repeat a clip.
    """
    divisor = _REUSE_DIVISOR.get(reuse, _REUSE_DIVISOR["balanced"])
    target = math.ceil(shots_total / divisor)
    target = max(target, min(shots_per_video, max(1, n_scenes, fit_clips)))
    return max(1, min(target, MAX_POOL, shots_total))


def _shot_pads(n: int, crossfade: bool) -> list[float]:
    """Extra footage (s) each of ``n`` shots needs for the renderer's crossfades (0 for cuts).

    The renderer plays a shot half a fade longer at each side that blends into a neighbour:
    an inner shot a whole fade, the first and last shot half of one; plus frame-rounding slack.
    """
    if not crossfade or n <= 1:
        return [0.0] * n
    inner = XFADE_PAD_SEC + FRAME_SLACK_SEC
    edge = XFADE_PAD_SEC / 2 + 2 * FRAME_SLACK_SEC
    return [edge] + [inner] * (n - 2) + [edge]


def fit_clip_floor(duration_ms: int, clip_duration: float) -> int:
    """Clips a generated video of ``duration_ms`` needs to be fitted at about NEAR_NATURAL_SPEED
    without repeats: ``ceil(D / L)`` with ``L`` the longest padded shot one clip covers."""
    usable = clip_duration - 2 * GENERATED_EDGE_MARGIN
    shot_ms = int((usable / NEAR_NATURAL_SPEED - (XFADE_PAD_SEC + FRAME_SLACK_SEC)) * 1000)
    shot_ms = max(_MIN_MS, min(_MAX_FIT_MS, shot_ms))
    return math.ceil(duration_ms / shot_ms)


def _default_model(provider: str) -> str:
    """Provider default model id (resolved through the registry when available)."""
    try:
        from app.services.providers.registry import get_provider  # type: ignore[import-not-found]

        return str(get_provider(provider).default_model())
    except Exception:  # registry is optional here; the pipeline should pass a resolved model
        return "default"


# --- song timing ---------------------------------------------------------------------------------


@dataclass
class _Section:
    start: float
    end: float
    label: str
    energy: float


@dataclass
class _Song:
    duration: float
    beats: list[float]
    downbeats: list[float]
    bar: float
    beat: float
    sections: list[_Section]
    lyrics: list[tuple[float, float, str]]  # (start, end, line id), timed lines only
    vocal_hop: float = 0.0  # analysis.vocal_activity (empty when missing)
    vocal: list[float] = field(default_factory=list)

    def section_at(self, t: float) -> _Section | None:
        for sec in self.sections:
            if sec.start <= t < sec.end:
                return sec
        return self.sections[-1] if self.sections and t >= self.sections[-1].end else None

    def energy_class(self, t: float) -> str:
        sec = self.section_at(t)
        if sec is None:
            return "mid"
        if sec.label == "chorus" or sec.energy >= 0.7:
            return "high"
        if sec.label in ("intro", "outro", "bridge", "break") or sec.energy < 0.4:
            return "low"
        return "mid"

    def lyric_ids(self, t0: float, t1: float) -> set[str]:
        return {line_id for start, end, line_id in self.lyrics if start < t1 and end > t0}

    def sung_coverage(self, t0: float, t1: float) -> float | None:
        """Share of ``[t0, t1]`` that is sung: lyric lines on screen and vocal activity.

        The smaller of the two measures that are available (an LRC line is often
        held through an instrumental break, so lyrics alone overstate it); None
        when neither is known.
        """
        if t1 <= t0:
            return None
        measures: list[float] = []
        if self.lyrics:
            covered, cursor = 0.0, t0
            for start, end, _ in sorted(self.lyrics):
                start, end = max(start, cursor), min(end, t1)
                if end > start:
                    covered += end - start
                    cursor = end
            measures.append(covered / (t1 - t0))
        if self.vocal_hop > 0 and len(self.vocal) >= 3:
            ordered = sorted(self.vocal)
            low, high = ordered[len(ordered) // 5], ordered[round(0.95 * (len(ordered) - 1))]
            i0, i1 = max(0, int(t0 / self.vocal_hop)), min(len(self.vocal), math.ceil(t1 / self.vocal_hop))
            window = self.vocal[i0:i1]
            if high - low >= 0.1 and window:
                threshold = low + 0.45 * (high - low)
                measures.append(sum(v >= threshold for v in window) / len(window))
        return min(measures) if measures else None

    def section_overlap(self, t0: float, t1: float, labels: frozenset[str]) -> float:
        return sum(max(0.0, min(sec.end, t1) - max(sec.start, t0)) for sec in self.sections if sec.label in labels)

    def mean_energy(self, t0: float, t1: float) -> float:
        weighted = [(min(sec.end, t1) - max(sec.start, t0), sec.energy) for sec in self.sections]
        weighted = [(w, e) for w, e in weighted if w > 0]
        total = sum(w for w, _ in weighted)
        return sum(w * e for w, e in weighted) / total if total > 0 else 0.5

    def snap_to_downbeat(self, t: float, tolerance: float = 0.35) -> float:
        if not self.downbeats:
            return t
        index = bisect.bisect_left(self.downbeats, t)
        nearby = [self.downbeats[i] for i in (index - 1, index) if 0 <= i < len(self.downbeats)]
        best = min(nearby, key=lambda d: abs(d - t))
        return best if abs(best - t) <= tolerance else t


def _build_song(analysis: Mapping[str, Any], lyric_lines: Sequence[Mapping[str, Any]]) -> _Song:
    raw_beats = sorted({b for b in (_num(x) for x in analysis.get("beats") or []) if b is not None and b >= 0})
    duration = _num(analysis.get("duration")) or (raw_beats[-1] if raw_beats else None)
    if not duration or duration <= 0:
        raise ServiceError(
            "楽曲解析の結果（曲の長さ）がないため、動画を計画できません", status_code=422, code="analysis_missing"
        )
    bpm = _num(analysis.get("bpm")) or 120.0
    bpm = min(240.0, max(40.0, bpm))
    beats = [b for b in raw_beats if b <= duration]
    if len(beats) < 2:
        step = 60.0 / bpm
        beats = [round(i * step, 3) for i in range(int(duration / step) + 1)]
    diffs = [b - a for a, b in itertools.pairwise(beats) if b > a]
    beat = statistics.median(diffs) if diffs else 60.0 / bpm
    downbeats = sorted(
        {d for d in (_num(x) for x in analysis.get("downbeats") or []) if d is not None and 0 <= d <= duration}
    )
    if len(downbeats) < 2:
        downbeats = beats[::4]
    bar_diffs = [b - a for a, b in itertools.pairwise(downbeats) if b > a]
    bar = statistics.median(bar_diffs) if bar_diffs else 4 * beat

    sections: list[_Section] = []
    for raw in analysis.get("sections") or []:
        start, end = _num(raw.get("start")), _num(raw.get("end"))
        if start is not None and end is not None and end > start:
            sections.append(_Section(start, end, str(raw.get("label") or "verse"), _num(raw.get("energy")) or 0.5))
    sections.sort(key=lambda s: s.start)

    lyrics: list[tuple[float, float, str]] = []
    for line in lyric_lines:
        start, end = _num(line.get("start")), _num(line.get("end"))
        if start is not None and line.get("id") is not None:
            lyrics.append((start, end if end is not None and end > start else start + 4.0, str(line["id"])))
    vocal_hop, vocal = 0.0, []
    raw_vocal = analysis.get("vocal_activity")
    if isinstance(raw_vocal, Mapping):
        vocal_hop = _num(raw_vocal.get("hop")) or 0.0
        vocal = [min(1.0, max(0.0, _num(v) or 0.0)) for v in raw_vocal.get("values") or []]
    return _Song(duration, beats, downbeats, bar, beat, sections, lyrics, vocal_hop, vocal)


# --- start times -------------------------------------------------------------------------------


def _video_starts(
    song: _Song,
    analysis: Mapping[str, Any],
    *,
    strategy: str,
    segment: tuple[float, float] | None,
    start_sec: float | None,
    duration: float,
    count: int,
    warnings: list[str],
) -> list[float]:
    max_start = max(0.0, song.duration - duration)

    def clamp(t: float) -> float:
        if t > max_start:
            # Prefer the latest downbeat that still leaves room for the whole video.
            fitting = [d for d in song.downbeats if max_start - song.bar <= d <= max_start]
            return round(fitting[-1] if fitting else max_start, 3)
        return round(max(0.0, t), 3)

    def best_suggested() -> float | None:
        suggestions = [s for s in analysis.get("suggested_segments") or [] if _num(s.get("start")) is not None]
        if not suggestions:
            return None
        best = min(
            suggestions, key=lambda s: (abs((_num(s.get("duration")) or 0) - duration), -(_num(s.get("score")) or 0))
        )
        return _num(best.get("start"))

    def chorus_starts() -> list[float]:
        candidates = sorted(
            (c for c in analysis.get("chorus_candidates") or [] if _num(c.get("start")) is not None),
            key=lambda c: -(_num(c.get("score")) or 0),
        )
        starts = [_num(c["start"]) for c in candidates]
        if not starts:
            choruses = sorted((s for s in song.sections if s.label == "chorus"), key=lambda s: -s.energy)
            starts = [s.start for s in choruses]
        return [s for s in starts if s is not None]

    segment_start = _num(segment[0]) if segment else None

    def fallback_start() -> float:
        for candidate in (segment_start, best_suggested(), *chorus_starts()):
            if candidate is not None:
                return candidate
        return 0.0

    if strategy == "custom":
        wanted = _num(start_sec)
        if wanted is None:
            warnings.append("開始位置が指定されていないため、選択区間の開始位置を使用しました")
            wanted = fallback_start()
        start = clamp(wanted)
        if abs(start - wanted) > 0.05:
            warnings.append(f"開始位置が曲の範囲を超えるため、{start:.1f}秒に調整しました")
        return [start] * count

    if strategy == "chorus":
        starts = chorus_starts()
        if not starts:
            warnings.append("サビ候補が見つからないため、選択区間の開始位置を使用しました")
            return [clamp(fallback_start())] * count
        fitting = [s for s in starts if s <= max_start + 0.05]
        return [clamp(song.snap_to_downbeat(fitting[0] if fitting else starts[0]))] * count

    if strategy == "varied":
        # Rotate among chorus / verse / highlight starts and the suggested segment that
        # fits this length. A start must open on singing: candidates whose window has
        # lyrics (or vocals) less than half the time, or that open on an intro/bridge,
        # are dropped (unless fewer than two remain); the rest go best-first.
        pinned = clamp(song.snap_to_downbeat(segment_start)) if segment_start is not None else None
        raw: list[float | None] = list(chorus_starts())
        raw += [
            _num(s.get("start")) for s in analysis.get("suggested_segments") or []
            if abs((_num(s.get("duration")) or 0.0) - duration) <= _SUGGESTED_DURATION_TOLERANCE * duration
        ]  # fmt: skip
        raw += [_num(h.get("start")) for h in analysis.get("highlights") or []]
        raw += [s.start for s in song.sections if s.label in ("verse", "chorus")]
        candidates: list[float] = []

        def add(value: float | None) -> None:
            if value is None:
                return
            start = clamp(song.snap_to_downbeat(value))
            if all(abs(start - c) >= 1.0 for c in [*candidates, *([pinned] if pinned is not None else [])]):
                candidates.append(start)

        def score(start: float) -> float:
            coverage = song.sung_coverage(start, start + duration)
            return (0.5 if coverage is None else coverage) + 0.5 * song.mean_energy(start, start + duration)

        def suitable(start: float) -> bool:
            coverage = song.sung_coverage(start, start + duration)
            opening = song.section_overlap(start, start + _VARIED_OPENING_SEC, _QUIET_SECTIONS)
            return (coverage is None or coverage >= _VARIED_MIN_COVERAGE) and opening <= _VARIED_OPENING_SEC / 2

        for value in raw:
            add(value)
        good = [c for c in candidates if suitable(c)]
        wanted = min(count, 4) - (pinned is not None)
        if len(good) < wanted and max_start >= 2.0:
            # Few usable landmarks: spread extra starts over the downbeats that fit.
            usable = [d for d in song.downbeats if d <= max_start] or [0.0]
            spread = min(count, 8)
            for k in range(spread):
                add(usable[round(k * (len(usable) - 1) / max(1, spread - 1))])
            good = [c for c in candidates if suitable(c)]
        pool = sorted(good if len(good) >= 2 else candidates, key=score, reverse=True)
        ordered = ([pinned] if pinned is not None else []) + pool or [0.0]
        return [ordered[i % len(ordered)] for i in range(count)]

    # "segment" (default)
    if segment_start is None:
        warnings.append("区間が未設定のため、おすすめ区間の開始位置を使用しました")
        wanted = fallback_start()
    else:
        wanted = segment_start
    start = clamp(wanted)
    if segment_start is not None and abs(start - segment_start) > 0.05:
        warnings.append(
            f"選択区間から{duration:g}秒取ると曲の終わりを超えるため、開始位置を{start:.1f}秒に調整しました"
        )
    return [start] * count


# --- shot boundaries ------------------------------------------------------------------------------


def _shot_lengths(bar_ms: int) -> tuple[tuple[int, bool], tuple[int, bool]]:
    """(short, long) shot lengths in ms, each flagged whether it is a whole number of bars.

    Normally 1 and 2 bars. Bars outside 1.5–6 s use the whole-bar counts that fit
    (fast songs: e.g. 2 and 4 bars); when only one whole-bar length fits (slow
    songs) the other length is a half-bar multiple, so rhythm can still vary.
    """
    whole = [n * bar_ms for n in range(1, 9) if _MIN_MS <= n * bar_ms <= _MAX_MS]
    halves = [n * bar_ms // 2 for n in range(1, 17) if n % 2 and _MIN_MS <= n * bar_ms // 2 <= _MAX_MS]
    if not whole:
        lengths = halves or [min(_MAX_MS, max(_MIN_MS, bar_ms))]
        return (lengths[0], False), (lengths[-1], False)
    short = whole[0]
    if len(whole) >= 2:
        return (short, True), (min(whole[1:], key=lambda w: abs(w - 2 * short)), True)
    if halves:
        return (short, True), (min(halves, key=lambda h: abs(h - 1.5 * short)), False)
    return (short, True), (short, True)


def _cut_points(song: _Song, start: float, duration_ms: int, rng: random.Random) -> list[int]:
    """Shot durations (ms) whose internal boundaries land on beats and that sum to ``duration_ms``."""
    if duration_ms <= _MAX_MS and duration_ms < 2 * _MIN_MS:
        return [duration_ms]
    bar_ms = max(1, round(song.bar * 1000))
    rel_beats = sorted({round((b - start) * 1000) for b in song.beats if 0 < (b - start) * 1000 < duration_ms})
    downbeat_set = {round((d - start) * 1000) for d in song.downbeats}
    lengths = _shot_lengths(bar_ms)
    pattern = rng.choice(_BAR_PATTERNS)
    offset = rng.randrange(len(pattern))

    def choose(low: int, high: int, target: int, on_bar: bool) -> int | None:
        lo, hi = bisect.bisect_left(rel_beats, low), bisect.bisect_right(rel_beats, high)
        window = rel_beats[lo:hi]
        if not window:
            return None
        # For whole-bar shots, bar lines (downbeats) win whenever one is in range.
        penalty = bar_ms if on_bar else 0
        return min(window, key=lambda c: abs(c - target) + (0 if c in downbeat_set else penalty))

    cuts: list[int] = []
    t = 0
    shot_index = 0
    while True:
        remaining = duration_ms - t
        units = pattern[(offset + shot_index) % len(pattern)]
        energy = song.energy_class(start + t / 1000)
        if energy == "high" and units == 2 and rng.random() < 0.4:
            units = 1
        elif energy == "low" and units == 1 and rng.random() < 0.4:
            units = 2
        length, on_bar = lengths[units - 1]
        target = t + length
        low, high = t + _MIN_MS, min(t + _MAX_MS, duration_ms - _MIN_MS)
        if remaining <= _MAX_MS:
            # The rest could be the last shot; cut once more only if the pattern wants a shorter shot.
            if remaining < 2 * _MIN_MS or target > high + bar_ms // 2:
                break
            cut = choose(low, high, target, on_bar)
            if cut is None or abs(cut - target) > bar_ms:
                break
        else:
            cut = choose(low, high, target, on_bar)
            if cut is None:  # no beat in range (very sparse beat grid): cut on the time grid
                cut = min(high, max(low, target))
        cuts.append(cut)
        t = cut
        shot_index += 1
    bounds = [0, *cuts, duration_ms]
    return [b - a for a, b in itertools.pairwise(bounds)]


def _end_bands(duration_ms: int, limits: Sequence[int]) -> list[tuple[int, int]]:
    """Where shot ``j`` of a split into ``len(limits)`` shots can end and still leave room for the
    rest: the later shots need at least 1.5 s each and cover at most their limits."""
    n = len(limits)
    rest_max = list(itertools.accumulate(reversed(limits[1:]), initial=0))[::-1]
    return [(duration_ms - rest_max[j], duration_ms - _MIN_MS * (n - 1 - j)) for j in range(n)]


def _split_feasible(beats: Sequence[int], duration_ms: int, limits: Sequence[int]) -> bool:
    """Whether ``[0, duration_ms]`` splits into ``len(limits)`` shots with inner boundaries on
    ``beats`` (sorted) and 1.5 s ≤ shot ``j`` ≤ ``limits[j]``."""
    n = len(limits)
    reach = [0]  # sorted boundaries the shots so far can end on
    for j, (cap, (low, high)) in enumerate(zip(limits, _end_bands(duration_ms, limits), strict=True)):
        if j == n - 1:
            ends: Sequence[int] = [duration_ms]
        else:
            low, high = max(low, reach[0] + _MIN_MS), min(high, reach[-1] + cap)
            ends = beats[bisect.bisect_left(beats, low) : bisect.bisect_right(beats, high)]
        following = []
        for b in ends:
            i = bisect.bisect_left(reach, b - cap)  # a reachable start in [b - cap, b - 1.5 s]
            if i < len(reach) and reach[i] <= b - _MIN_MS:
                following.append(b)
        if not following:
            return False
        reach = following
    return True


@functools.lru_cache(maxsize=512)
def _even_limits(
    beats: tuple[int, ...], duration_ms: int, caps: tuple[int, ...], usable_sec: float, pads: tuple[float, ...]
) -> tuple[int, ...] | None:
    """Per-shot length limits (ms) of the most even split within ``caps``, None when there is none.

    Minimax: the smallest footage need S (= 1 / slowest speed) for which a split exists, plus 1 %.
    It does not depend on the per-video weights, so the videos of a batch that share a start compute
    it once (and later plans with the same inputs, e.g. the estimate and then the batch, reuse it).
    """
    if not _split_feasible(beats, duration_ms, caps):
        return None
    low, high = 0.0, 1.0 / FIT_MIN_SPEED
    for _ in range(20):
        mid = (low + high) / 2
        limits = [min(c, int((mid * usable_sec - pads[j]) * 1000)) for j, c in enumerate(caps)]
        if min(limits) >= _MIN_MS and _split_feasible(beats, duration_ms, limits):
            high = mid
        else:
            low = mid
    return tuple(min(c, int((high * 1.01 * usable_sec - pads[j]) * 1000)) for j, c in enumerate(caps))


def _fit_cuts(
    song: _Song,
    start: float,
    duration_ms: int,
    n: int,
    usable_sec: float,
    pads: Sequence[float],
    weights: Sequence[float],
    avoid: Collection[tuple[int, ...]] = frozenset(),
) -> list[int] | None:
    """``n`` shot durations (ms) on beats that clips with ``usable_sec`` of footage can play at
    ≥ FIT_MIN_SPEED (each shot plus its crossfade pad), or None when the beat grid allows none.

    As even as the grid allows: first the smallest "slowest shot" that is feasible (minimax),
    then the cheapest split under that limit — close to the per-shot ideal (``weights``),
    little slow motion, and boundaries on section/lyric-line starts or downbeats. With
    ``avoid`` (splits other videos took and whose clip orders are all used up), the cheapest
    split within the ≥ FIT_MIN_SPEED limits that is not one of them: the next most even.
    """
    caps = [min(_MAX_FIT_MS, int((usable_sec / FIT_MIN_SPEED - pads[j]) * 1000)) for j in range(n)]
    if n == 1:
        return [duration_ms] if duration_ms <= caps[0] and (duration_ms,) not in avoid else None
    if sum(caps) < duration_ms:
        return None
    # The same rounding as _cut_points, so fitted cuts are on beats exactly like natural ones.
    beats = tuple(sorted({round((b - start) * 1000) for b in song.beats if 0 < (b - start) * 1000 < duration_ms}))
    even = _even_limits(beats, duration_ms, tuple(caps), usable_sec, tuple(pads))
    if even is None:
        return None
    if not avoid:
        caps = list(even)
    points = [0, *beats, duration_ms]

    def window(b: int, cap: int) -> list[int]:
        """Boundaries ``a`` a shot ending at ``b`` may start from (1.5 s ≤ b - a ≤ cap)."""
        return points[bisect.bisect_left(points, b - cap) : bisect.bisect_right(points, b - _MIN_MS)]

    downbeats = {round((d - start) * 1000) for d in song.downbeats}
    half_beat = song.beat * 500
    anchors = sorted(
        [(sec.start - start) * 1000 for sec in song.sections] + [(t - start) * 1000 for t, _, _ in song.lyrics]
    )

    def boundary_cost(b: int) -> float:
        if b == duration_ms:
            return 0.0
        i = bisect.bisect_left(anchors, b - half_beat)
        if i < len(anchors) and anchors[i] <= b + half_beat:
            return 0.0  # a section or lyric-line start
        return 0.02 if b in downbeats else 0.08

    total_weight = sum(weights)
    ideals = [duration_ms * w / total_weight for w in weights]
    keep = len(avoid) + 1  # the cheapest ``keep`` splits per state: one of them is not avoided
    # best[j][b]: the cheapest ways for shot j - 1 to end at b, as (cost, its start a, rank of the way to a).
    best: list[dict[int, list[tuple[float, int, int]]]] = [{0: [(0.0, -1, 0)]}]
    for j, (low, high) in enumerate(_end_bands(duration_ms, caps)):
        layer: dict[int, list[tuple[float, int, int]]] = {}
        ideal, pad = ideals[j], pads[j]
        if not best[j]:
            return None
        low, high = max(low, min(best[j]) + _MIN_MS), min(high, max(best[j]) + caps[j])  # reachable from a start
        ends = [duration_ms] if j == n - 1 else beats[bisect.bisect_left(beats, low) : bisect.bisect_right(beats, high)]
        for b in ends:
            extra = boundary_cost(b)
            options: list[tuple[float, int, int]] = []
            for a in window(b, caps[j]):
                length = b - a
                for rank, (previous, _, _) in enumerate(best[j].get(a, ())):
                    need = (length / 1000 + pad) / usable_sec
                    cost = previous + ((length - ideal) / ideal) ** 2 + FIT_SLOW_WEIGHT * max(0.0, need - 1.0) ** 2
                    options.append((cost + extra, a, rank))
            if options:  # ties go to the earliest start
                layer[b] = [min(options)] if keep == 1 else heapq.nsmallest(keep, options)
        best.append(layer)
    for top in range(len(best[n].get(duration_ms, ()))):
        bounds, b, rank = [duration_ms], duration_ms, top
        for j in range(n, 0, -1):
            _, b, rank = best[j][b][rank]
            bounds.append(b)
        bounds.reverse()
        cuts = [b - a for a, b in itertools.pairwise(bounds)]
        if tuple(cuts) not in avoid:
            return cuts
    return None


def _fit_usable(pool: Sequence[_PoolClip], k: int) -> float:
    """Footage (s) of the clip with the least of it among the ``k`` longest (all of them for k ≥ the pool)."""
    usable = sorted((c.duration - 2 * c.margin for c in pool), reverse=True)
    return usable[min(k, len(usable)) - 1]


def _fit_weights(seed: int, video: int, k: int, count: int) -> list[float]:
    """Per-shot weights of a video's fit into ``k`` shots: equal for one video, else ±10 % per shot."""
    if count == 1:
        return [1.0] * k
    weight_rng = random.Random(_stable_int("fit", seed, video, k))
    return [weight_rng.uniform(0.9, 1.1) for _ in range(k)]


def _fit_to_pool(
    song: _Song,
    start: float,
    duration_ms: int,
    cuts: list[int],
    pool: Sequence[_PoolClip],
    crossfade: bool,
    seed: int,
    video: int,
    count: int,
) -> tuple[list[int], bool, bool]:
    """(cuts, fitted, crossfade kept): a video with more beat cuts than distinct pool clips is
    re-cut into the fewest beat shots that need no repeat (generated mode only).

    A crossfade video is fitted with the fade padding first, else without it (its transition
    then becomes a cut: better than a repeat). When even ``len(pool)`` shots cannot fill the
    video, more shots are tried — each one beyond the pool is a repeat (last resort). No
    draw from the video's RNG. The minimax leaves little room for the per-video weights, so
    videos that share a start usually get the same cuts here; ``plan_batch`` then gives them
    different clip orders, and the next most even split once every order is taken.
    """
    distinct = len(pool)
    if len(cuts) <= distinct:
        return cuts, False, crossfade
    for k in range(distinct, len(cuts)):
        u = _fit_usable(pool, k)
        if u <= 0:
            continue
        weights = _fit_weights(seed, video, k, count)
        for padded in (True, False) if crossfade else (False,):
            fitted = _fit_cuts(song, start, duration_ms, k, u, _shot_pads(k, padded), weights)
            if fitted is not None:
                return fitted, True, padded
    return cuts, False, crossfade


# --- clip pool ---------------------------------------------------------------------------------------


@dataclass
class _PoolClip:
    ref: str  # clip_key for generated/cached specs, clip id for library uploads
    duration: float  # length available for planning (seconds)
    margin: float
    scene_id: str | None = None
    section: str | None = None
    lyric_ref: str | None = None
    clip_id: str | None = None  # set for cache hits and uploads
    spec: dict[str, Any] | None = None


@functools.lru_cache(maxsize=4096)
def _variant_prompt(prompt: str, variant: int) -> str:
    if variant <= 0:
        return prompt
    hint = _VARIANT_HINTS[(variant - 1) % len(_VARIANT_HINTS)]
    # Re-sanitizing keeps the safety clauses at the end of the prompt.
    return sanitize_prompt(f"{prompt} {hint}", forbidden_names=[])[0]


def _scene_weights(
    scenes: Sequence[Mapping[str, Any]],
    song: _Song,
    windows: Sequence[tuple[float, Sequence[int]]],
) -> list[float]:
    """Relevance of each scene for the planned videos: seconds of its section (and lyric line) on screen."""
    section_seconds: dict[str, float] = {}
    lyric_seconds: dict[str, float] = {}
    for start, durations_ms in windows:
        t = start
        for duration_ms in durations_ms:
            shot = duration_ms / 1000
            section = song.section_at(t + shot / 2)
            if section is not None:
                section_seconds[section.label] = section_seconds.get(section.label, 0.0) + shot
            for line_id in song.lyric_ids(t, t + shot):
                lyric_seconds[line_id] = lyric_seconds.get(line_id, 0.0) + shot
            t += shot
    raw = [
        section_seconds.get(str(scene.get("section")), 0.0) + 2.0 * lyric_seconds.get(str(scene.get("lyric_ref")), 0.0)
        for scene in scenes
    ]
    relevant = [w for w in raw if w > 0]
    if not relevant:
        return [1.0] * len(scenes)
    # Scenes of other sections keep some weight: same world, and a pool needs variety.
    floor = 0.4 * statistics.fmean(relevant)
    return [max(w, floor) for w in raw]


def _allocation_order(weights: Sequence[float], slots: int) -> list[tuple[int, int]]:
    """``(scene index, take)`` pairs, most wanted first (D'Hondt over the weights; ties → storyboard order)."""
    counts = [0] * len(weights)
    order: list[tuple[int, int]] = []
    for _ in range(slots):
        best = max(range(len(weights)), key=lambda i: (weights[i] / (counts[i] + 1), -i))
        order.append((best, counts[best]))
        counts[best] += 1
    return order


def _generated_pool(
    *,
    scenes: Sequence[Mapping[str, Any]],
    weights: Sequence[float],
    provider: str,
    model: str,
    clip_duration: float,
    existing_by_key: Mapping[str, Mapping[str, Any]],
    target: int,
    max_new: int | None,
    batch_seed: int,
    fresh: bool,
    preset_id: str,
    palette: Sequence[str],
) -> list[_PoolClip]:
    pool: list[_PoolClip] = []
    extras: list[_PoolClip] = []
    new_count = 0
    wanted = _allocation_order(weights, MAX_POOL)
    paid_slots = target if max_new is None else min(target, max_new)
    if paid_slots <= len(scenes):
        # A small pool shows distinct scenes: an uncached alternate take goes behind every first
        # take (stable sort); cached takes keep their place, so new clips never go up.
        def is_cached(entry: tuple[int, int]) -> bool:
            scene, variant = scenes[entry[0]], entry[1]
            prompt = _variant_prompt(str(scene.get("prompt_en") or ""), variant)
            negative = str(scene.get("negative_prompt_en") or "")
            clip_seed = _stable_int("clip", prompt, negative, variant, batch_seed if fresh else "")
            key = make_cache_key(provider, model, prompt, negative, clip_duration, ASPECT_RATIO, clip_seed)
            return key in existing_by_key

        wanted = sorted(wanted, key=lambda entry: entry[1] > 0 and not is_cached(entry))
    # Every other take (round-robin) is still considered for free cache hits.
    round_robin = [(i % len(scenes), i // len(scenes)) for i in range(MAX_POOL)]
    for scene_index, variant in dict.fromkeys([*wanted, *round_robin]):
        if len(pool) + len(extras) >= MAX_POOL:
            break
        scene = scenes[scene_index]
        prompt = _variant_prompt(str(scene.get("prompt_en") or ""), variant)
        negative = str(scene.get("negative_prompt_en") or "")
        clip_seed = _stable_int("clip", prompt, negative, variant, batch_seed if fresh else "")
        key = make_cache_key(provider, model, prompt, negative, clip_duration, ASPECT_RATIO, clip_seed)
        cached = existing_by_key.get(key)
        if len(pool) >= target and cached is None:
            continue
        if cached is None and max_new is not None and new_count >= max_new:
            continue
        spec = {
            "key": "",
            "scene_id": scene.get("id"),
            "prompt": prompt,
            "negative_prompt": negative,
            "duration": clip_duration,
            "seed": clip_seed,
            "cache_key": key,
            "existing_clip_id": str(cached["id"]) if cached is not None else None,
            "variant": variant,
            "section": scene.get("section"),
            "hints": {
                "preset": preset_id,
                "palette": list(palette),
                "scene_type": scene.get("scene_type"),
                "camera_move": scene.get("camera_move"),
                "section": scene.get("section"),
            },
        }
        clip = _PoolClip(
            ref="",
            duration=(_num(cached.get("duration")) if cached is not None else None) or clip_duration,
            margin=GENERATED_EDGE_MARGIN,
            scene_id=scene.get("id"),
            section=scene.get("section"),
            lyric_ref=scene.get("lyric_ref"),
            clip_id=spec["existing_clip_id"],
            spec=spec,
        )
        if len(pool) < target:
            pool.append(clip)
            new_count += cached is None
        else:
            extras.append(clip)  # free cache hits beyond the target add variety at no cost
    return pool + extras


# --- per-video planning -------------------------------------------------------------------------------


Footage = tuple[str, int]  # (clip ref, in-point bucket)
Framing = tuple[bool, str, int]  # (mirrored, move class: a pan / "gentle" / "punch", zoom direction)
Move = tuple[str, int]  # (pan / "gentle" / "punch", zoom direction: 1 in, -1 out, 0 still or not told apart)


def _zoom_sign(zoom_from: float, zoom_to: float) -> int:
    """Zoom direction: 1 in, -1 out, 0 still."""
    return (zoom_to > zoom_from) - (zoom_to < zoom_from)


def _move_class(pan: str, zoom_from: float, zoom_to: float) -> Move:
    """A camera move as it reads on screen.

    Library moves (pans, 3–12 % zooms) are told apart by pan and zoom direction. A generated clip's slight
    centred zoom (≤ GENERATED_MAX_ZOOM_DELTA) looks the same in or out — its own camera move dominates —
    so every such zoom is one class, ``("gentle", 0)``; the static crop (GENERATED_CROP_ZOOM) and the static
    punch-in (PUNCH_IN_ZOOM) are two more, ``("crop", 0)`` and ``("punch", 0)``.
    """
    if zoom_from == zoom_to and zoom_from >= PUNCH_IN_ZOOM[0] - 1e-9:
        return "punch", 0
    if pan == "center" and max(zoom_from, zoom_to) <= 1.0 + GENERATED_MAX_ZOOM_DELTA + 1e-9:
        return "gentle", 0
    if zoom_from == zoom_to:
        return "crop", 0
    return str(pan), _zoom_sign(zoom_from, zoom_to)


_GENTLE: Move = ("gentle", 0)
_CROP: Move = ("crop", 0)
_PUNCH: Move = ("punch", 0)


def _framing(hflip: bool, pan: str, zoom_from: float, zoom_to: float) -> Framing:
    """How a shot frames its footage: mirrored or not, and its camera move as it reads on screen."""
    return (bool(hflip), *_move_class(pan, zoom_from, zoom_to))


@dataclass
class _Usage:
    global_uses: dict[str, int] = field(default_factory=dict)
    positions: dict[tuple[str, int], int] = field(default_factory=dict)
    framings: dict[Footage, set[Framing]] = field(default_factory=dict)

    def copy(self) -> _Usage:
        return _Usage(dict(self.global_uses), dict(self.positions), {k: set(v) for k, v in self.framings.items()})

    def add(self, footage: Footage, framing: Framing) -> None:
        self.framings.setdefault(footage, set()).add(framing)


def _speed_and_in(
    clip: _PoolClip,
    shot_sec: float,
    energy: str,
    rng: random.Random,
    usage: _Usage,
    *,
    pad: float = 0.0,
    fitted: bool = False,
    taken: Sequence[tuple[float, float]] = (),
    room_after: float = 0.0,
) -> tuple[float, float, int]:
    """(speed, src_in, position index) with ``src_in + (shot_sec + pad) * speed <= clip.duration``.

    ``pad`` is the crossfade footage a fitted shot reserves; a fitted shot gets no stylistic
    slow-down. ``taken`` are the source windows this clip already showed in the video: the
    in-point that overlaps them least (with REPEAT_GAP_SEC) wins. ``room_after`` is the footage
    (gaps included) this clip may still have to show later in the video: an in-point that
    leaves a free stretch that long wins next (packing next to an earlier window or at an
    end of the clip, so the later shots stay disjoint), then the least used one.
    """
    # Fitted shots are already as slow as the footage requires: no energy slow-down on top.
    desired = (1.0,) if fitted else {"high": (1.0,), "mid": (1.0, 1.0, 0.9), "low": (1.0, 0.9, 0.85, 0.8)}[energy]
    speed = rng.choice(desired)
    margin = clip.margin
    footage = shot_sec + pad
    if (clip.duration - 2 * margin) / footage < speed:
        speed = (clip.duration - 2 * margin) / footage
        if speed < MIN_NATURAL_SPEED:
            margin = 0.0
            speed = min(rng.choice(desired), clip.duration / footage)
    speed = max(0.01, _floor3(speed))
    span = footage * speed
    slack = max(0.0, clip.duration - span)
    low = min(margin, slack / 2)
    high = max(low, slack - low)
    n_positions = max(1, int((high - low) // 0.5) + 1)
    positions = [low + (high - low) * k / max(1, n_positions - 1) for k in range(n_positions)]
    if taken:
        # The 0.5 s grid misses the packing spots: right after / before an earlier window (with the gap).
        after = {math.ceil((b + REPEAT_GAP_SEC) * 1000 - 1e-6) / 1000 for _, b in taken}
        spots = after | {_floor3(a - REPEAT_GAP_SEC - span) for a, _ in taken}
        positions += sorted(p for p in spots if low <= p <= high and all(abs(p - q) > 1e-6 for q in positions))

    def grid(k: int) -> int:
        """Position index on the 0.5 s grid (usage is balanced per grid position)."""
        if k < n_positions:
            return k
        return round((positions[k] - low) / (high - low) * (n_positions - 1)) if n_positions > 1 else 0

    def overlap(k: int) -> float:
        p = positions[k]
        return sum(max(0.0, min(b + REPEAT_GAP_SEC, p + span) - max(a - REPEAT_GAP_SEC, p)) for a, b in taken)

    def shortfall(k: int) -> float:
        """How much of ``room_after`` the longest free stretch left by this window lacks."""
        if room_after <= 0:
            return 0.0
        edge, longest = margin, 0.0
        for a, b in sorted([*taken, (positions[k], positions[k] + span)]):
            longest = max(longest, a - REPEAT_GAP_SEC - edge)
            edge = max(edge, b + REPEAT_GAP_SEC)
        longest = max(longest, clip.duration - margin - edge)
        return round(max(0.0, room_after - longest), 3)

    best = min(
        range(len(positions)),
        key=lambda k: (round(overlap(k), 6), shortfall(k), usage.positions.get((clip.ref, grid(k)), 0), rng.random()),
    )
    src_in = _floor3(min(positions[best], max(0.0, clip.duration - span)))
    while src_in > 0 and src_in + span > clip.duration + 1e-9:
        src_in = max(0.0, round(src_in - 0.001, 3))
    return speed, src_in, grid(best)


def _ken_burns(
    energy: str,
    rng: random.Random,
    avoid: frozenset[Move] = frozenset(),
    punch_in: bool = False,
    *,
    gentle: bool = False,
    crop: bool = False,
) -> tuple[float, float, str]:
    """(zoom_from, zoom_to, pan); a move in ``avoid`` (:func:`_move_class` of the moves this footage
    already had) is re-drawn a few times, so repeated footage looks different. ``punch_in``
    (a clip shown again in the same video) frames it tighter: zoom 1.12–1.19.

    ``gentle`` (generated clips: the footage already moves) keeps the frame centred and the zoom
    within GENERATED_MAX_ZOOM_DELTA (1/6 of the library move), and a punch-in holds still at
    1.15–1.19; ``crop`` (gentle only) holds still at GENERATED_CROP_ZOOM. Every gentle zoom reads
    the same, so for generated clips the caller picks one of the three visible framings (full frame,
    crop, punch-in; see :func:`_plan_video_shots`). Both modes draw the same random numbers per attempt.
    """
    for _ in range(6):
        delta = rng.uniform(0.06, 0.12) if energy == "high" else rng.uniform(0.03, 0.07)
        pan = "center" if rng.random() < (0.3 if energy == "high" else 0.45) else rng.choice(_PANS)
        zoom_in = rng.random() < 0.65
        if gentle:
            pan = "center"
            if punch_in:  # a static tighter framing: no glide stacked on the clip's own move
                zoom_from = zoom_to = PUNCH_IN_ZOOM[0] + min(delta, PUNCH_IN_ZOOM[1] - PUNCH_IN_ZOOM[0])
            elif crop:
                zoom_from = zoom_to = GENERATED_CROP_ZOOM
            else:
                delta *= GENERATED_MAX_ZOOM_DELTA / _LIBRARY_MAX_ZOOM_DELTA
                zoom_from, zoom_to = (1.0, 1.0 + delta) if zoom_in else (1.0 + delta, 1.0)
        else:
            base = 1.0 if pan == "center" else 1.06  # a pan needs headroom at both ends
            if punch_in:
                base, delta = PUNCH_IN_ZOOM[0], min(delta, PUNCH_IN_ZOOM[1] - PUNCH_IN_ZOOM[0])
            zoom_from, zoom_to = (base, base + delta) if zoom_in else (base + delta, base)
        if _move_class(pan, zoom_from, zoom_to) not in avoid:
            break
    return round(zoom_from, 3), round(zoom_to, 3), pan


@dataclass
class _ShotContext:
    shot_sec: float
    section: _Section | None
    lyric_ids: set[str]
    previous: str | None
    any_feasible: bool
    previous_scene: str | None = None
    scenes_in_video: Mapping[str, int] = field(default_factory=dict)  # earlier shots per scene id


def _clip_cost(
    clip: _PoolClip, ctx: _ShotContext, *, pool_size: int, in_video: Mapping[str, int], usage: _Usage
) -> float:
    """Lower is better: avoid repeats (adjacent, in-video, batch-wide) and the same scene again
    in the video (another take of it), prefer matching section/lyric."""
    value = 1000.0 if clip.ref == ctx.previous and pool_size > 1 else 0.0
    value += 50.0 * in_video.get(clip.ref, 0) + 6.0 * usage.global_uses.get(clip.ref, 0)
    if ctx.section is not None and clip.section is not None and clip.section != ctx.section.label:
        value += 4.0
    if clip.lyric_ref is not None and clip.lyric_ref in ctx.lyric_ids:
        value -= 3.0
    if clip.scene_id is not None:
        value += SCENE_REPEAT_COST * ctx.scenes_in_video.get(clip.scene_id, 0)
        if clip.scene_id == ctx.previous_scene:
            value += SCENE_ADJACENT_COST
    if ctx.any_feasible and clip.duration < ctx.shot_sec * MIN_NATURAL_SPEED:
        value += 200.0
    # Prefer clips long enough to play the shot at (near) normal speed.
    return value + 10.0 * max(0.0, 1.0 - (clip.duration - 2 * clip.margin) / ctx.shot_sec)


def _room_after(
    clip: _PoolClip,
    pool: Sequence[_PoolClip],
    in_video: Mapping[str, int],
    rest_ms: Sequence[int],
    rest_pads: Sequence[float],
) -> float:
    """Footage (s, with REPEAT_GAP_SEC per window) ``clip`` may still have to show later in the
    video: the later shots no unused clip is left for, spread over the pool (its longest ones,
    as a clip shows each at most at normal speed)."""
    unused = sum(1 for c in pool if c.ref != clip.ref and not in_video.get(c.ref))
    back = math.ceil(max(0, len(rest_ms) - unused) / len(pool))
    if not back:
        return 0.0
    usable = clip.duration - 2 * clip.margin
    longest = sorted((min(usable, ms / 1000 + pad) for ms, pad in zip(rest_ms, rest_pads, strict=True)), reverse=True)
    return sum(longest[:back]) + back * REPEAT_GAP_SEC


def _plan_video_shots(
    *,
    song: _Song,
    start: float,
    durations_ms: Sequence[int],
    pool: Sequence[_PoolClip],
    usage: _Usage,
    rng: random.Random,
    library: bool,
    allow_mirror: bool = False,
    pads: Sequence[float] | None = None,
    fitted: bool = False,
    taken_orders: Sequence[Sequence[str]] = (),
) -> list[dict[str, Any]]:
    """Pick a clip, in-point, speed and camera move per shot. A clip not yet in this video wins
    whenever one is long enough (fresh-first); a clip that has to come back gets the source
    window that overlaps its earlier ones least and a tighter framing it has not had (generated
    clips: a static punch-in, alternating with a static crop when the clip comes back again; their
    camera moves are slight, see :func:`_ken_burns`).

    ``taken_orders`` (a retry after an edit identical to an earlier video's) are the clip orders
    earlier videos with these cuts used: a clip that would follow one of them again costs more,
    so the video takes another order (no slower shots needed to make it different).
    """
    shots: list[dict[str, Any]] = []
    following = list(taken_orders)  # earlier orders this video's clips so far still follow
    in_video: dict[str, int] = {}
    scenes_in_video: dict[str, int] = {}
    windows: dict[str, list[tuple[float, float]]] = {}  # source windows per clip in this video
    moves: dict[str, set[Move]] = {}  # camera moves (:func:`_move_class`) per clip in this video
    last_move: dict[str, Move] = {}  # the clip's most recent move in this video
    previous: str | None = None
    previous_scene: str | None = None
    t_ms = 0
    pads = list(pads) if pads is not None else [0.0] * len(durations_ms)
    for index, (duration_ms, pad) in enumerate(zip(durations_ms, pads, strict=True)):
        shot_sec = duration_ms / 1000
        t0, t1 = start + t_ms / 1000, start + (t_ms + duration_ms) / 1000
        section = song.section_at((t0 + t1) / 2)
        energy = song.energy_class((t0 + t1) / 2)
        ctx = _ShotContext(
            shot_sec=shot_sec,
            section=section,
            lyric_ids=song.lyric_ids(t0, t1),
            previous=previous,
            any_feasible=any(c.duration >= shot_sec * MIN_NATURAL_SPEED for c in pool),
            previous_scene=previous_scene,
            scenes_in_video=scenes_in_video,
        )
        costs = [
            _clip_cost(c, ctx, pool_size=len(pool), in_video=in_video, usage=usage) + rng.random() * 2.0 for c in pool
        ]
        # Fresh-first is a hard rule, not a cost: a repeat only when no unused clip can play the shot.
        fresh = [
            i for i, c in enumerate(pool)
            if not in_video.get(c.ref) and c.duration >= (shot_sec + pad) * MIN_NATURAL_SPEED
        ]  # fmt: skip
        if following:
            for i, c in enumerate(pool):
                costs[i] += _ORDER_TAKEN_COST * sum(order[index] == c.ref for order in following)
        clip = pool[min(fresh or range(len(pool)), key=costs.__getitem__)]
        following = [order for order in following if order[index] == clip.ref]
        repeat = bool(in_video.get(clip.ref))
        speed, src_in, position = _speed_and_in(
            clip, shot_sec, energy, rng, usage, pad=pad, fitted=fitted, taken=windows.get(clip.ref, ()),
            room_after=_room_after(clip, pool, in_video, durations_ms[index + 1 :], pads[index + 1 :]),
        )  # fmt: skip
        footage = (clip.ref, int(src_in // SRC_IN_BUCKET_SEC))
        seen = usage.framings.get(footage, set())
        hflip = False
        if allow_mirror:  # mirroring is opt-in: it turns text in the footage into mirror writing
            plain, flipped = any(not f[0] for f in seen), any(f[0] for f in seen)
            if plain and not flipped:
                hflip = True
            elif not plain:
                hflip = rng.random() < (0.1 if library else 0.2)
            else:
                hflip = rng.random() < 0.5
        # Footage seen before gets a camera move it has not had with this mirroring.
        avoid = frozenset((f[1], f[2]) for f in seen if f[0] == hflip) | frozenset(moves.get(clip.ref, ()))
        crop = False
        if library:
            punch_in = repeat
        elif repeat:
            # A generated clip shown again in this video is framed tighter, as a static punch-in; when it
            # has to come back more than once, punch-in and the static crop alternate, so two uses in a
            # row (a pool of one) never share a framing.
            punch_in = last_move.get(clip.ref) != _PUNCH
            crop = not punch_in
        else:
            # Footage back from an earlier video gets a framing it has not had: a slight zoom in or out
            # reads the same, so the static crop, then the punch-in.
            crop = _GENTLE in avoid and _CROP not in avoid
            punch_in = _GENTLE in avoid and _CROP in avoid and _PUNCH not in avoid
        zoom_from, zoom_to, pan = _ken_burns(
            energy, rng, avoid, punch_in=punch_in, gentle=not library, crop=crop
        )
        shots.append(
            {
                "clip_key": None if library else clip.ref,
                "clip_id": clip.clip_id,
                "src_in": src_in,
                "duration": shot_sec,
                "hflip": hflip,
                "zoom_from": zoom_from,
                "zoom_to": zoom_to,
                "pan": pan,
                "speed": speed,
                "scene_id": clip.scene_id,
                "section": section.label if section else None,
            }
        )
        in_video[clip.ref] = in_video.get(clip.ref, 0) + 1
        if clip.scene_id is not None:
            scenes_in_video[clip.scene_id] = scenes_in_video.get(clip.scene_id, 0) + 1
        windows.setdefault(clip.ref, []).append((src_in, src_in + (shot_sec + pad) * speed))
        moves.setdefault(clip.ref, set()).add(_move_class(pan, zoom_from, zoom_to))
        last_move[clip.ref] = _move_class(pan, zoom_from, zoom_to)
        previous_scene = clip.scene_id
        usage.global_uses[clip.ref] = usage.global_uses.get(clip.ref, 0) + 1
        usage.positions[(clip.ref, position)] = usage.positions.get((clip.ref, position), 0) + 1
        usage.add(footage, _framing(hflip, pan, zoom_from, zoom_to))
        previous = clip.ref
        t_ms += duration_ms
    return shots


def _shot_signature(shots: Sequence[Mapping[str, Any]]) -> tuple[Any, ...]:
    return tuple((s["clip_key"] or s["clip_id"], s["src_in"], s["duration"], s["hflip"]) for s in shots)


def _assign_unused(
    videos: list[dict[str, Any]],
    pool: Sequence[_PoolClip],
    *,
    must_use: set[str],
    usage: _Usage,
    library: bool,
    rng: random.Random,
    pads: Sequence[Sequence[float]] | None = None,
    fitted: Sequence[bool] | None = None,
) -> None:
    """Swap clips that ended up unused (paid generations) into shots of over-used clips.

    An unused clip is in no video, so a swap never creates an in-video repeat. A shot of a
    fitted video (``fitted[v]``, crossfade footage ``pads[v][s]``) only takes a clip that plays
    it at ≥ FIT_MIN_SPEED.
    """

    def ref_of(shot: Mapping[str, Any]) -> str:
        return str(shot["clip_key"] or shot["clip_id"])

    def pad_of(v: int, s: int) -> float:
        return pads[v][s] if pads else 0.0

    for clip in pool:
        if clip.ref not in must_use or usage.global_uses.get(clip.ref, 0) > 0:
            continue
        best: tuple[float, int, int] | None = None
        for v, video in enumerate(videos):
            shots = video["shots"]
            for s, shot in enumerate(shots):
                current = ref_of(shot)
                if usage.global_uses.get(current, 0) < 2 or clip.duration < shot["duration"] * MIN_NATURAL_SPEED:
                    continue
                footage = shot["duration"] + pad_of(v, s)
                if fitted and fitted[v] and clip.duration - 2 * clip.margin < footage * FIT_MIN_SPEED:
                    continue
                neighbours = {ref_of(shots[i]) for i in (s - 1, s + 1) if 0 <= i < len(shots)}
                if clip.ref in neighbours:
                    continue
                score = -usage.global_uses[current] + (0.0 if shot.get("section") == clip.section else 0.5)
                if best is None or score < best[0]:
                    best = (score, v, s)
        if best is None:
            continue
        _, v, s = best
        shot = videos[v]["shots"][s]
        old = ref_of(shot)
        usage.global_uses[old] -= 1
        speed, src_in, position = _speed_and_in(
            clip, shot["duration"], "mid", rng, usage, pad=pad_of(v, s), fitted=bool(fitted and fitted[v])
        )
        shot.update(
            {
                "clip_key": None if library else clip.ref,
                "clip_id": clip.clip_id,
                "src_in": src_in,
                "speed": speed,
                "scene_id": clip.scene_id,
                "hflip": False,
            }
        )
        usage.global_uses[clip.ref] = usage.global_uses.get(clip.ref, 0) + 1
        usage.positions[(clip.ref, position)] = usage.positions.get((clip.ref, position), 0) + 1
        usage.add(
            (clip.ref, int(src_in // SRC_IN_BUCKET_SEC)),
            _framing(False, shot["pan"], shot["zoom_from"], shot["zoom_to"]),
        )


def _variety_score(videos: Sequence[Mapping[str, Any]]) -> float:
    """Distinct looks per shot: each footage (clip, in-point bucket) counts once, twice when it
    comes back framed visibly differently (another camera move, a punch-in or a mirror; a generated
    clip's slight zoom in or out counts as one framing); further repeats add nothing."""
    framings: dict[Footage, set[Framing]] = {}
    total = 0
    for video in videos:
        for shot in video["shots"]:
            total += 1
            footage = (str(shot["clip_key"] or shot["clip_id"]), int(shot["src_in"] // SRC_IN_BUCKET_SEC))
            framings.setdefault(footage, set()).add(
                _framing(shot["hflip"], shot["pan"], shot["zoom_from"], shot["zoom_to"])
            )
    return round(sum(min(2, len(f)) for f in framings.values()) / total, 3) if total else 0.0


def _variety_warning(
    *,
    library: bool,
    pool_size: int,
    pool_target: int,
    max_new: int | None,
    reuse: str,
    shots_total: int,
    n_scenes: int,
) -> str:
    """The variety warning with advice for the causes that apply (Japanese)."""
    advice: list[str] = []
    if library:
        advice.append(
            f"アップロード素材が {pool_size} 本しかなく、{shots_total} ショットで繰り返し使われています。"
            "素材を追加でアップロードするか、動画の本数を減らすと改善します"
        )
    else:
        if max_new is not None and pool_size < pool_target:
            advice.append(
                f"新規生成の上限（{max_new} 本）により素材が {pool_size} 本に抑えられています。"
                f"上限を {pool_target} 本程度まで引き上げると改善します"
            )
        if reuse == "economy":
            advice.append("再利用設定が「節約」です。「バランス」か「バリエーション最大」にすると改善します")
        elif pool_size >= MAX_POOL and math.ceil(shots_total / _REUSE_DIVISOR.get(reuse, 3)) > MAX_POOL:
            advice.append(
                f"動画の本数が多く、素材数が上限（{MAX_POOL} 本）に達しています。"
                "本数を減らすか、何回かに分けて生成すると改善します"
            )
        if n_scenes <= FEW_SCENES:
            advice.append(
                f"絵コンテのシーンが {n_scenes} 個しかなく、同じ場面の別テイクが多くなります。"
                "カスタム世界観に場面を書き足すか、絵コンテを作り直してシーンを増やすと改善します"
            )
    if not advice:
        advice.append(
            "動画の本数を減らすか、再利用設定を「バリエーション最大」にすると改善します"
            if reuse != "max_variety"
            else "動画の本数を減らすか、動画を短くすると改善します"
        )
    return VARIETY_WARNING_JA + "。".join(advice) + "。"


def _repeat_counts(
    shots: Sequence[Mapping[str, Any]], pads: Sequence[float], *, library: bool
) -> tuple[int, int, int, int]:
    """(repeat shots, overlapping repeats, adjacent repeats, scene repeats) of one video's final shots.

    A shot repeats when its clip was shown earlier in the video (generated mode) or, for
    uploads, only when its source window ``(src_in, src_in + (duration + pad) * speed)``
    comes closer than REPEAT_GAP_SEC to an earlier one of that clip (a long upload may give
    disjoint windows). An adjacent repeat follows a shot of the same clip (a pool of one).
    A scene repeat is another clip of a storyboard scene already shown.
    """
    windows: dict[str, list[tuple[float, float]]] = {}
    scene_refs: dict[str, set[str]] = {}
    repeats = overlaps = adjacent = scene_repeats = 0
    previous: str | None = None
    for shot, pad in zip(shots, pads, strict=True):
        ref = str(shot["clip_key"] or shot["clip_id"])
        window = (shot["src_in"], shot["src_in"] + (shot["duration"] + pad) * shot["speed"])
        earlier = windows.setdefault(ref, [])
        # Windows exactly REPEAT_GAP_SEC apart are disjoint (1 µs for float noise on the ms grid).
        gap = REPEAT_GAP_SEC - 1e-6
        overlapping = any(window[0] < b + gap and a - gap < window[1] for a, b in earlier)
        if earlier and (overlapping or not library):
            repeats += 1
            overlaps += overlapping
            adjacent += ref == previous
        earlier.append(window)
        previous = ref
        scene_id = shot.get("scene_id")
        if scene_id:
            if not library and scene_id in scene_refs and ref not in scene_refs[scene_id]:
                scene_repeats += 1
            scene_refs.setdefault(scene_id, set()).add(ref)
    return repeats, overlaps, adjacent, scene_repeats


def _pace_ja(longest: float, slowest: float) -> str:
    """How the fitted shots play (Japanese): the longest shot and the slowest speed, or 等速."""
    return f"最長{longest:.1f}秒・" + ("等速" if slowest >= 0.995 else f"最も遅いショット{slowest:.2f}倍速")


# --- public API ------------------------------------------------------------------------------------------


def plan_batch(
    *,
    analysis: Mapping[str, Any],
    lyric_lines: Sequence[Mapping[str, Any]],
    settings: Mapping[str, Any],
    storyboard: Mapping[str, Any] | None,
    segment: tuple[float, float] | None,
    provider_clip_duration: float,
    existing_clips: Sequence[Mapping[str, Any]],
    seed: int,
) -> dict[str, Any]:
    """Plan a batch: clip pool (``clip_specs``), one VideoPlan per video and stats (§6.4).

    ``settings`` is a GenerationSettings dict. Optional extension:
    ``settings["fresh_clips"]`` (bool) mixes ``seed`` into the clip seeds so
    no cached clip matches (used to force new footage). Raises
    :class:`ServiceError` (422) when no clip can be planned.

    Shots are 1.5–6 s on beats; a generated video whose beat cuts outnumber the
    pool is re-cut into fewer shots of up to 8 s (``VideoPlan.fit``), so no clip
    appears twice in a video unless even that cannot fill it (counted in the
    stats and ``stats.repeat_warning``). The pool is sized from the beat cuts and
    the fit runs after it, so the spend is the same as without fitting; the budget
    is still not an input.
    """
    warnings: list[str] = []
    song = _build_song(analysis, lyric_lines)
    preset = get_preset(str(settings.get("preset") or "cinematic"))
    provider = str(settings.get("provider") or "mock")
    library = provider == "library"
    count = max(1, int(settings.get("count") or 1))
    reuse = str(settings.get("reuse") or "balanced")
    clip_duration = float(provider_clip_duration)

    wanted_duration = _num(settings.get("duration_sec")) or 15.0
    duration_ms = round(wanted_duration * 1000)
    song_ms = math.floor(song.duration * 1000 + 1e-6)
    if duration_ms > song_ms:
        duration_ms = song_ms
        warnings.append(f"楽曲が{wanted_duration:g}秒より短いため、動画の長さを{song_ms / 1000:g}秒にしました")
    duration = duration_ms / 1000

    starts = _video_starts(
        song, analysis, strategy=str(settings.get("start_strategy") or "segment"), segment=segment,
        start_sec=settings.get("start_sec"), duration=duration, count=count, warnings=warnings,
    )  # fmt: skip
    batch_rng = random.Random(_stable_int("batch", seed))
    video_rngs = [random.Random(_stable_int("video", seed, i)) for i in range(count)]
    cut_lists = [_cut_points(song, starts[i], duration_ms, video_rngs[i]) for i in range(count)]
    beat_counts = [len(cuts) for cuts in cut_lists]
    beat_total = sum(beat_counts)
    # Drawn before the pool: a fitted crossfade video reserves the fades' footage.
    soft = preset["id"] in _SOFT_TRANSITION_PRESETS
    transitions = [
        "crossfade" if random.Random(_stable_int("video-look", seed, i)).random() < (0.5 if soft else 0.15) else "cut"
        for i in range(count)
    ]

    ready = [c for c in existing_clips if c.get("id") is not None and c.get("status", "ready") == "ready"]
    scenes = [s for s in (storyboard or {}).get("scenes") or [] if isinstance(s, Mapping) and s.get("prompt_en")]
    pool_target = 0
    fit_floor = 0
    max_new = settings.get("max_new_clips")
    if library:
        uploads = [c for c in ready if c.get("source") == "upload" and (_num(c.get("duration")) or 0) > 0]
        if not uploads:
            raise ServiceError(
                "ライブラリモードにはアップロード済みの素材が必要です", status_code=422, code="library_empty"
            )
        pool = [
            _PoolClip(ref=str(c["id"]), duration=float(c["duration"]), margin=UPLOAD_EDGE_MARGIN,
                      scene_id=c.get("scene_id"), clip_id=str(c["id"]))
            for c in uploads
        ]  # fmt: skip
    else:
        if not scenes:
            raise ServiceError(
                "絵コンテにシーンがないため、素材を計画できません", status_code=422, code="storyboard_empty"
            )
        model = str(settings.get("model") or _default_model(provider))
        existing_by_key: dict[str, Mapping[str, Any]] = {}
        for clip in ready:
            key = clip.get("cache_key")
            if key and key not in existing_by_key:
                existing_by_key[str(key)] = clip
        # Sized from the beat cuts (the fit below only shortens cut lists, never adds spend).
        fit_floor = fit_clip_floor(duration_ms, clip_duration)
        pool_target = pool_target_size(reuse, beat_total, max(beat_counts), len(scenes), fit_floor)
        weights = _scene_weights(scenes, song, list(zip(starts, cut_lists, strict=True)))
        pool = _generated_pool(
            scenes=scenes, weights=weights, provider=provider, model=model, clip_duration=clip_duration,
            existing_by_key=existing_by_key, target=pool_target,
            max_new=int(max_new) if max_new is not None else None, batch_seed=seed,
            fresh=bool(settings.get("fresh_clips")), preset_id=preset["id"], palette=preset["palette"],
        )  # fmt: skip
        if not pool:
            raise ServiceError(
                "新規生成数の上限が0で、再利用できる素材もないため動画を作成できません",
                status_code=422, code="no_clips",
            )  # fmt: skip
        if max_new is not None and int(max_new) <= 0:
            warnings.append(f"生成済みの素材{len(pool)}本だけで構成します")
        elif max_new is not None and len(pool) < pool_target:
            warnings.append(f"新規生成の上限（{int(max_new)}本）により、素材数を{len(pool)}本に抑えました")
        for index, clip in enumerate(pool):
            clip.ref = f"c{index + 1:02d}"

    fitted = [False] * count
    if not library:  # uploads keep their beat cuts (long uploads give disjoint windows instead)
        for index in range(count):
            cut_lists[index], fitted[index], crossfade = _fit_to_pool(
                song, starts[index], duration_ms, cut_lists[index], pool,
                transitions[index] == "crossfade", seed, index, count,
            )  # fmt: skip
            if not crossfade:
                transitions[index] = "cut"  # only fits without the fades' footage: a cut beats a repeat
    pads = [
        _shot_pads(len(cuts), transitions[i] == "crossfade") if fitted[i] else [0.0] * len(cuts)
        for i, cuts in enumerate(cut_lists)
    ]
    shots_total = sum(len(cuts) for cuts in cut_lists)

    usage = _Usage()
    videos: list[dict[str, Any]] = []
    signatures: set[tuple[Any, ...]] = set()
    grades = [g for g in preset["color_grades"] if g in COLOR_GRADES] or ["natural"]
    grade_offset = batch_rng.randrange(len(grades))
    lyric_style = str(settings.get("lyric_style") or "simple")
    style_cycle = [s for s in preset["lyric_styles"] if s in LYRIC_STYLES] or ["simple"]
    color_grade = str(settings.get("color_grade") or "auto")
    text_style = {k: v for k, v in dict(settings.get("text_style") or {}).items() if v is not None}
    allow_mirror = bool(settings.get("allow_mirror", False))

    # Videos that share a start are fitted to the same (most even) cuts: a video whose edit came out
    # identical to an earlier one's is re-planned with another clip order, and only when every order
    # those cuts allow is taken (``exhausted``) re-cut on the next most even split.
    orders: dict[tuple[int, ...], list[list[str]]] = {}  # clip order of the earlier videos, per cut list
    exhausted: dict[float, set[tuple[int, ...]]] = {}  # per start
    for index in range(count):
        shots: list[dict[str, Any]] = []
        attempt = 0
        for _ in range(_MAX_REFITS + 1):
            avoid = exhausted.setdefault(starts[index], set())
            if fitted[index] and tuple(cut_lists[index]) in avoid:
                k = len(cut_lists[index])
                alternative = _fit_cuts(
                    song, starts[index], duration_ms, k, _fit_usable(pool, k), pads[index],
                    _fit_weights(seed, index, k, count), avoid=frozenset(avoid),
                )  # fmt: skip
                if alternative is not None:
                    cut_lists[index] = alternative
                elif shots:
                    break  # no other split: keep the edit as it is
            taken = orders.get(tuple(cut_lists[index]), [])
            for _ in range(6):
                rng = video_rngs[index] if attempt == 0 else random.Random(_stable_int("video", seed, index, attempt))
                trial_usage = usage.copy()
                shots = _plan_video_shots(
                    song=song, start=starts[index], durations_ms=cut_lists[index], pool=pool, usage=trial_usage,
                    rng=rng, library=library, allow_mirror=allow_mirror, pads=pads[index], fitted=fitted[index],
                    taken_orders=taken if attempt else (),
                )  # fmt: skip
                attempt += 1
                if _shot_signature(shots) not in signatures:
                    break
            if _shot_signature(shots) not in signatures or not fitted[index]:
                break
            avoid.add(tuple(cut_lists[index]))
        usage = trial_usage
        signatures.add(_shot_signature(shots))
        orders.setdefault(tuple(cut_lists[index]), []).append([str(s["clip_key"] or s["clip_id"]) for s in shots])
        videos.append(
            {
                "index": index,
                "title": f"{preset['name_en']} #{index + 1}",
                "seed": _stable_int("video", seed, index),
                "start": starts[index],
                "duration": duration,
                "lyric_style": style_cycle[index % len(style_cycle)] if lyric_style == "auto" else lyric_style,
                "text_style": dict(text_style),
                "color_grade": grades[(grade_offset + index) % len(grades)] if color_grade == "auto" else color_grade,
                "shots": shots,
                "transition": transitions[index],
            }
        )

    must_use = {clip.ref for clip in pool if not library and clip.clip_id is None}
    _assign_unused(
        videos, pool, must_use=must_use, usage=usage, library=library, rng=batch_rng, pads=pads, fitted=fitted
    )

    used_refs = {str(s["clip_key"] or s["clip_id"]) for v in videos for s in v["shots"]}
    clip_specs: list[dict[str, Any]] = []
    if not library:
        renumber: dict[str, str] = {}
        for clip in pool:
            if clip.ref in used_refs and clip.spec is not None:
                renumber[clip.ref] = f"c{len(renumber) + 1:02d}"
                clip_specs.append({**clip.spec, "key": renumber[clip.ref]})
        for video in videos:
            for shot in video["shots"]:
                shot["clip_key"] = renumber[shot["clip_key"]]

    # variety_score is cross-video; in-video repeats are reported separately below.
    variety = _variety_score(videos)
    variety_warning = None
    if variety < VARIETY_WARNING_THRESHOLD:
        variety_warning = _variety_warning(
            library=library,
            pool_size=len(pool),
            pool_target=pool_target,
            max_new=int(max_new) if max_new is not None else None,
            reuse=reuse,
            shots_total=beat_total,
            n_scenes=len(scenes),
        )
    new_clips = sum(1 for spec in clip_specs if spec["existing_clip_id"] is None)

    repeat_total = overlap_total = adjacent_total = scene_total = videos_with_repeats = 0
    for index, video in enumerate(videos):
        shots = video["shots"]
        repeats, overlaps, adjacent, scene_repeats = _repeat_counts(shots, pads[index], library=library)
        video["fit"] = {
            "beat_shots": beat_counts[index],
            "shots": len(shots),
            "fitted": fitted[index],
            # Rounded as the notes print them, so every surface shows the same figure.
            "min_speed": round(min(s["speed"] for s in shots), 2),
            "max_shot_sec": round(max(s["duration"] for s in shots), 1),
            "repeat_shots": repeats,
            "overlap_repeat_shots": overlaps,
            "adjacent_repeat_shots": adjacent,
            "scene_repeat_shots": scene_repeats,
        }
        repeat_total += repeats
        overlap_total += overlaps
        adjacent_total += adjacent
        scene_total += scene_repeats
        videos_with_repeats += repeats > 0

    clean = [v["fit"] for i, v in enumerate(videos) if fitted[i] and not v["fit"]["repeat_shots"]]
    if clean:
        pace = _pace_ja(max(f["max_shot_sec"] for f in clean), min(f["min_speed"] for f in clean))
        if count == 1:
            warnings.append(
                f"素材が{len(pool)}本のため、同じ映像を繰り返さないよう、"
                f"ショットを{clean[0]['beat_shots']}→{clean[0]['shots']}にまとめました（{pace}）"
            )
        else:
            warnings.append(
                f"素材が{len(pool)}本のため、同じ映像を繰り返さないよう、"
                f"{len(clean)}本の動画でショットを長めにまとめました（{pace}）"
            )

    repeat_warning = None
    if repeat_total and library:
        repeat_warning = (
            f"アップロード素材が足りないため、同じ映像が{repeat_total}ショット再登場します。"
            "素材を追加でアップロードすると解消します"
        )
    elif repeat_total:
        where = "動画内で" if count == 1 else f"{videos_with_repeats}本の動画で"
        total = f"{repeat_total}" if count == 1 else f"計{repeat_total}"
        # Only what holds for every repeat: a pool of one shows a clip back to back, and a fitted
        # last-resort shot plays (almost) the whole clip, so it shows the same footage again.
        manner = ("" if adjacent_total else "連続はせず、") + ("" if overlap_total else "別の区間・") + "寄りの構図"
        repeat_warning = f"素材が足りないため、{where}同じ素材が{total}ショット再登場します（{manner}で使います）。"
        # Clips one video needs when every shot is as long as a clip allows (≤ 8 s at ≥ 0.75x).
        usable = clip_duration - 2 * GENERATED_EDGE_MARGIN
        limit_ms = min(_MAX_FIT_MS, int((usable / FIT_MIN_SPEED - (XFADE_PAD_SEC + FRAME_SLACK_SEC)) * 1000))
        need = math.ceil(duration_ms / max(_MIN_MS, limit_ms))
        if max_new is None:
            repeat_warning += "動画を短くすると解消します"
        elif int(max_new) > 0:
            cached_in_pool = sum(1 for clip in pool if clip.clip_id is not None)
            repeat_warning += (
                f"新規生成の上限を{max(int(max_new) + 1, need - cached_in_pool)}本以上に引き上げると解消します"
            )
        else:
            repeat_warning += (
                f"生成済みの素材だけでは足りません。新しい素材を{max(1, need - len(pool))}本以上追加すると解消します"
            )

    all_shots = [s for v in videos for s in v["shots"]]
    stats = {
        "videos": count,
        "shots_total": shots_total,
        "unique_clips": len(used_refs),
        "new_clips": new_clips,
        "reused_clips": len(used_refs) if library else len(clip_specs) - new_clips,
        "variety_score": variety,
        "variety_warning": variety_warning,
        "pool_target": pool_target,
        "clip_duration": clip_duration,
        "shots_per_video": [len(v["shots"]) for v in videos],
        # additive: fitting to the pool and in-video repeats (shots_total / shots_per_video are post-fit)
        "beat_shots_per_video": beat_counts,
        "fitted_videos": sum(fitted),
        "repeat_shots": repeat_total,
        "overlap_repeat_shots": overlap_total,
        "adjacent_repeat_shots": adjacent_total,
        "scene_repeat_shots": scene_total,
        "videos_with_repeats": videos_with_repeats,
        "max_shot_sec": round(max(s["duration"] for s in all_shots), 1),
        "min_speed": round(min(s["speed"] for s in all_shots), 2),
        "repeat_warning": repeat_warning,
        "pool_fit_floor": fit_floor,
    }
    return {"clip_specs": clip_specs, "videos": videos, "stats": stats, "warnings": list(dict.fromkeys(warnings))}
