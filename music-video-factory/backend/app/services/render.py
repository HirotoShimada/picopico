"""FFmpeg compositor: shots + song + lyrics → 9:16 H.264/AAC MP4 (ARCHITECTURE.md §6.6).

One FFmpeg invocation renders the whole video:

* every shot is its own input (``-ss``/``-t`` input seeking, ``-stream_loop``
  when a clip is too short) and filter chain: trim → speed → 30 fps CFR →
  whole-pixel ``crop`` to the part of the frame the Ken Burns move ever shows
  → one fixed scale (square pixels, at most the output size) → edge-repeating
  pad → ``perspective`` with per-frame corner expressions (``eval=frame``,
  cubic interpolation): the zoom/pan view is resampled at sub-pixel positions,
  so the motion glides instead of stepping 1 px / 2 px per frame (see
  :class:`_ShotGeometry`) → fixed scale to the output size and planar RGB →
  optional ``hflip`` → ``setsar=1``. Grade and subtitles see true colours; the
  frame count of each shot is exact (``tpad`` + ``trim``);
* shots are joined with ``concat`` (cut) or a chain of ``xfade`` crossfades
  centred on the planned cut points, with shot lengths compensated so the
  total frame count equals ``duration × fps``;
* colour grade (``presets.COLOR_GRADES`` when available, else a local
  fallback with the same ids), subtle vignette + luma grain, lyrics burned in
  with libass (``subtitles`` filter, ASS from :mod:`app.services.subtitles`);
* the song is decoded once from the original file at ``audio_start`` and
  encoded once to AAC-LC (afade in/out, padded/trimmed to the exact length).
  When that segment's true peak exceeds ``TRUE_PEAK_CEILING_DBTP`` (-1 dBTP,
  measured with ``ebur128=peak=true`` on the exact chain), a pure linear
  ``volume`` gain brings it down to the ceiling, so the AAC encode does not
  clip; otherwise the audio chain has no gain stage at all.

Output: H.264 High yuv420p (BT.709), 30 fps CFR, GOP 2 s, ``+faststart``;
thumbnail JPEG at half resolution (540×960) from ~1 s.
"""

from __future__ import annotations

import functools
import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services.errors import RenderError
from app.services.subtitles import build_ass

logger = logging.getLogger(__name__)

__all__ = [
    "CROSSFADE_SEC",
    "DEFAULT_PRESET",
    "FALLBACK_COLOR_GRADES",
    "TRUE_PEAK_CEILING_DBTP",
    "ProgressCallback",
    "RenderResult",
    "RenderShot",
    "RenderSpec",
    "color_grade_filter",
    "escape_filter_value",
    "render_video",
    "split_filter_chain",
]

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

# libx264 preset used when RenderSpec.preset is left at its default. Measured in
# this container (see scripts/render_sample.py): "veryfast" renders a 15 s
# 1080×1920 video in well under a minute on 2 cores at CRF 18; slower presets
# mostly buy smaller files, not visibly better frames, at this CRF.
DEFAULT_PRESET = "veryfast"
CROSSFADE_SEC = 0.3
# Loud masters whose segment's true peak exceeds this are turned down to it (linear gain)
# before the AAC encode: lossy coding adds overshoot, and overs clip on playback.
TRUE_PEAK_CEILING_DBTP = -1.0
MAX_RATE = "25M"  # VBV cap: keeps grainy footage from exploding the bitrate
BUF_SIZE = "50M"
_ALLOWED_PRESETS = frozenset(
    {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"}
)
_SPEED_LIMITS = (0.25, 4.0)
_MIN_SLOWDOWN = 0.6  # slow a too-short clip down to at most 60 % speed, else loop it
_LOG_TAIL_LINES = 40
_KB_PIXFMT = "yuv420p"
_KB_INTERP = "cubic"
_KB_EDGE = 4  # px of repeated edge around the scaled box (≥ cubic kernel radius)

ProgressCallback = Callable[[float, str], None]


# --------------------------------------------------------------------------- data


@dataclass
class RenderShot:
    clip_path: str
    src_in: float
    duration: float
    hflip: bool = False
    zoom_from: float = 1.0
    zoom_to: float = 1.06
    pan: str = "center"  # left | right | up | down | center
    speed: float = 1.0


@dataclass
class RenderSpec:
    shots: list[RenderShot]
    audio_path: str
    audio_start: float
    duration: float
    lyric_lines: list[dict[str, Any]]
    lyric_style: str
    text_style: dict[str, Any]
    color_grade: str
    output_path: str
    work_dir: str
    beats: list[float] | None = None
    width: int = 1080
    height: int = 1920
    fps: int = 30
    crf: int = 18
    preset: str = DEFAULT_PRESET
    audio_bitrate: str = "256k"
    audio_fade_in: float = 0.15
    audio_fade_out: float = 0.8
    transition: str = "cut"  # cut | crossfade


@dataclass
class RenderResult:
    output_path: str
    thumbnail_path: str
    duration: float
    size_bytes: int
    log_tail: str
    timings: dict[str, float] = field(default_factory=dict)
    audio_gain_db: float = 0.0  # < 0 when a hot master was turned down to TRUE_PEAK_CEILING_DBTP


# --------------------------------------------------------------------------- colour grades

def _saturation_mixer(saturation: float) -> str:
    """``colorchannelmixer`` that scales chroma around Rec.709 luma (exact, cheap 3×3 matrix)."""
    kr, kg, kb = 0.2126, 0.7152, 0.0722
    inv = 1.0 - saturation

    def c(value: float) -> str:
        return f"{value:.4f}".rstrip("0").rstrip(".")

    return (
        f"colorchannelmixer=rr={c(kr + (1 - kr) * saturation)}:rg={c(kg * inv)}:rb={c(kb * inv)}"
        f":gr={c(kr * inv)}:gg={c(kg + (1 - kg) * saturation)}:gb={c(kb * inv)}"
        f":br={c(kr * inv)}:bg={c(kg * inv)}:bb={c(kb + (1 - kb) * saturation)}"
    )


# Same ids as presets.COLOR_GRADES; used when that module is unavailable or a
# preset filter fails validation. Built from cheap point filters only (a 3×3
# channel mix for saturation + per-channel tone curves: ~3 ms per 1080×1920 frame).
FALLBACK_COLOR_GRADES: dict[str, dict[str, str]] = {
    "natural": {
        "name": "ナチュラル",
        "ffmpeg_filter": f"{_saturation_mixer(1.05)},curves=m='0/0 0.25/0.235 0.75/0.765 1/1'",
    },
    "teal_orange": {
        "name": "ティール＆オレンジ",
        "ffmpeg_filter": f"{_saturation_mixer(1.12)},curves="
        "r='0/0 0.25/0.2 0.5/0.5 0.75/0.8 1/1':"
        "g='0/0 0.25/0.235 0.5/0.5 0.75/0.765 1/1':"
        "b='0/0.04 0.25/0.3 0.5/0.5 0.75/0.69 1/0.94':"
        "m='0/0 0.25/0.23 0.75/0.77 1/1'",
    },
    "warm_film": {
        "name": "ウォームフィルム",
        "ffmpeg_filter": f"{_saturation_mixer(0.92)},curves="
        "r='0/0.05 0.5/0.55 1/0.98':g='0/0.04 0.5/0.51 1/0.96':b='0/0.06 0.5/0.45 1/0.88':"
        "m='0/0 0.25/0.24 0.75/0.77 1/1'",
    },
    "cool_night": {
        "name": "クールナイト",
        "ffmpeg_filter": f"{_saturation_mixer(0.85)},curves="
        "r='0/0 0.5/0.44 1/0.93':g='0/0.01 0.5/0.49 1/0.97':b='0/0.05 0.5/0.57 1/1':"
        "m='0/0 0.25/0.2 0.5/0.45 0.75/0.73 1/0.97'",
    },
    "bleach_bypass": {
        "name": "ブリーチバイパス",
        "ffmpeg_filter": f"{_saturation_mixer(0.5)},curves=m='0/0 0.2/0.13 0.5/0.5 0.8/0.87 1/1'",
    },
    "faded_pastel": {
        "name": "フェードパステル",
        "ffmpeg_filter": f"{_saturation_mixer(0.78)},curves="
        "r='0/0.02 1/1':b='0/0.05 1/0.97':m='0/0.1 0.5/0.56 1/0.94'",
    },
    "mono_noir": {
        "name": "モノクロノワール",
        "ffmpeg_filter": f"{_saturation_mixer(0.0)},curves=m='0/0 0.25/0.16 0.5/0.47 0.75/0.83 1/1'",
    },
    "vivid": {
        "name": "ビビッド",
        "ffmpeg_filter": f"{_saturation_mixer(1.3)},curves=m='0/0 0.25/0.23 0.75/0.78 1/1'",
    },
}

# Finishing per grade: (vignette lens angle in radians or None, luma grain strength
# for the ``noise`` filter at half resolution; 0 = none; grain std ≈ 0.43 × strength
# code values). Subtle on purpose: temporal grain is expensive for the encoder.
_GRADE_FINISH: dict[str, tuple[float | None, int]] = {
    "natural": (math.pi / 8, 0),
    "teal_orange": (math.pi / 6.5, 0),
    "warm_film": (math.pi / 6, 2),
    "cool_night": (math.pi / 6, 0),
    "bleach_bypass": (math.pi / 6, 3),
    "faded_pastel": (math.pi / 8, 2),
    "mono_noir": (math.pi / 5.5, 3),
    "vivid": (None, 0),
}
_DEFAULT_FINISH = (math.pi / 8, 0)
_GRAIN_SEED = 20251011

# Filters that map each pixel's colour independently of position and time:
# consecutive runs of them are baked into one 3D LUT (one cheap SIMD lookup per
# pixel instead of several float filters and RGB<->YUV round trips).
_POINT_FILTERS = frozenset(
    {
        "eq", "colorbalance", "colortemperature", "curves", "hue", "colorchannelmixer", "lutrgb",
        "lutyuv", "lut", "colorlevels", "colorcontrast", "colorcorrect", "vibrance", "huesaturation",
        "selectivecolor", "exposure", "monochrome", "negate", "lut1d", "lut3d", "format", "null",
    }
)  # fmt: skip
_HALD_LEVEL = 6  # 36³ LUT; tetrahedral interpolation keeps errors below ~1/255 on average
# Approximate cost (ms per 1080×1920 frame, measured on 2 cores) of applying a
# point filter directly; a run costing more than the LUT lookup gets baked.
_DIRECT_COST_MS: dict[str, float] = {
    "curves": 1.0, "lutrgb": 0.6, "lut": 1.0, "colorchannelmixer": 2.0, "colorlevels": 2.0,
    "eq": 4.5, "lutyuv": 4.5, "hue": 6.0, "colortemperature": 9.0, "colorbalance": 27.0,
    "format": 0.0, "null": 0.0,
}  # fmt: skip
_UNKNOWN_POINT_COST_MS = 15.0
_LUT_COST_MS = 15.0


@functools.lru_cache(maxsize=128)
def _filter_is_valid(filter_str: str) -> bool:
    """True when ``filter_str`` builds and runs in a tiny RGB test graph."""
    cmd = [
        FFMPEG, "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=32x32:d=0.1:r=10",
        "-vf", f"format=gbrp,{filter_str},format=gbrp", "-f", "null", "-",
    ]  # fmt: skip
    try:
        return subprocess.run(cmd, capture_output=True, timeout=30, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _preset_grade_filter(grade_id: str) -> str | None:
    try:
        from app.services.presets import COLOR_GRADES  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - presets is optional for rendering
        return None
    grade = COLOR_GRADES.get(grade_id) if isinstance(COLOR_GRADES, dict) else None
    if grade is None:
        return None
    value = grade.get("ffmpeg_filter") if isinstance(grade, dict) else getattr(grade, "ffmpeg_filter", None)
    return value.strip() if isinstance(value, str) and value.strip() else None


def color_grade_filter(grade_id: str) -> str:
    """FFmpeg filter string for ``grade_id`` (unknown/``auto`` → natural)."""
    preset = _preset_grade_filter(grade_id)
    if preset:
        if _filter_is_valid(preset):
            return preset
        logger.warning("color grade %r from presets is not a valid FFmpeg filter; using fallback", grade_id)
    grade = FALLBACK_COLOR_GRADES.get(grade_id) or FALLBACK_COLOR_GRADES["natural"]
    return grade["ffmpeg_filter"]


def split_filter_chain(chain: str) -> list[str]:
    """Split a filter chain at top-level commas (quotes and backslash escapes respected)."""
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    index = 0
    while index < len(chain):
        ch = chain[index]
        if ch == "\\" and index + 1 < len(chain):
            current.append(chain[index : index + 2])
            index += 2
            continue
        if ch == "'":
            quoted = not quoted
        if ch == "," and not quoted:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
        index += 1
    parts.append("".join(current).strip())
    return [part for part in parts if part]


def _filter_name(element: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9_]+)", element)
    return match.group(1).lower() if match else ""


def _bake_lut(chain: str, out_path: Path) -> bool:
    """Sample ``chain`` on a Hald CLUT and write it as a ``.cube`` 3D LUT."""
    import numpy as np

    size = _HALD_LEVEL**2
    cmd = [
        FFMPEG, "-nostdin", "-v", "error", "-f", "lavfi", "-i", f"haldclutsrc=level={_HALD_LEVEL}",
        "-vf", f"format=gbrp,{chain},format=gbrp,format=rgb48le", "-frames:v", "1", "-f", "rawvideo", "-",
    ]  # fmt: skip
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    expected = size**3 * 3 * 2
    if proc.returncode != 0 or len(proc.stdout) != expected:
        return False
    table = np.frombuffer(proc.stdout, dtype="<u2").reshape(-1, 3).astype(np.float64) / 65535.0
    tmp = out_path.with_suffix(".tmp")
    with tmp.open("w", encoding="ascii") as fh:
        fh.write(f'TITLE "mvf grade"\nLUT_3D_SIZE {size}\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n')
        np.savetxt(fh, table, fmt="%.6f")
    os.replace(tmp, out_path)
    return True


@dataclass(frozen=True)
class _PostStep:
    """One stage after the shots are joined: a plain filter chain, a vignette mask or grain."""

    kind: str  # "filters" | "vignette" | "grain"
    value: str


def _grade_steps(grade_id: str, work_dir: Path) -> list[_PostStep]:
    """Colour grade + finishing (vignette, grain) as an ordered list of steps."""
    chain = color_grade_filter(grade_id)
    elements = split_filter_chain(chain)
    vignette_angle, grain = _GRADE_FINISH.get(grade_id, _DEFAULT_FINISH)
    names = {_filter_name(e) for e in elements}
    steps: list[_PostStep] = []
    run: list[str] = []

    def flush() -> None:
        if not run:
            return
        lut_path = work_dir / f"grade_{len(steps)}.cube"
        joined = ",".join(run)
        cost = sum(_DIRECT_COST_MS.get(_filter_name(e), _UNKNOWN_POINT_COST_MS) for e in run)
        if cost <= _LUT_COST_MS:
            steps.append(_PostStep("filters", f"{joined},format=gbrp"))
        elif _bake_lut(joined, lut_path):
            steps.append(
                _PostStep("filters", f"format=gbrpf32le,lut3d=file={escape_filter_value(str(lut_path))}"
                                     ":interp=tetrahedral,format=gbrp")
            )  # fmt: skip
        else:
            logger.warning("could not bake colour grade into a LUT; applying filters directly")
            steps.append(_PostStep("filters", f"{joined},format=gbrp"))
        run.clear()

    for element in elements:
        name = _filter_name(element)
        if name in _POINT_FILTERS:
            run.append(element)
            continue
        flush()
        if name == "vignette":
            steps.append(_PostStep("vignette", element))
        else:
            steps.append(_PostStep("filters", f"{element},format=gbrp"))
    flush()
    if vignette_angle and "vignette" not in names:
        steps.append(_PostStep("vignette", f"vignette=angle={vignette_angle:.4f}"))
    if grain and "noise" not in names:
        steps.append(_PostStep("grain", str(grain)))
    return steps


# --------------------------------------------------------------------------- escaping & formatting


def escape_filter_value(value: str) -> str:
    """Escape ``value`` for use as a filter option inside a filtergraph string.

    Two levels (FFmpeg "filtergraph escaping"): option level (``\\ ' :``)
    then graph level (``\\ ' [ ] , ;``).
    """
    level1 = re.sub(r"([\\':])", r"\\\1", value)
    return re.sub(r"([\\'\[\],;])", r"\\\1", level1)


def _f(value: float, digits: int = 6) -> str:
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


# --------------------------------------------------------------------------- probing


@dataclass(frozen=True)
class _ClipInfo:
    path: str
    duration: float
    width: float  # display size: rotation and sample aspect ratio applied
    height: float
    color_matrix: str  # swscale in_color_matrix
    pixel_width: int = 0  # decoded frame size after autorotation (0 = same as the display size)
    pixel_height: int = 0

    @property
    def frame_size(self) -> tuple[int, int]:
        """Size of the frames the filter graph receives (autorotated, sample aspect not applied)."""
        if self.pixel_width > 0 and self.pixel_height > 0:
            return self.pixel_width, self.pixel_height
        return max(1, round(self.width)), max(1, round(self.height))


_MATRIX_BY_COLORSPACE = {
    "bt709": "bt709",
    "smpte170m": "bt601",
    "bt470bg": "bt601",
    "bt2020nc": "bt2020",
    "bt2020c": "bt2020",
    "smpte240m": "smpte240m",
    "fcc": "fcc",
}


def _ffprobe_json(path: str) -> dict[str, Any]:
    cmd = [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    except FileNotFoundError as exc:
        raise RenderError("ffprobe が見つかりません。サーバーに FFmpeg をインストールしてください。") from exc
    except subprocess.TimeoutExpired as exc:
        raise RenderError(f"メディアの解析がタイムアウトしました: {Path(path).name}") from exc
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace")[-2000:]
        raise RenderError(f"メディアファイルを読み込めませんでした: {Path(path).name}", stderr_tail=tail)
    try:
        return json.loads(proc.stdout or b"{}")
    except json.JSONDecodeError as exc:
        raise RenderError(f"メディアの解析結果を読み取れませんでした: {Path(path).name}") from exc


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _ratio(value: Any) -> float | None:
    if not isinstance(value, str) or ":" not in value:
        return None
    num, _, den = value.partition(":")
    n, d = _float(num), _float(den)
    if not n or not d or n <= 0 or d <= 0:
        return None
    return n / d


def _rotation(stream: dict[str, Any]) -> int:
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            rotation = _float(side.get("rotation"))
            if rotation is not None:
                return round(rotation) % 360
    rotation = _float((stream.get("tags") or {}).get("rotate"))
    return round(rotation) % 360 if rotation is not None else 0


def _probe_clip(path: str) -> _ClipInfo:
    data = _ffprobe_json(path)
    streams = [
        s
        for s in data.get("streams") or []
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
    ]
    if not streams:
        raise RenderError(f"素材ファイルに映像がありません: {Path(path).name}")
    stream = streams[0]
    coded_w, coded_h = _float(stream.get("width")), _float(stream.get("height"))
    if not coded_w or not coded_h:
        raise RenderError(f"素材動画の解像度を判定できません: {Path(path).name}")
    sar = _ratio(stream.get("sample_aspect_ratio")) or 1.0
    width, height = coded_w * sar, coded_h
    pixel_w, pixel_h = int(coded_w), int(coded_h)
    if _rotation(stream) % 180 == 90:
        width, height = height, width
        pixel_w, pixel_h = pixel_h, pixel_w
    duration = _float(stream.get("duration")) or _float((data.get("format") or {}).get("duration"))
    if not duration or duration <= 0:
        raise RenderError(f"素材動画の長さを判定できません: {Path(path).name}")
    matrix = _MATRIX_BY_COLORSPACE.get(str(stream.get("color_space") or ""))
    if matrix is None:
        matrix = "bt709" if max(coded_w, coded_h) >= 720 else "bt601"
    return _ClipInfo(
        path=path, duration=duration, width=width, height=height, color_matrix=matrix,
        pixel_width=pixel_w, pixel_height=pixel_h,
    )  # fmt: skip


@dataclass(frozen=True)
class _AudioInfo:
    duration: float | None
    sample_rate: int


def _probe_audio(path: str) -> _AudioInfo:
    data = _ffprobe_json(path)
    streams = [s for s in data.get("streams") or [] if s.get("codec_type") == "audio"]
    if not streams:
        raise RenderError(f"音声ファイルに音声ストリームがありません: {Path(path).name}")
    stream = streams[0]
    rate = int(_float(stream.get("sample_rate")) or 0)
    duration = _float((data.get("format") or {}).get("duration")) or _float(stream.get("duration"))
    return _AudioInfo(duration=duration, sample_rate=rate if rate in (44100, 48000) else 48000)


# --------------------------------------------------------------------------- shot planning


@dataclass(frozen=True)
class _ShotPlan:
    shot: RenderShot
    clip: _ClipInfo
    frames: int  # rendered frames, including crossfade overlap
    src_in: float
    speed: float
    loop: bool
    zoom_from: float
    zoom_to: float


def _frame_allocation(durations: Sequence[float], total_frames: int) -> list[int]:
    """Frames per shot from cumulative boundaries (sums exactly to ``total_frames``)."""
    total = sum(durations)
    frames: list[int] = []
    acc = 0.0
    prev = 0
    for duration in durations:
        acc += duration
        boundary = round(acc / total * total_frames)
        frames.append(boundary - prev)
        prev = boundary
    return frames


def _pan_travel(pan: str, seconds: float, width: int, height: int) -> tuple[int, int, float, float]:
    """(dir_x, dir_y, travel_x px, travel_y px) for a pan over ``seconds``."""
    dir_x = {"left": -1, "right": 1}.get(pan, 0)
    dir_y = {"up": -1, "down": 1}.get(pan, 0)
    travel_x = min(0.035 * width * seconds, 0.12 * width) if dir_x else 0.0
    travel_y = min(0.025 * height * seconds, 0.08 * height) if dir_y else 0.0
    return dir_x, dir_y, travel_x, travel_y


def _cover_size(clip: _ClipInfo, width: int, height: int) -> tuple[float, float]:
    factor = max(width / clip.width, height / clip.height)
    return clip.width * factor, clip.height * factor


def _plan_shots(spec: RenderSpec, clips: dict[str, _ClipInfo]) -> tuple[list[_ShotPlan], int]:
    """Exact per-shot frame counts, crossfade frames, source ranges and clip fallbacks."""
    fps = spec.fps
    total_frames = round(spec.duration * fps)
    shots = [s for s in spec.shots if s.duration > 0]
    if not shots:
        raise RenderError("ショットがありません。動画の構成を確認してください。")
    planned = sum(s.duration for s in shots)
    if abs(planned - spec.duration) > 0.05:
        logger.warning("shot durations sum to %.3f s, expected %.3f s; rescaling", planned, spec.duration)
    frames = _frame_allocation([s.duration for s in shots], total_frames)
    kept = [(s, f) for s, f in zip(shots, frames, strict=True) if f > 0]
    shots, frames = [s for s, _ in kept], [f for _, f in kept]

    xfade = 0
    if spec.transition == "crossfade" and len(shots) > 1:
        xfade = min(round(CROSSFADE_SEC * fps), min(frames) // 2)
        if xfade < 2:
            xfade = 0
    head, tail = xfade // 2, xfade - xfade // 2

    plans: list[_ShotPlan] = []
    last = len(shots) - 1
    for index, (shot, base_frames) in enumerate(zip(shots, frames, strict=True)):
        clip = clips[shot.clip_path]
        rendered = base_frames + (head if index > 0 and xfade else 0) + (tail if index < last and xfade else 0)
        speed = _clamp(shot.speed if shot.speed and shot.speed > 0 else 1.0, *_SPEED_LIMITS)
        seconds = rendered / fps
        available = max(clip.duration - 0.05, 0.04)
        need = seconds * speed
        src_in = _clamp(shot.src_in or 0.0, 0.0, max(0.0, available - need))
        loop = False
        if need > available:
            ratio = available / need
            if ratio >= _MIN_SLOWDOWN:
                speed *= ratio  # slow down slightly to stretch the clip
                src_in = 0.0
            else:
                loop = True
                src_in = 0.0
        zoom_from = max(1.0, _float(shot.zoom_from) or 1.0)
        zoom_to = max(1.0, _float(shot.zoom_to) or 1.0)
        dir_x, dir_y, travel_x, travel_y = _pan_travel(shot.pan, seconds, spec.width, spec.height)
        cover_w, cover_h = _cover_size(clip, spec.width, spec.height)
        # A pan needs room: zoom in just enough that the travel never hits the frame edge.
        needed = max(
            (spec.width + travel_x) / cover_w if dir_x else 1.0,
            (spec.height + travel_y) / cover_h if dir_y else 1.0,
        )
        lowest = min(zoom_from, zoom_to)
        if needed > lowest:
            zoom_from += needed - lowest
            zoom_to += needed - lowest
        plans.append(
            _ShotPlan(shot=shot, clip=clip, frames=rendered, src_in=src_in, speed=speed, loop=loop,
                      zoom_from=zoom_from, zoom_to=zoom_to)
        )  # fmt: skip
    return plans, xfade


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# --------------------------------------------------------------------------- filtergraph


def _ease(u: str) -> str:
    """Half linear, half smoothstep: gentle start/stop without a dead pause."""
    return f"(0.5*{u}+0.5*{u}*{u}*(3-2*{u}))"


def _ease_value(u: float) -> float:
    u = min(max(u, 0.0), 1.0)
    return 0.5 * u + 0.5 * u * u * (3 - 2 * u)


@dataclass(frozen=True)
class _ShotGeometry:
    """Ken Burns geometry of one shot.

    The decoded frames are cropped (whole pixels) to the box the camera move
    ever looks at (``box``) and scaled once to square pixels, at most the output
    size (``scaled``). ``perspective`` then resamples the per-frame view with
    sub-pixel corner positions (zoom and pan glide instead of snapping to whole
    pixels) into ``view`` pixels — the source's own detail, so a 720p clip is
    interpolated at 720p — and one fixed scaler takes it to the output size.
    ``perspective`` keeps its frame size, so the scaled box is padded to
    ``frame`` (≥ box and ≥ view) with its edge pixels repeated.
    """

    box: tuple[int, int, int, int]  # x, y, w, h in decoded-frame pixels
    scaled: tuple[int, int]  # size of the box after the one-off scale (≤ output size)
    view: tuple[int, int]  # size the view is resampled to (≤ output size, output aspect)
    frame: tuple[int, int]  # perspective frame size (scaled box + repeated edge, ≥ view)
    corners: tuple[str, str, str, str]  # perspective x0/x1/y0/y1 expressions of the input frame count ``in``
    views: list[tuple[float, float, float, float]]  # per frame (x0, x1, y0, y1), same values as the expressions


def _even_ceil(value: float) -> int:
    return 2 * math.ceil(value / 2 - 1e-6)


def _shot_geometry(plan: _ShotPlan, spec: RenderSpec) -> _ShotGeometry:
    width, height, frames = spec.width, spec.height, plan.frames
    clip = plan.clip
    frame_w, frame_h = clip.frame_size
    # display px per decoded px (sample aspect ratio), cover factor display → output
    aspect_x, aspect_y = clip.width / frame_w, clip.height / frame_h
    cover = max(width / clip.width, height / clip.height)
    cover_w, cover_h = clip.width * cover, clip.height * cover
    seconds = frames / spec.fps
    dir_x, dir_y, travel_x, travel_y = _pan_travel(plan.shot.pan, seconds, width, height)
    zoom_from, zoom_delta = plan.zoom_from, plan.zoom_to - plan.zoom_from

    def view(n: int) -> tuple[float, float, float, float]:
        """Frame n's crop window of the zoomed cover image, as a decoded-frame rectangle (x0, x1, y0, y1)."""
        ease = _ease_value(n / frames)
        zoom = zoom_from + zoom_delta * ease
        x = (cover_w * zoom - width) / 2 + dir_x * travel_x * (ease - 0.5)
        y = (cover_h * zoom - height) / 2 + dir_y * travel_y * (ease - 0.5)
        x = min(max(x, 0.0), cover_w * zoom - width)
        y = min(max(y, 0.0), cover_h * zoom - height)
        scale = zoom * cover  # output px per display px
        return (
            x / scale / aspect_x,
            (x + width) / scale / aspect_x,
            y / scale / aspect_y,
            (y + height) / scale / aspect_y,
        )

    rects = [view(n) for n in range(frames)]
    margin = 3  # room for the cubic kernel
    left = max(0, (math.floor(min(r[0] for r in rects)) - margin) // 2 * 2)
    top = max(0, (math.floor(min(r[2] for r in rects)) - margin) // 2 * 2)
    right = min(frame_w, math.ceil(max(r[1] for r in rects)) + margin)
    bottom = min(frame_h, math.ceil(max(r[3] for r in rects)) + margin)
    box_w, box_h = max(2, right - left), max(2, bottom - top)
    # One-off resize: square pixels, never larger than the output (big sources are
    # downscaled with a proper filter here; ``perspective`` only interpolates).
    shrink = min(1.0, width / (box_w * aspect_x), height / (box_h * aspect_y))
    scaled_w = max(2, min(width, 2 * round(box_w * aspect_x * shrink / 2)))  # even: 4:2:0 padding
    scaled_h = max(2, min(height, 2 * round(box_h * aspect_y * shrink / 2)))
    fx, fy = scaled_w / box_w, scaled_h / box_h
    # Resample the view at the source's own density (the widest view), at most the output size.
    density = min(1.0, max(r[1] - r[0] for r in rects) * fx / width)
    view_w, view_h = min(width, _even_ceil(width * density)), min(height, _even_ceil(height * density))
    full_w = max(view_w, min(scaled_w + _KB_EDGE, width) if scaled_w < width else scaled_w)
    full_h = max(view_h, min(scaled_h + _KB_EDGE, height) if scaled_h < height else scaled_h)

    def corners(rect: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        # ``perspective`` maps output pixel i to x0 + i*(x1-x0)/frame_w (pixel indices,
        # not centres): the view's pixel pitch is sx, the quad extends over the whole
        # frame, and x0 moves by half a pixel of the scale difference so pixel
        # centres line up (a pure zoom stays centred).
        x0, x1 = (rect[0] - left) * fx, (rect[1] - left) * fx
        y0, y1 = (rect[2] - top) * fy, (rect[3] - top) * fy
        sx, sy = (x1 - x0) / view_w, (y1 - y0) / view_h
        x0 += 0.5 * sx - 0.5
        y0 += 0.5 * sy - 0.5
        return x0, x0 + full_w * sx, y0, y0 + full_h * sy

    # The same curve as ``view`` as FFmpeg expressions of the frame number.
    # ``in`` counts input frames from 1 in ``perspective``, so frame n is ``in - 1``.
    ease = _ease(f"min(max((in-1)/{frames},0),1)")
    zoom = f"({_f(zoom_from)}+{_f(zoom_delta)}*{ease})"
    x = f"(({_f(cover_w, 4)}*{zoom}-{width})/2"
    y = f"(({_f(cover_h, 4)}*{zoom}-{height})/2"
    x += f"+{_f(dir_x * travel_x, 4)}*({ease}-0.5))" if dir_x else ")"
    y += f"+{_f(dir_y * travel_y, 4)}*({ease}-0.5))" if dir_y else ")"
    x = f"min(max({x},0),{_f(cover_w, 4)}*{zoom}-{width})"
    y = f"min(max({y},0),{_f(cover_h, 4)}*{zoom}-{height})"
    gx = fx / (cover * aspect_x)  # scaled-box px per (output px / zoom)
    gy = fy / (cover * aspect_y)
    x0 = f"({_f(gx, 9)}*({x}+{_f(0.5 * width / view_w, 9)})/{zoom}-{_f(left * fx + 0.5, 6)})"
    y0 = f"({_f(gy, 9)}*({y}+{_f(0.5 * height / view_h, 9)})/{zoom}-{_f(top * fy + 0.5, 6)})"
    x1 = f"({x0}+{_f(gx * width * full_w / view_w, 6)}/{zoom})"
    y1 = f"({y0}+{_f(gy * height * full_h / view_h, 6)}/{zoom})"
    return _ShotGeometry(
        box=(left, top, box_w, box_h),
        scaled=(scaled_w, scaled_h),
        view=(view_w, view_h),
        frame=(full_w, full_h),
        corners=(x0, x1, y0, y1),
        views=[corners(r) for r in rects],
    )


def _shot_chain(index: int, plan: _ShotPlan, spec: RenderSpec) -> str:
    fps, width, height = spec.fps, spec.width, spec.height
    seconds = plan.frames / fps
    geometry = _shot_geometry(plan, spec)
    frame_w, frame_h = plan.clip.frame_size
    box_x, box_y, box_w, box_h = geometry.box
    scaled_w, scaled_h = geometry.scaled
    view_w, view_h = geometry.view
    full_w, full_h = geometry.frame
    matrix = plan.clip.color_matrix
    x0, x1, y0, y1 = geometry.corners
    flags = "bicubic+full_chroma_int+accurate_rnd"

    parts = ["setpts=PTS-STARTPTS"]
    if abs(plan.speed - 1.0) > 1e-3:
        parts.append(f"setpts=PTS/{_f(plan.speed)}")
    parts += [
        f"fps={fps}",
        # Guarantees the exact frame count even if the source ends a little early.
        f"tpad=stop_mode=clone:stop_duration={_f(seconds + 1, 3)}",
        f"trim=end_frame={plan.frames}",
        "setpts=PTS-STARTPTS",
    ]
    if (box_x, box_y, box_w, box_h) != (0, 0, frame_w, frame_h):
        # Whole-pixel crop to what the move ever shows (cheap: a pointer offset), so
        # a landscape source is not upscaled in full only to throw most of it away.
        parts.append(f"crop=w={box_w}:h={box_h}:x={box_x}:y={box_y}:exact=1")
    parts += [
        f"scale=w={scaled_w}:h={scaled_h}:flags={flags}:in_color_matrix={matrix}:out_color_matrix={matrix}",
        f"format={_KB_PIXFMT}",
    ]
    if (scaled_w, scaled_h) != (full_w, full_h):
        # Repeat the edge pixels a little, so interpolation at the box edge never
        # pulls in black (the view never reaches further than the kernel radius).
        edge_w, edge_h = min(full_w, scaled_w + _KB_EDGE), min(full_h, scaled_h + _KB_EDGE)
        parts += [
            f"pad=w={edge_w}:h={edge_h}:x=0:y=0",
            f"fillborders=right={edge_w - scaled_w}:bottom={edge_h - scaled_h}:mode=smear",
        ]
        if (edge_w, edge_h) != (full_w, full_h):
            parts.append(f"pad=w={full_w}:h={full_h}:x=0:y=0")
    # Sub-pixel zoom/pan: the view's corners are evaluated per frame and the image
    # is resampled with cubic interpolation.
    parts.append(
        f"perspective=x0='{x0}':y0='{y0}':x1='{x1}':y1='{y0}':x2='{x0}':y2='{y1}':x3='{x1}':y3='{y1}'"
        f":interpolation={_KB_INTERP}:eval=frame"
    )
    if (view_w, view_h) != (full_w, full_h):
        parts.append(f"crop=w={view_w}:h={view_h}:x=0:y=0")
    parts += [
        f"scale=w={width}:h={height}:flags={flags}:in_color_matrix={matrix}",
        "format=gbrp",
    ]
    if plan.shot.hflip:
        parts.append("hflip")
    parts.append("setsar=1")
    return f"[{index}:v:0]" + ",".join(parts) + f"[s{index}]"


def _subtitles_filter(ass_path: Path, fonts_dir: str | None) -> str:
    value = f"subtitles=filename={escape_filter_value(str(ass_path))}"
    if fonts_dir:
        value += f":fontsdir={escape_filter_value(fonts_dir)}"
    return value


def _fonts_dir() -> str | None:
    try:
        from app.config import get_settings

        configured = get_settings().FONTS_DIR
        value = str(configured) if configured else None
    except Exception:  # noqa: BLE001 - settings are optional for the renderer
        value = os.environ.get("FONTS_DIR") or None
    if value and Path(value).is_dir():
        return str(Path(value).resolve())
    return None


def _build_filtergraph(
    spec: RenderSpec,
    plans: Sequence[_ShotPlan],
    xfade: int,
    ass_path: Path | None,
    audio: _AudioInfo,
    audio_index: int,
    post_steps: Sequence[_PostStep],
    audio_gain_db: float = 0.0,
) -> str:
    chains = [_shot_chain(i, plan, spec) for i, plan in enumerate(plans)]
    fps = spec.fps
    if len(plans) == 1:
        joined = "s0"
    elif xfade:
        current, length = "s0", plans[0].frames
        for i in range(1, len(plans)):
            offset = (length - xfade) / fps
            label = f"x{i}"
            chains.append(
                f"[{current}][s{i}]xfade=transition=fade:duration={_f(xfade / fps)}:offset={_f(offset)}[{label}]"
            )
            current, length = label, length + plans[i].frames - xfade
        joined = current
    else:
        inputs = "".join(f"[s{i}]" for i in range(len(plans)))
        chains.append(f"{inputs}concat=n={len(plans)}:v=1:a=0[cat]")
        joined = "cat"

    width, height = spec.width, spec.height
    current = joined
    pending = ["format=gbrp"]
    for k, step in enumerate(post_steps):
        if step.kind == "filters":
            pending.append(step.value)
            continue
        base, overlay, out = f"p{k}", f"m{k}", f"q{k}"
        chains.append(f"[{current}]" + ",".join(pending) + f"[{base}]")
        if step.kind == "vignette":
            # The lens falloff is static: render it once on white, loop that frame and multiply.
            chains.append(
                f"color=c=white:s={width}x{height}:r={fps},trim=end_frame=1,format=gbrp,{step.value},"
                f"format=gbrp,loop=loop=-1:size=1,setpts=N/{fps}/TB[{overlay}]"
            )
            chains.append(f"[{base}][{overlay}]blend=all_mode=multiply:shortest=1[{out}]")
        else:
            # Soft (half-res, upscaled) temporal grain: noise on G only, copied to R and B
            # by the channel mixer, so the grain is neutral (luma only) after grainmerge.
            half_w, half_h = max(2, width // 2), max(2, height // 2)
            chains.append(
                f"color=c=0x808080:s={half_w}x{half_h}:r={fps},format=gbrp,"
                f"noise=c0s={step.value}:c0f=t:c0_seed={_GRAIN_SEED},"
                "colorchannelmixer=rr=0:rg=1:rb=0:br=0:bg=1:bb=0,"
                f"scale={width}:{height}:flags=bicubic,format=gbrp[{overlay}]"
            )
            chains.append(f"[{base}][{overlay}]blend=all_mode=grainmerge:shortest=1[{out}]")
        current, pending = out, ["format=gbrp"]
    if ass_path is not None:
        pending.append(_subtitles_filter(ass_path, _fonts_dir()))
    pending += [
        "scale=out_color_matrix=bt709:out_range=tv:flags=bicubic+full_chroma_int+accurate_rnd",
        "format=yuv420p",
    ]
    chains.append(f"[{current}]" + ",".join(pending) + "[vout]")

    audio_parts = _audio_chain(spec, audio)
    if audio_gain_db < 0:
        # Pure linear gain (no limiter): keeps the AAC encode from clipping on hot masters.
        audio_parts.append(f"volume={audio_gain_db:.4f}dB")
    chains.append(f"[{audio_index}:a:0]" + ",".join(audio_parts) + "[aout]")
    return ";\n".join(chains)


def _audio_chain(spec: RenderSpec, audio: _AudioInfo) -> list[str]:
    """The song segment as encoded: exact length, short fades, stereo float at the output rate."""
    duration = _f(spec.duration)
    fade_in = max(0.0, min(spec.audio_fade_in, spec.duration / 4))
    fade_out = max(0.0, min(spec.audio_fade_out, spec.duration / 3))
    audio_parts = [
        f"atrim=duration={duration}",
        "asetpts=PTS-STARTPTS",
        f"apad=whole_dur={duration}",
        f"atrim=duration={duration}",
    ]
    if fade_in > 0:
        audio_parts.append(f"afade=t=in:st=0:d={_f(fade_in)}")
    if fade_out > 0:
        audio_parts.append(f"afade=t=out:st={_f(spec.duration - fade_out)}:d={_f(fade_out)}")
    audio_parts.append(f"aformat=sample_fmts=fltp:sample_rates={audio.sample_rate}:channel_layouts=stereo")
    return audio_parts


def _true_peak_dbtp(audio_path: str, start: float, chain: Sequence[str]) -> float | None:
    """True peak (dBTP, ITU-R BS.1770 via ``ebur128=peak=true``) of the segment exactly as it
    would be encoded (same input seek and audio chain), or None when it cannot be measured."""
    cmd = [FFMPEG, "-nostdin", "-hide_banner", "-nostats", "-v", "info"]
    if start > 0:
        cmd += ["-ss", _f(start, 6)]
    measure = "ebur128=peak=true:framelog=quiet:metadata=1,ametadata=mode=print:key=lavfi.r128.true_peak"
    cmd += ["-i", audio_path, "-vn", "-af", ",".join([*chain, measure]), "-f", "null", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    # ``lavfi.r128.true_peak`` is the running maximum over all channels (linear); the last one counts.
    values = re.findall(rb"lavfi\.r128\.true_peak=([0-9.eE+-]+|inf|-inf|nan)", proc.stderr)
    if proc.returncode != 0 or not values:
        return None
    linear = _float(values[-1].decode())
    if linear is None or linear < 0:
        return None
    return 20 * math.log10(linear) if linear > 0 else -math.inf


def _audio_gain_db(audio_path: str, spec: RenderSpec, audio: _AudioInfo) -> float:
    """Gain (≤ 0 dB) that brings a segment peaking above ``TRUE_PEAK_CEILING_DBTP`` down to it; 0 otherwise."""
    peak = _true_peak_dbtp(audio_path, spec.audio_start, _audio_chain(spec, audio))
    if peak is None:
        logger.warning("could not measure the true peak of %s; encoding without headroom", audio_path)
        return 0.0
    return round(TRUE_PEAK_CEILING_DBTP - peak, 4) if peak > TRUE_PEAK_CEILING_DBTP else 0.0


# --------------------------------------------------------------------------- running ffmpeg


def _report(progress_cb: ProgressCallback | None, fraction: float, stage: str) -> None:
    if progress_cb is not None:
        progress_cb(max(0.0, min(1.0, fraction)), stage)


def _tail(path: Path, lines: int = _LOG_TAIL_LINES) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return "".join(deque(fh, maxlen=lines)).strip()
    except OSError:
        return ""


def _run_ffmpeg(
    cmd: list[str],
    *,
    log_path: Path,
    duration: float,
    timeout: float,
    progress_cb: ProgressCallback | None,
    span: tuple[float, float],
) -> None:
    """Run FFmpeg with ``-progress pipe:1``; stderr goes to ``log_path``.

    An exception raised by ``progress_cb`` (e.g. a cancellation) stops FFmpeg
    and propagates.
    """
    low, high = span
    with log_path.open("wb") as log:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log, stdin=subprocess.DEVNULL)
        except FileNotFoundError as exc:
            raise RenderError("FFmpeg が見つかりません。サーバーに FFmpeg をインストールしてください。") from exc
        timed_out = threading.Event()

        def _kill() -> None:
            timed_out.set()
            proc.kill()

        timer = threading.Timer(timeout, _kill)
        timer.daemon = True
        timer.start()
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                    value = _float(line.split("=", 1)[1])
                    if value is not None and duration > 0:
                        _report(progress_cb, low + (high - low) * min(value / 1e6 / duration, 1.0), "encode")
            proc.wait()
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        finally:
            timer.cancel()
    if timed_out.is_set():
        raise RenderError("動画のレンダリングがタイムアウトしました。", stderr_tail=_tail(log_path))
    if proc.returncode != 0:
        raise RenderError(
            f"動画のレンダリングに失敗しました（FFmpeg 終了コード {proc.returncode}）。",
            stderr_tail=_tail(log_path),
        )


def _make_thumbnail(video: Path, out: Path, at: float, width: int, height: int) -> None:
    thumb_w, thumb_h = max(2, width // 4 * 2), max(2, height // 4 * 2)  # half size, even
    cmd = [
        FFMPEG, "-nostdin", "-v", "error", "-y", "-ss", _f(at, 3), "-i", str(video),
        "-frames:v", "1", "-vf", f"scale={thumb_w}:{thumb_h}:flags=lanczos", "-q:v", "3", str(out),
    ]  # fmt: skip
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RenderError("サムネイル画像を生成できませんでした。") from exc
    if proc.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        raise RenderError(
            "サムネイル画像を生成できませんでした。", stderr_tail=proc.stderr.decode("utf-8", "replace")[-2000:]
        )


def _probe_duration(path: Path) -> float:
    data = _ffprobe_json(str(path))
    return _float((data.get("format") or {}).get("duration")) or 0.0


# --------------------------------------------------------------------------- public entry point


def _validate(spec: RenderSpec) -> None:
    if not spec.shots:
        raise RenderError("ショットがありません。動画の構成を確認してください。")
    if not (spec.duration and spec.duration > 0 and math.isfinite(spec.duration)):
        raise RenderError("動画の長さが不正です。")
    if spec.width <= 0 or spec.height <= 0 or spec.width % 2 or spec.height % 2:
        raise RenderError("出力解像度が不正です（幅・高さは正の偶数である必要があります）。")
    if spec.fps <= 0:
        raise RenderError("フレームレートが不正です。")
    if not Path(spec.audio_path).is_file():
        raise RenderError(f"音声ファイルが見つかりません: {Path(spec.audio_path).name}")
    for shot in spec.shots:
        if not Path(shot.clip_path).is_file():
            raise RenderError(f"素材ファイルが見つかりません: {Path(shot.clip_path).name}")


def render_video(spec: RenderSpec, progress_cb: ProgressCallback | None = None) -> RenderResult:
    """Render ``spec`` to ``spec.output_path`` (+ ``<stem>_thumb.jpg``). Raises :class:`RenderError`."""
    started = time.monotonic()
    _validate(spec)
    _report(progress_cb, 0.0, "prepare")
    work_dir = Path(spec.work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    output = Path(spec.output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    audio_path = str(Path(spec.audio_path).resolve())

    audio = _probe_audio(audio_path)
    if audio.duration is not None and spec.audio_start >= audio.duration - 0.05:
        raise RenderError("音声の開始位置が曲の長さを超えています。")
    clips: dict[str, _ClipInfo] = {}
    resolved_shots: list[RenderShot] = []
    for shot in spec.shots:
        path = str(Path(shot.clip_path).resolve())
        if path not in clips:
            clips[path] = _probe_clip(path)
        resolved_shots.append(RenderShot(**{**shot.__dict__, "clip_path": path}))
    spec_resolved = RenderSpec(**{**spec.__dict__, "shots": resolved_shots})
    plans, xfade = _plan_shots(spec_resolved, clips)

    ass_path: Path | None = None
    if spec.lyric_lines:
        ass_text = build_ass(
            spec.lyric_lines,
            style_id=spec.lyric_style,
            text_style=spec.text_style or {},
            segment_start=spec.audio_start,
            duration=spec.duration,
            width=spec.width,
            height=spec.height,
            beats=spec.beats,
        )
        if "\nDialogue:" in ass_text:
            ass_path = work_dir / "lyrics.ass"
            ass_path.write_text(ass_text, encoding="utf-8")

    cmd: list[str] = [FFMPEG, "-nostdin", "-hide_banner", "-y", "-v", "warning", "-progress", "pipe:1", "-nostats"]
    for plan in plans:
        if plan.loop:
            cmd += ["-stream_loop", "-1"]
        if plan.src_in > 0:
            cmd += ["-ss", _f(plan.src_in, 3)]
        cmd += ["-t", _f(plan.frames / spec.fps * plan.speed + 0.5, 3), "-i", plan.clip.path]
    audio_index = len(plans)
    if spec.audio_start > 0:
        cmd += ["-ss", _f(spec.audio_start, 6)]
    cmd += ["-i", audio_path]

    post_steps = _grade_steps(spec.color_grade, work_dir)
    audio_gain_db = _audio_gain_db(audio_path, spec, audio)
    graph = _build_filtergraph(spec, plans, xfade, ass_path, audio, audio_index, post_steps, audio_gain_db)
    (work_dir / "filtergraph.txt").write_text(graph, encoding="utf-8")
    preset = spec.preset if spec.preset in _ALLOWED_PRESETS else DEFAULT_PRESET
    crf = int(_clamp(spec.crf, 0, 51))
    partial = output.with_name(f".{output.stem}.part.mp4")
    cmd += [
        "-filter_complex", graph,
        "-map", "[vout]", "-map", "[aout]",
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-g", str(spec.fps * 2), "-keyint_min", str(spec.fps // 2 or 1),
        "-maxrate", MAX_RATE, "-bufsize", BUF_SIZE,
        "-r", str(spec.fps), "-fps_mode", "cfr",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
        "-c:a", "aac", "-profile:a", "aac_low", "-b:a", spec.audio_bitrate,
        "-ar", str(audio.sample_rate), "-ac", "2",
        "-movflags", "+faststart",
        "-f", "mp4", str(partial),
    ]  # fmt: skip

    log_path = work_dir / "ffmpeg.log"
    prepared = time.monotonic()
    timeout = max(300.0, spec.duration * 20)
    try:
        _run_ffmpeg(
            cmd, log_path=log_path, duration=spec.duration, timeout=timeout,
            progress_cb=progress_cb, span=(0.03, 0.95),
        )  # fmt: skip
        os.replace(partial, output)
    finally:
        partial.unlink(missing_ok=True)
    encoded = time.monotonic()

    _report(progress_cb, 0.96, "thumbnail")
    thumbnail = output.with_name(f"{output.stem}_thumb.jpg")
    _make_thumbnail(output, thumbnail, min(1.0, spec.duration / 2), spec.width, spec.height)
    duration = _probe_duration(output)
    finished = time.monotonic()
    _report(progress_cb, 1.0, "done")
    return RenderResult(
        output_path=str(output),
        thumbnail_path=str(thumbnail),
        duration=duration,
        size_bytes=output.stat().st_size,
        log_tail=_tail(log_path),
        timings={
            "prepare": round(prepared - started, 3),
            "encode": round(encoded - prepared, 3),
            "finalize": round(finished - encoded, 3),
            "total": round(finished - started, 3),
        },
        audio_gain_db=audio_gain_db,
    )
