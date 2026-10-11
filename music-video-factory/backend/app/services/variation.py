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
* **Variation** — videos differ by start time, clip order, in-points,
  horizontal flips, Ken Burns zoom/pan, speed, colour grade and transition;
  clip usage is balanced so every newly generated clip is used.

Everything is deterministic for a given ``seed``; the budget is not an input
(the estimate, not the plan, decides whether a batch is affordable).
"""

from __future__ import annotations

import bisect
import hashlib
import itertools
import math
import random
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.services.errors import ServiceError
from app.services.presets import COLOR_GRADES, LYRIC_STYLES, get_preset
from app.services.prompting import sanitize_prompt

__all__ = [
    "ASPECT_RATIO",
    "MAX_POOL",
    "MAX_SHOT_SEC",
    "MIN_SHOT_SEC",
    "SRC_IN_BUCKET_SEC",
    "VARIETY_WARNING_THRESHOLD",
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
VARIETY_WARNING_JA = (
    "素材の再利用率が高いため、動画間の映像バリエーションが限定的です。"
    "再利用設定を「バランス」か「バリエーション重視」にすると改善します"
)
MIN_NATURAL_SPEED = 0.6  # slower than this looks like obvious slow motion
GENERATED_EDGE_MARGIN = 0.1  # generated clips often have weak first/last frames
UPLOAD_EDGE_MARGIN = 0.05

_MIN_MS = int(MIN_SHOT_SEC * 1000)
_MAX_MS = int(MAX_SHOT_SEC * 1000)
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


def pool_target_size(reuse: str, shots_total: int, shots_per_video: int, n_scenes: int) -> int:
    """Clip pool size K for a reuse policy (before ``max_new_clips`` and cache reuse)."""
    divisor = _REUSE_DIVISOR.get(reuse, _REUSE_DIVISOR["balanced"])
    target = math.ceil(shots_total / divisor)
    target = max(target, min(shots_per_video, max(1, n_scenes)))
    return max(1, min(target, MAX_POOL, shots_total))


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
    return _Song(duration, beats, downbeats, bar, beat, sections, lyrics)


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
        raw: list[float | None] = [segment_start, *chorus_starts(), best_suggested()]
        raw += [_num(s.get("start")) for s in analysis.get("suggested_segments") or []]
        raw += [_num(h.get("start")) for h in analysis.get("highlights") or []]
        raw += [s.start for s in song.sections if s.label in ("verse", "chorus", "bridge")]
        candidates: list[float] = []
        for value in raw:
            if value is None:
                continue
            start = clamp(song.snap_to_downbeat(value))
            if all(abs(start - c) >= 1.0 for c in candidates):
                candidates.append(start)
        if len(candidates) < min(count, 4) and max_start >= 2.0:
            # Few analysis landmarks: spread extra starts over the downbeats that fit.
            usable = [d for d in song.downbeats if d <= max_start] or [0.0]
            for k in range(min(count, 8)):
                start = clamp(usable[round(k * (len(usable) - 1) / max(1, min(count, 8) - 1))])
                if all(abs(start - c) >= 1.0 for c in candidates):
                    candidates.append(start)
        candidates = candidates or [0.0]
        return [candidates[i % len(candidates)] for i in range(count)]

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


@dataclass
class _Usage:
    global_uses: dict[str, int] = field(default_factory=dict)
    positions: dict[tuple[str, int], int] = field(default_factory=dict)
    combos: set[tuple[str, int, bool]] = field(default_factory=set)

    def copy(self) -> _Usage:
        return _Usage(dict(self.global_uses), dict(self.positions), set(self.combos))


def _speed_and_in(
    clip: _PoolClip, shot_sec: float, energy: str, rng: random.Random, usage: _Usage
) -> tuple[float, float, int]:
    """(speed, src_in, position index) with ``src_in + shot_sec * speed <= clip.duration``."""
    desired = {"high": (1.0,), "mid": (1.0, 1.0, 0.9), "low": (1.0, 0.9, 0.85, 0.8)}[energy]
    speed = rng.choice(desired)
    margin = clip.margin
    if (clip.duration - 2 * margin) / shot_sec < speed:
        speed = (clip.duration - 2 * margin) / shot_sec
        if speed < MIN_NATURAL_SPEED:
            margin = 0.0
            speed = min(rng.choice(desired), clip.duration / shot_sec)
    speed = max(0.01, _floor3(speed))
    slack = max(0.0, clip.duration - shot_sec * speed)
    low = min(margin, slack / 2)
    high = max(low, slack - low)
    n_positions = max(1, int((high - low) // 0.5) + 1)
    positions = [low + (high - low) * k / max(1, n_positions - 1) for k in range(n_positions)]
    best = min(range(n_positions), key=lambda k: (usage.positions.get((clip.ref, k), 0), rng.random()))
    src_in = _floor3(min(positions[best], max(0.0, clip.duration - shot_sec * speed)))
    while src_in > 0 and src_in + shot_sec * speed > clip.duration + 1e-9:
        src_in = max(0.0, round(src_in - 0.001, 3))
    return speed, src_in, best


def _ken_burns(energy: str, rng: random.Random) -> tuple[float, float, str]:
    delta = rng.uniform(0.06, 0.12) if energy == "high" else rng.uniform(0.03, 0.07)
    pan = "center" if rng.random() < (0.3 if energy == "high" else 0.45) else rng.choice(_PANS)
    base = 1.0 if pan == "center" else 1.06  # a pan needs headroom at both ends
    if rng.random() < 0.65:
        zoom_from, zoom_to = base, base + delta
    else:
        zoom_from, zoom_to = base + delta, base
    return round(zoom_from, 3), round(zoom_to, 3), pan


@dataclass
class _ShotContext:
    shot_sec: float
    section: _Section | None
    lyric_ids: set[str]
    previous: str | None
    any_feasible: bool


def _clip_cost(
    clip: _PoolClip, ctx: _ShotContext, *, pool_size: int, in_video: Mapping[str, int], usage: _Usage
) -> float:
    """Lower is better: avoid repeats (adjacent, in-video, batch-wide), prefer matching section/lyric."""
    value = 1000.0 if clip.ref == ctx.previous and pool_size > 1 else 0.0
    value += 50.0 * in_video.get(clip.ref, 0) + 6.0 * usage.global_uses.get(clip.ref, 0)
    if ctx.section is not None and clip.section is not None and clip.section != ctx.section.label:
        value += 4.0
    if clip.lyric_ref is not None and clip.lyric_ref in ctx.lyric_ids:
        value -= 3.0
    if ctx.any_feasible and clip.duration < ctx.shot_sec * MIN_NATURAL_SPEED:
        value += 200.0
    # Prefer clips long enough to play the shot at (near) normal speed.
    return value + 10.0 * max(0.0, 1.0 - (clip.duration - 2 * clip.margin) / ctx.shot_sec)


def _plan_video_shots(
    *,
    song: _Song,
    start: float,
    durations_ms: Sequence[int],
    pool: Sequence[_PoolClip],
    usage: _Usage,
    rng: random.Random,
    library: bool,
) -> list[dict[str, Any]]:
    shots: list[dict[str, Any]] = []
    in_video: dict[str, int] = {}
    previous: str | None = None
    t_ms = 0
    for duration_ms in durations_ms:
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
        )
        costs = [
            _clip_cost(c, ctx, pool_size=len(pool), in_video=in_video, usage=usage) + rng.random() * 2.0 for c in pool
        ]
        clip = pool[min(range(len(pool)), key=costs.__getitem__)]
        speed, src_in, position = _speed_and_in(clip, shot_sec, energy, rng, usage)
        bucket = int(src_in // SRC_IN_BUCKET_SEC)
        plain, flipped = (clip.ref, bucket, False), (clip.ref, bucket, True)
        if plain in usage.combos and flipped not in usage.combos:
            hflip = True
        elif plain not in usage.combos:
            hflip = rng.random() < (0.1 if library else 0.2)
        else:
            hflip = rng.random() < 0.5
        zoom_from, zoom_to, pan = _ken_burns(energy, rng)
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
        usage.global_uses[clip.ref] = usage.global_uses.get(clip.ref, 0) + 1
        usage.positions[(clip.ref, position)] = usage.positions.get((clip.ref, position), 0) + 1
        usage.combos.add((clip.ref, bucket, hflip))
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
) -> None:
    """Swap clips that ended up unused (paid generations) into shots of over-used clips."""

    def ref_of(shot: Mapping[str, Any]) -> str:
        return str(shot["clip_key"] or shot["clip_id"])

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
        speed, src_in, position = _speed_and_in(clip, shot["duration"], "mid", rng, usage)
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
        usage.combos.add((clip.ref, int(src_in // SRC_IN_BUCKET_SEC), False))


def _variety_score(videos: Sequence[Mapping[str, Any]]) -> float:
    combos: set[tuple[str, int, bool]] = set()
    total = 0
    for video in videos:
        for shot in video["shots"]:
            total += 1
            combos.add(
                (
                    str(shot["clip_key"] or shot["clip_id"]),
                    int(shot["src_in"] // SRC_IN_BUCKET_SEC),
                    bool(shot["hflip"]),
                )
            )
    return round(len(combos) / total, 3) if total else 0.0


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
    shots_total = sum(len(cuts) for cuts in cut_lists)
    shots_per_video = max(len(cuts) for cuts in cut_lists)

    ready = [c for c in existing_clips if c.get("id") is not None and c.get("status", "ready") == "ready"]
    scenes = [s for s in (storyboard or {}).get("scenes") or [] if isinstance(s, Mapping) and s.get("prompt_en")]
    pool_target = 0
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
        max_new = settings.get("max_new_clips")
        pool_target = pool_target_size(reuse, shots_total, shots_per_video, len(scenes))
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
        if max_new is not None and len(pool) < pool_target:
            warnings.append(f"新規生成の上限（{int(max_new)}本）により、素材数を{len(pool)}本に抑えました")
        for index, clip in enumerate(pool):
            clip.ref = f"c{index + 1:02d}"

    usage = _Usage()
    videos: list[dict[str, Any]] = []
    signatures: set[tuple[Any, ...]] = set()
    grades = [g for g in preset["color_grades"] if g in COLOR_GRADES] or ["natural"]
    grade_offset = batch_rng.randrange(len(grades))
    lyric_style = str(settings.get("lyric_style") or "simple")
    style_cycle = [s for s in preset["lyric_styles"] if s in LYRIC_STYLES] or ["simple"]
    color_grade = str(settings.get("color_grade") or "auto")
    text_style = {k: v for k, v in dict(settings.get("text_style") or {}).items() if v is not None}

    for index in range(count):
        shots: list[dict[str, Any]] = []
        for attempt in range(6):
            rng = video_rngs[index] if attempt == 0 else random.Random(_stable_int("video", seed, index, attempt))
            trial_usage = usage.copy()
            shots = _plan_video_shots(
                song=song, start=starts[index], durations_ms=cut_lists[index], pool=pool, usage=trial_usage,
                rng=rng, library=library,
            )  # fmt: skip
            if _shot_signature(shots) not in signatures or attempt == 5:
                usage = trial_usage
                break
        signatures.add(_shot_signature(shots))
        video_rng = random.Random(_stable_int("video-look", seed, index))
        soft = preset["id"] in _SOFT_TRANSITION_PRESETS
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
                "transition": "crossfade" if video_rng.random() < (0.5 if soft else 0.15) else "cut",
            }
        )

    must_use = {clip.ref for clip in pool if not library and clip.clip_id is None}
    _assign_unused(videos, pool, must_use=must_use, usage=usage, library=library, rng=batch_rng)

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

    variety = _variety_score(videos)
    new_clips = sum(1 for spec in clip_specs if spec["existing_clip_id"] is None)
    stats = {
        "videos": count,
        "shots_total": shots_total,
        "unique_clips": len(used_refs),
        "new_clips": new_clips,
        "reused_clips": len(used_refs) if library else len(clip_specs) - new_clips,
        "variety_score": variety,
        "variety_warning": VARIETY_WARNING_JA if variety < VARIETY_WARNING_THRESHOLD else None,
        "pool_target": pool_target,
        "clip_duration": clip_duration,
        "shots_per_video": [len(v["shots"]) for v in videos],
    }
    return {"clip_specs": clip_specs, "videos": videos, "stats": stats, "warnings": list(dict.fromkeys(warnings))}
