"""FFprobe/FFmpeg helpers and upload validation.

File types are validated by *content* (ffprobe must recognise an allowed
container and find the required stream), not only by extension.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import unicodedata
import uuid
from pathlib import Path
from typing import IO, Any, Protocol

import anyio

from app.services.errors import MediaValidationError, ServiceError

FFPROBE = shutil.which("ffprobe") or "ffprobe"
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"

AUDIO_EXTENSIONS: frozenset[str] = frozenset({".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"})
VIDEO_EXTENSIONS: frozenset[str] = frozenset({".mp4", ".mov", ".webm", ".m4v"})

AUDIO_CONTENT_TYPES: dict[str, str] = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
}
VIDEO_CONTENT_TYPES: dict[str, str] = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".m4v": "video/x-m4v",
}

# ffprobe demuxer names (``format.format_name`` is a comma list) accepted per upload kind.
_AUDIO_FORMATS = frozenset({"mp3", "wav", "w64", "mov", "mp4", "m4a", "aac", "flac", "ogg", "matroska", "webm"})
_VIDEO_FORMATS = frozenset({"mov", "mp4", "m4a", "3gp", "3g2", "mj2", "matroska", "webm"})

MIN_AUDIO_SEC = 5.0
MAX_AUDIO_SEC = 15 * 60.0
MIN_VIDEO_SEC = 1.0
MAX_VIDEO_SEC = 10 * 60.0
MAX_VIDEO_DIMENSION = 7680

_PROBE_TIMEOUT = 60
_FFMPEG_TIMEOUT = 120
_CHUNK = 1024 * 1024


# --- probing ---------------------------------------------------------------------


def _to_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and result not in (float("inf"), float("-inf")) else None


def _to_int(value: Any) -> int | None:
    f = _to_float(value)
    return int(f) if f is not None else None


def _parse_rate(rate: Any) -> float | None:
    if not isinstance(rate, str) or not rate:
        return None
    if "/" in rate:
        num, _, den = rate.partition("/")
        n, d = _to_float(num), _to_float(den)
        if n is None or not d:
            return None
        return round(n / d, 3) if n > 0 else None
    value = _to_float(rate)
    return round(value, 3) if value else None


def _rotation(stream: dict[str, Any]) -> int:
    rotation: float | None = None
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            rotation = _to_float(side.get("rotation"))
            break
    if rotation is None:
        rotation = _to_float((stream.get("tags") or {}).get("rotate"))
    if rotation is None:
        return 0
    return round(rotation) % 360


def probe(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Run ffprobe and return a normalised description of the file.

    Keys: ``duration`` (s, None if unknown), ``has_audio``, ``has_video``
    (cover-art "attached pictures" do not count as video), ``width``/``height``
    (*display* size, i.e. after applying ``rotation``), ``coded_width``/
    ``coded_height``, ``fps``, ``rotation`` (0/90/180/270), ``codecs``
    (``{"video": str|None, "audio": str|None}``), ``format_name``,
    ``bit_rate``, ``size``, ``pix_fmt``, ``sample_rate``, ``channels``.

    Raises :class:`MediaValidationError` when ffprobe cannot read the file.
    """
    cmd = [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", os.fspath(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_PROBE_TIMEOUT, check=False)
    except FileNotFoundError as exc:
        raise ServiceError("ffprobe が見つかりません。サーバーに FFmpeg をインストールしてください。") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaValidationError("メディアファイルの解析がタイムアウトしました。") from exc
    if proc.returncode != 0:
        raise MediaValidationError("メディアファイルを読み取れませんでした（形式が不正か、ファイルが破損しています）。")
    try:
        data = json.loads(proc.stdout or b"{}")
    except json.JSONDecodeError as exc:
        raise MediaValidationError("メディアファイルの解析結果を読み取れませんでした。") from exc

    fmt: dict[str, Any] = data.get("format") or {}
    streams: list[dict[str, Any]] = data.get("streams") or []
    videos = [
        s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
    ]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    v = videos[0] if videos else None
    a = audios[0] if audios else None

    duration = _to_float(fmt.get("duration"))
    if duration is None:
        stream_durations = [d for s in (v, a) if s for d in [_to_float(s.get("duration"))] if d]
        duration = max(stream_durations) if stream_durations else None

    coded_w = _to_int(v.get("width")) if v else None
    coded_h = _to_int(v.get("height")) if v else None
    rotation = _rotation(v) if v else 0
    width, height = coded_w, coded_h
    if rotation % 180 == 90:
        width, height = coded_h, coded_w

    fps = None
    if v:
        fps = _parse_rate(v.get("avg_frame_rate")) or _parse_rate(v.get("r_frame_rate"))

    return {
        "duration": round(duration, 3) if duration is not None else None,
        "has_audio": a is not None,
        "has_video": v is not None,
        "width": width,
        "height": height,
        "coded_width": coded_w,
        "coded_height": coded_h,
        "fps": fps,
        "rotation": rotation,
        "codecs": {
            "video": v.get("codec_name") if v else None,
            "audio": a.get("codec_name") if a else None,
        },
        "pix_fmt": v.get("pix_fmt") if v else None,
        "sample_rate": _to_int(a.get("sample_rate")) if a else None,
        "channels": _to_int(a.get("channels")) if a else None,
        "format_name": fmt.get("format_name") or "",
        "bit_rate": _to_int(fmt.get("bit_rate")),
        "size": _to_int(fmt.get("size")),
    }


# --- filenames ---------------------------------------------------------------------

_RESERVED_CHARS = re.compile(r'[<>:"/\\|?*]')
_WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])$", re.IGNORECASE)
_MAX_FILENAME_LEN = 150


def sanitize_filename(name: str | None, default: str = "file") -> str:
    """Return a display-safe base filename (keeps Japanese characters).

    Strips any directory part, control/format characters (incl. bidi
    overrides), path-reserved characters, and leading/trailing dots and spaces;
    limits the length while keeping the extension. Never used to build a
    storage path — storage keys are generated server-side.
    """
    name = unicodedata.normalize("NFC", name or "")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if not unicodedata.category(ch).startswith("C"))
    name = _RESERVED_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    stem, ext = os.path.splitext(name)
    if not re.fullmatch(r"\.[0-9A-Za-z]{1,10}", ext or ""):
        stem, ext = name, ""
    stem = stem.strip(" .")
    if not stem or set(stem) <= {"_"}:
        stem = default
    if _WINDOWS_RESERVED.match(stem):
        stem = f"_{stem}"
    stem = stem[: _MAX_FILENAME_LEN - len(ext)].rstrip(" .") or default
    return f"{stem}{ext.lower()}"


def file_extension(filename: str | None) -> str:
    """Lower-case extension including the dot (``".mp3"``) or ``""``."""
    return os.path.splitext(filename or "")[1].lower()


def check_extension(filename: str | None, allowed: frozenset[str], label: str) -> str:
    ext = file_extension(filename)
    if ext not in allowed:
        allowed_list = ", ".join(sorted(e.lstrip(".") for e in allowed))
        shown = ext.lstrip(".") or "拡張子なし"
        raise MediaValidationError(f"対応していない{label}形式です（{shown}）。対応形式: {allowed_list}")
    return ext


# --- upload validation -------------------------------------------------------------


def _format_allowed(format_name: str, allowed: frozenset[str]) -> bool:
    return any(part.strip() in allowed for part in format_name.split(","))


def _fmt_seconds(sec: float) -> str:
    if sec >= 60:
        minutes, rest = divmod(round(sec), 60)
        return f"{minutes}分{rest:02d}秒" if rest else f"{minutes}分"
    return f"{sec:g}秒"


def validate_audio_upload(path: str | os.PathLike[str], filename: str) -> dict[str, Any]:
    """Validate an uploaded song; returns :func:`probe` output."""
    check_extension(filename, AUDIO_EXTENSIONS, "音声")
    try:
        info = probe(path)
    except MediaValidationError as exc:
        raise MediaValidationError(
            "音声ファイルとして読み取れませんでした。ファイルの中身が音声データか確認してください。"
        ) from exc
    if not _format_allowed(info["format_name"], _AUDIO_FORMATS) or not info["has_audio"]:
        raise MediaValidationError("音声ストリームが見つかりません。ファイルの中身が音声データか確認してください。")
    duration = info["duration"]
    if duration is None:
        raise MediaValidationError("音声の長さを判定できませんでした。別の形式で書き出して再度お試しください。")
    if duration < MIN_AUDIO_SEC:
        raise MediaValidationError(
            f"音声が短すぎます（{duration:.1f}秒）。{_fmt_seconds(MIN_AUDIO_SEC)}以上の音源をアップロードしてください。"
        )
    if duration > MAX_AUDIO_SEC:
        raise MediaValidationError(
            f"音声が長すぎます（{_fmt_seconds(duration)}）。{_fmt_seconds(MAX_AUDIO_SEC)}以内の音源をアップロードしてください。"
        )
    return info


def validate_video_upload(path: str | os.PathLike[str], filename: str) -> dict[str, Any]:
    """Validate an uploaded video material clip; returns :func:`probe` output."""
    check_extension(filename, VIDEO_EXTENSIONS, "動画")
    try:
        info = probe(path)
    except MediaValidationError as exc:
        raise MediaValidationError(
            "動画ファイルとして読み取れませんでした。ファイルの中身が動画データか確認してください。"
        ) from exc
    if not _format_allowed(info["format_name"], _VIDEO_FORMATS) or not info["has_video"]:
        raise MediaValidationError("映像ストリームが見つかりません。ファイルの中身が動画データか確認してください。")
    width, height = info["width"] or 0, info["height"] or 0
    if width <= 0 or height <= 0:
        raise MediaValidationError("動画の解像度を判定できませんでした。")
    if max(width, height) > MAX_VIDEO_DIMENSION:
        raise MediaValidationError(
            f"動画の解像度が大きすぎます（{width}×{height}）。長辺{MAX_VIDEO_DIMENSION}px以下にしてください。"
        )
    duration = info["duration"]
    if duration is None:
        raise MediaValidationError("動画の長さを判定できませんでした。")
    if duration < MIN_VIDEO_SEC:
        raise MediaValidationError(
            f"動画が短すぎます（{duration:.1f}秒）。{_fmt_seconds(MIN_VIDEO_SEC)}以上の素材をアップロードしてください。"
        )
    if duration > MAX_VIDEO_SEC:
        raise MediaValidationError(
            f"動画が長すぎます（{_fmt_seconds(duration)}）。{_fmt_seconds(MAX_VIDEO_SEC)}以内の素材をアップロードしてください。"
        )
    return info


# --- thumbnails ------------------------------------------------------------------------


def make_thumbnail(
    video_path: str | os.PathLike[str],
    out_jpg: str | os.PathLike[str],
    at: float = 1.0,
    width: int = 540,
) -> Path:
    """Extract one JPEG frame scaled to ``width`` (height keeps aspect, even)."""
    out = Path(out_jpg)
    out.parent.mkdir(parents=True, exist_ok=True)
    for seek in dict.fromkeys((max(at, 0.0), 0.0)):
        out.unlink(missing_ok=True)
        cmd = [
            FFMPEG, "-nostdin", "-v", "error", "-y",
            "-ss", f"{seek:.3f}", "-i", os.fspath(video_path),
            "-frames:v", "1", "-vf", f"scale={int(width)}:-2", "-q:v", "3",
            os.fspath(out),
        ]  # fmt: skip
        try:
            subprocess.run(cmd, capture_output=True, timeout=_FFMPEG_TIMEOUT, check=False)
        except FileNotFoundError as exc:
            raise ServiceError("ffmpeg が見つかりません。サーバーに FFmpeg をインストールしてください。") from exc
        except subprocess.TimeoutExpired:
            continue
        if out.exists() and out.stat().st_size > 0:
            return out
    raise MediaValidationError("サムネイル画像を生成できませんでした。動画ファイルが破損している可能性があります。")


# --- streaming uploads ----------------------------------------------------------------


class _AsyncUpload(Protocol):
    """The subset of ``fastapi.UploadFile`` used here."""

    file: IO[bytes]
    size: int | None


def _too_large(max_bytes: int) -> MediaValidationError:
    limit_mb = max_bytes / (1024 * 1024)
    shown = f"{limit_mb:.0f}" if limit_mb >= 1 else f"{limit_mb:.2f}"
    return MediaValidationError(
        f"ファイルサイズが上限（{shown} MB）を超えています。", status_code=413, code="file_too_large"
    )


def copy_stream_limited(
    src: IO[bytes], dest: str | os.PathLike[str], max_bytes: int, *, chunk_size: int = _CHUNK
) -> int:
    """Copy ``src`` to ``dest`` chunk by chunk, aborting once ``max_bytes`` is exceeded.

    Writes to a temporary sibling first; on any failure nothing is left at ``dest``.
    Raises :class:`MediaValidationError` with ``status_code=413`` when too large.
    """
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_path.with_name(f".{dest_path.name}.{uuid.uuid4().hex}.part")
    written = 0
    try:
        with tmp.open("wb") as out:
            while chunk := src.read(chunk_size):
                written += len(chunk)
                if written > max_bytes:
                    raise _too_large(max_bytes)
                out.write(chunk)
        os.replace(tmp, dest_path)
    finally:
        tmp.unlink(missing_ok=True)
    return written


def save_upload_file(upload_file: _AsyncUpload, dest: str | os.PathLike[str], max_bytes: int) -> int:
    """Blocking variant of :func:`save_upload_stream` for sync route handlers."""
    if upload_file.size is not None and upload_file.size > max_bytes:
        raise _too_large(max_bytes)
    src = upload_file.file
    src.seek(0)
    return copy_stream_limited(src, dest, max_bytes)


async def save_upload_stream(upload_file: _AsyncUpload, dest: str | os.PathLike[str], max_bytes: int) -> int:
    """Persist an ``UploadFile`` to ``dest`` enforcing ``max_bytes`` while copying.

    The copy runs in a worker thread in 1 MiB chunks and stops as soon as the
    limit is crossed. Returns the number of bytes written; raises
    :class:`MediaValidationError` (status 413) when the upload is too large.
    """
    return await anyio.to_thread.run_sync(save_upload_file, upload_file, dest, max_bytes)


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()
