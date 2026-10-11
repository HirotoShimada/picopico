"""Output validation for rendered videos (ARCHITECTURE.md §6.6).

:func:`validate_output` probes a rendered MP4 and returns a **QualityReport**::

    {"passed": bool,
     "checks": [{"name", "passed", "value", "expected"}, ...],
     "loudness_lufs": float | None,
     "black_ratio": float}

Checks: container mp4, H.264 video, yuv420p, frame size, 30 fps, duration
±0.1 s, AAC audio present, audio duration ±0.1 s, faststart (``moov`` before
``mdat``), black-frame ratio < 0.5 (``blackdetect``) and the integrated
loudness (``ebur128``), which is informational only and always passes.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any

__all__ = ["DURATION_TOLERANCE", "MAX_BLACK_RATIO", "mp4_top_level_boxes", "validate_output"]

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

DURATION_TOLERANCE = 0.1
MAX_BLACK_RATIO = 0.5
_MP4_BRANDS = frozenset({"isom", "iso2", "iso3", "iso4", "iso5", "iso6", "mp41", "mp42", "avc1", "dash"})
_BLACK_RE = re.compile(r"black_start:\s*([\d.]+)\s+black_end:\s*([\d.]+)\s+black_duration:\s*([\d.]+)")
_LUFS_RE = re.compile(r"Integrated loudness:\s*I:\s*(-?[\d.]+|-inf|nan)\s*LUFS", re.IGNORECASE)


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _rate(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    num, _, den = value.partition("/")
    n, d = _float(num), _float(den or "1")
    if n is None or not d:
        return None
    return n / d


def mp4_top_level_boxes(path: str | os.PathLike[str]) -> list[tuple[str, int, int]]:
    """``(type, offset, size)`` of the top-level ISO-BMFF boxes (stops at the first malformed box)."""
    boxes: list[tuple[str, int, int]] = []
    with open(path, "rb") as fh:
        total = os.fstat(fh.fileno()).st_size
        offset = 0
        while offset + 8 <= total and len(boxes) < 10_000:
            fh.seek(offset)
            header = fh.read(16)
            if len(header) < 8:
                break
            size, kind = struct.unpack(">I4s", header[:8])
            if size == 1:
                if len(header) < 16:
                    break
                size = struct.unpack(">Q", header[8:16])[0]
            elif size == 0:
                size = total - offset
            if size < 8:
                break
            boxes.append((kind.decode("latin-1"), offset, size))
            offset += size
    return boxes


def _probe(path: Path) -> dict[str, Any] | None:
    cmd = [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout or b"{}")
    except json.JSONDecodeError:
        return None


def _analyze(path: Path, duration: float, has_audio: bool) -> tuple[float | None, float | None]:
    """One decode pass: (black seconds, integrated loudness LUFS)."""
    graph = "[0:v:0]scale=270:480,blackdetect=d=0.1:pic_th=0.98:pix_th=0.10[v]"
    maps = ["-map", "[v]"]
    if has_audio:
        graph += ";[0:a:0]ebur128=framelog=verbose[a]"
        maps += ["-map", "[a]"]
    cmd = [FFMPEG, "-nostdin", "-hide_banner", "-nostats", "-v", "info", "-i", str(path),
           "-filter_complex", graph, *maps, "-f", "null", "-"]  # fmt: skip
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=max(60.0, duration * 6), check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    if proc.returncode != 0:
        return None, None
    log = proc.stderr.decode("utf-8", "replace")
    black = sum(float(m.group(3)) for m in _BLACK_RE.finditer(log))
    lufs: float | None = None
    match = _LUFS_RE.search(log)
    if match:
        lufs = _float(match.group(1))
    return black, lufs


def validate_output(
    path: str | os.PathLike[str],
    *,
    expected_duration: float,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
) -> dict[str, Any]:
    """Validate a rendered MP4 and return a QualityReport dict (never raises for bad media)."""
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, value: Any, expected: Any) -> None:
        checks.append({"name": name, "passed": bool(passed), "value": value, "expected": expected})

    file_path = Path(path)
    data = _probe(file_path) if file_path.is_file() else None
    if data is None:
        add("readable", False, None, "ffprobe で読み取れる動画ファイル")
        return {"passed": False, "checks": checks, "loudness_lufs": None, "black_ratio": 0.0}

    fmt: dict[str, Any] = data.get("format") or {}
    streams: list[dict[str, Any]] = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    format_names = {part.strip() for part in str(fmt.get("format_name") or "").split(",")}
    brand = str((fmt.get("tags") or {}).get("major_brand") or "").strip().lower()
    add("container", "mp4" in format_names and (not brand or brand in _MP4_BRANDS),
        f"mp4 ({brand})" if brand else ",".join(sorted(format_names)) or None, "mp4")  # fmt: skip

    codec = video.get("codec_name") if video else None
    profile = video.get("profile") if video else None
    add("video_codec", codec == "h264", f"{codec} ({profile})" if codec and profile else codec, "h264")
    pix_fmt = video.get("pix_fmt") if video else None
    add("pix_fmt", pix_fmt == "yuv420p", pix_fmt, "yuv420p")
    size = f"{video.get('width')}x{video.get('height')}" if video else None
    add("resolution", size == f"{width}x{height}", size, f"{width}x{height}")
    avg_rate = _rate(video.get("avg_frame_rate")) if video else None
    real_rate = _rate(video.get("r_frame_rate")) if video else None
    fps_ok = (
        avg_rate is not None and abs(avg_rate - fps) < 0.01 and real_rate is not None and abs(real_rate - fps) < 0.01
    )
    add("fps", fps_ok, round(avg_rate, 3) if avg_rate is not None else None, fps)

    duration = _float(fmt.get("duration"))
    add(
        "duration",
        duration is not None and abs(duration - expected_duration) <= DURATION_TOLERANCE,
        round(duration, 3) if duration is not None else None,
        f"{expected_duration:.2f}±{DURATION_TOLERANCE}",
    )

    a_codec = audio.get("codec_name") if audio else None
    a_profile = audio.get("profile") if audio else None
    add("audio_codec", a_codec == "aac" and a_profile in (None, "LC"),
        f"{a_codec} ({a_profile})" if a_codec and a_profile else a_codec, "aac (LC)")  # fmt: skip
    a_duration = _float(audio.get("duration")) if audio else None
    add(
        "audio_duration",
        a_duration is not None and abs(a_duration - expected_duration) <= DURATION_TOLERANCE,
        round(a_duration, 3) if a_duration is not None else None,
        f"{expected_duration:.2f}±{DURATION_TOLERANCE}",
    )

    try:
        boxes = mp4_top_level_boxes(file_path)
    except OSError:
        boxes = []
    offsets = {kind: offset for kind, offset, _ in reversed(boxes)}  # first occurrence wins
    moov, mdat = offsets.get("moov"), offsets.get("mdat")
    faststart = moov is not None and (mdat is None or moov < mdat)
    order = " < ".join(kind for kind, _, _ in boxes if kind in ("moov", "mdat")) or None
    add("faststart", faststart, order, "moov < mdat")

    black_seconds, lufs = (None, None)
    if video is not None:
        black_seconds, lufs = _analyze(file_path, duration or expected_duration, audio is not None)
    total = duration or expected_duration
    black_ratio = min(1.0, black_seconds / total) if black_seconds is not None and total > 0 else 0.0
    add("black_frames", black_seconds is not None and black_ratio < MAX_BLACK_RATIO,
        round(black_ratio, 3), f"< {MAX_BLACK_RATIO}")  # fmt: skip
    add("loudness", True, round(lufs, 1) if lufs is not None else None, "参考値（LUFS）")

    return {
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
        "loudness_lufs": round(lufs, 2) if lufs is not None else None,
        "black_ratio": round(black_ratio, 4),
    }
