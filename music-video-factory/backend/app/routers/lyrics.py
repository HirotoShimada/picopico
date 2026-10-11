"""Lyrics: text/file input, manual line edits, auto-timing and LRC/SRT export (ARCHITECTURE.md §5.4).

The user's text is stored verbatim (``lyrics_text``) and never rewritten;
``lyric_lines`` carry the timing used for rendering.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, Query, UploadFile
from fastapi.responses import Response

from app.config import get_settings
from app.db import DbSession
from app.models import Project, utcnow
from app.pipeline import retime_line
from app.schemas import (
    AutoTimeOut,
    AutoTimeRequest,
    LyricLinesOut,
    LyricLinesUpdate,
    LyricsOut,
    LyricsParseOut,
    LyricsUpdate,
)
from app.security import CurrentUser, get_owned_project
from app.services.errors import MediaValidationError, ServiceError, WhisperUnavailable
from app.services.lyrics_parse import decode_bytes_with_info, detect_format, parse_lyrics, to_lrc, to_srt
from app.services.lyrics_timing import auto_time
from app.services.media import check_extension, sanitize_filename
from app.storage import build_content_disposition, get_storage

router = APIRouter(tags=["lyrics"])

LYRICS_EXTENSIONS: frozenset[str] = frozenset({".txt", ".lrc", ".srt"})
MAX_LYRICS_BYTES = 1024 * 1024
_FORMAT_BY_EXTENSION = {".lrc": "lrc", ".srt": "srt", ".txt": "auto"}
_WORD_SPAN_TOLERANCE = 0.05


def _parse_and_store(project: Project, text: str, fmt: str, warnings: list[str]) -> LyricsParseOut:
    resolved = detect_format(text) if fmt == "auto" else fmt
    lines, parse_warnings = parse_lyrics(text, resolved)
    project.lyrics_text = text
    project.lyrics_format = resolved
    project.lyric_lines = [dict(line) for line in lines]
    project.updated_at = utcnow()
    return LyricsParseOut(
        format=resolved,  # type: ignore[arg-type]
        lines=project.lyric_lines,  # type: ignore[arg-type]
        warnings=list(dict.fromkeys([*warnings, *parse_warnings])),
    )


@router.put("/projects/{project_id}/lyrics", response_model=LyricsParseOut)
def put_lyrics(project_id: str, body: LyricsUpdate, db: DbSession, user: CurrentUser) -> LyricsParseOut:
    project = get_owned_project(db, project_id, user)
    out = _parse_and_store(project, body.text, body.format, [])
    db.commit()
    return out


@router.post("/projects/{project_id}/lyrics/upload", response_model=LyricsParseOut)
def upload_lyrics(
    project_id: str,
    file: Annotated[UploadFile, File(description="歌詞ファイル（.txt / .lrc / .srt、1 MB まで）")],
    db: DbSession,
    user: CurrentUser,
) -> LyricsParseOut:
    project = get_owned_project(db, project_id, user)
    filename = sanitize_filename(file.filename, default="lyrics.txt")
    ext = check_extension(filename, LYRICS_EXTENSIONS, "歌詞ファイル")
    file.file.seek(0)
    data = file.file.read(MAX_LYRICS_BYTES + 1)
    if len(data) > MAX_LYRICS_BYTES:
        raise MediaValidationError("歌詞ファイルが上限（1 MB）を超えています。", status_code=413, code="file_too_large")
    decoded = decode_bytes_with_info(data)
    out = _parse_and_store(project, decoded.text, _FORMAT_BY_EXTENSION[ext], list(decoded.warnings))
    db.commit()
    return out


@router.get("/projects/{project_id}/lyrics", response_model=LyricsOut)
def get_lyrics(project_id: str, db: DbSession, user: CurrentUser) -> LyricsOut:
    project = get_owned_project(db, project_id, user)
    return LyricsOut(
        text=project.lyrics_text or "",
        format=project.lyrics_format or "txt",  # type: ignore[arg-type]
        lines=list(project.lyric_lines or []),  # type: ignore[arg-type]
    )


def _words_match(text: str, words: list[dict[str, Any]] | None) -> bool:
    if not words:
        return False
    joined = "".join(str(w.get("text") or "") for w in words)
    return "".join(joined.split()) == "".join(text.split())


@router.put("/projects/{project_id}/lyrics/lines", response_model=LyricLinesOut)
def put_lyric_lines(project_id: str, body: LyricLinesUpdate, db: DbSession, user: CurrentUser) -> LyricLinesOut:
    """Replace the timed lines (manual edits).

    Karaoke word timings follow an edited line (rescaled from the words' own
    span to the new line span) and are dropped when the line text no longer
    matches them.
    """
    project = get_owned_project(db, project_id, user)
    lines: list[dict[str, Any]] = []
    for line in body.lines:
        data = line.model_dump(mode="json")
        words = data.get("words")
        start, end = data.get("start"), data.get("end")
        if words and not _words_match(data["text"], words):
            data["words"] = None
        elif words and start is not None and end is not None and end > start:
            first, last = words[0].get("start"), words[-1].get("end")
            if (
                first is not None
                and last is not None
                and last > first
                and (abs(first - start) > _WORD_SPAN_TOLERANCE or abs(last - end) > _WORD_SPAN_TOLERANCE)
            ):
                source = data["source"]
                data = retime_line({**data, "start": first, "end": last}, start, end)
                data["source"] = source
        lines.append(data)
    project.lyric_lines = lines
    project.updated_at = utcnow()
    db.commit()
    return LyricLinesOut(lines=project.lyric_lines)  # type: ignore[arg-type]


@router.post("/projects/{project_id}/lyrics/auto-time", response_model=AutoTimeOut)
def auto_time_lyrics(project_id: str, body: AutoTimeRequest, db: DbSession, user: CurrentUser) -> AutoTimeOut:
    project = get_owned_project(db, project_id, user)
    current = list(project.lyric_lines or [])
    if not current:
        raise ServiceError("歌詞がありません。先に歌詞を入力してください。", status_code=422, code="lyrics_missing")
    analysis = project.analysis if project.analysis_status == "done" else None
    if body.method == "whisper":
        lines, warnings = _whisper_align(project, current, analysis, body)
    else:
        lines, warnings = auto_time(current, analysis, body.start, body.end, body.overwrite_timed)
    project.lyric_lines = [dict(line) for line in lines]
    project.updated_at = utcnow()
    db.commit()
    return AutoTimeOut(
        lines=project.lyric_lines,  # type: ignore[arg-type]
        method_used=body.method,
        warnings=list(dict.fromkeys(warnings)),
    )


def _whisper_align(
    project: Project, current: list[dict[str, Any]], analysis: dict[str, Any] | None, body: AutoTimeRequest
) -> tuple[list[Any], list[str]]:
    from app.services.lyrics_align import align_lines, whisper_available

    available, reason = whisper_available()
    if not available:
        raise WhisperUnavailable(
            reason or "Whisper モデルを読み込めないため、音声認識による自動タイミングは使えません。"
        )
    audio = project.audio_asset
    if audio is None:
        raise ServiceError("先に楽曲をアップロードしてください。", status_code=422, code="audio_missing")
    # Heuristic timing first, so lines Whisper cannot match keep a sensible position.
    lines, warnings = auto_time(current, analysis, body.start, body.end, body.overwrite_timed)
    storage = get_storage()
    path = storage.local_path(audio.storage_key)
    tmp_dir: Path | None = None
    if path is None:
        tmp_dir = get_settings().WORK_DIR / "whisper" / project.id
        path = storage.fetch_to(audio.storage_key, tmp_dir / f"audio{Path(audio.filename).suffix.lower()}")
    try:
        aligned, align_warnings = align_lines(str(path), lines, start=body.start, end=body.end)
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    return aligned, [*warnings, *align_warnings]


@router.get("/projects/{project_id}/lyrics/export")
def export_lyrics(
    project_id: str,
    db: DbSession,
    user: CurrentUser,
    format: Annotated[Literal["lrc", "srt"], Query()] = "lrc",
) -> Response:
    project = get_owned_project(db, project_id, user)
    lines = list(project.lyric_lines or [])
    if format == "lrc":
        metadata = {"ti": project.title, "ar": project.artist_name}
        text = to_lrc(lines, metadata=metadata)
    else:
        text = to_srt(lines)
    filename = sanitize_filename(f"{project.artist_name}_{project.title}.{format}", default=f"lyrics.{format}")
    return Response(
        content=text.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": build_content_disposition(filename)},
    )
