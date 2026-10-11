"""Lyrics parsing and export for TXT / LRC / SRT (ARCHITECTURE.md §6.2).

The user's lyric text is the source of truth: a line's text is only stripped of
surrounding whitespace and line endings, never rewritten (internal spaces,
full-width spaces and punctuation are kept). Markup that belongs to the file
*format* (LRC time/word tags, SRT indexes, timecodes and ``<i>``-style tags) is
not lyric text and is removed. All times are absolute seconds in the song.

Line ids are stable and positional (``l001``, ``l002`` …, in time order for
LRC/SRT, in text order for TXT).
"""

from __future__ import annotations

import bisect
import itertools
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypedDict, cast

from app.services.errors import LyricsParseError

__all__ = [
    "DEFAULT_LAST_LINE_SEC",
    "DecodedText",
    "LyricLine",
    "LyricsFormat",
    "WordTiming",
    "copy_line",
    "decode_bytes",
    "decode_bytes_with_info",
    "detect_format",
    "line_id",
    "make_line",
    "parse_lyrics",
    "to_lrc",
    "to_srt",
]

LyricsFormat = Literal["txt", "lrc", "srt"]


class WordTiming(TypedDict):
    text: str
    start: float | None
    end: float | None


class LyricLine(TypedDict):
    id: str
    text: str
    start: float | None
    end: float | None
    words: list[WordTiming] | None
    source: str  # manual|lrc|srt|txt|heuristic|whisper
    confidence: float | None


DEFAULT_LAST_LINE_SEC = 4.0  # LRC has no end times: the last line is held this long
LONG_HOLD_WARN_SEC = 15.0
_TIME_DECIMALS = 3

_NEWLINES = re.compile(r"\r\n|[\r\n\u2028\u2029]")

# LRC: [mm:ss], [mm:ss.x], [mm:ss.xx], [mm:ss.xxx] (also the rarer [mm:ss:xx]).
_LRC_TIME = r"(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)"
_LRC_TIME_TAG = re.compile(rf"\[{_LRC_TIME}\]")
_LRC_LEADING_TAGS = re.compile(rf"^(?:\s*\[{_LRC_TIME}\])+")
_LRC_WORD_TAG = re.compile(rf"<{_LRC_TIME}>")
_LRC_META = re.compile(r"^\[([A-Za-z#][A-Za-z0-9_#-]*)\s*:(.*)\]$")
# Only these keys are treated as LRC metadata, so TXT headings such as
# "[Chorus: …]" are never swallowed.
_LRC_META_KEYS = frozenset(
    {"ti", "ar", "al", "au", "by", "length", "offset", "re", "ve", "la", "lr", "tool", "id", "#", "key", "language"}
)

# SRT/WebVTT timecode; hours and the fraction are optional to accept sloppy files.
_SRT_TC = r"(?:(\d{1,3}):)?(\d{1,2}):(\d{1,2})(?:[,.](\d{1,3}))?"
_SRT_CUE = re.compile(rf"^\s*{_SRT_TC}\s*-->\s*{_SRT_TC}(?:\s.*)?$")
_SRT_INDEX = re.compile(r"^\s*\d+\s*$")
_SRT_FORMAT_TAGS = re.compile(r"</?(?:i|b|u|s|font)(?:\s[^>]*)?>|\{\\[^}]*\}", re.IGNORECASE)

# A TXT line that is entirely a bracketed heading, e.g. "[サビ]" or "【Verse 1】".
_HEADING_LINE = re.compile(r"^(?:\[[^\[\]]{1,30}\]|【[^【】]{1,30}】)$")


# --------------------------------------------------------------------------- line helpers


def line_id(index: int) -> str:
    """Stable id for the ``index``-th (0-based) line: ``l001``, ``l002`` …"""
    return f"l{index + 1:03d}"


def _round_time(value: float | None) -> float | None:
    if value is None:
        return None
    return round(max(0.0, float(value)), _TIME_DECIMALS)


def make_line(
    index: int,
    text: str,
    start: float | None,
    end: float | None,
    *,
    source: str,
    words: list[WordTiming] | None = None,
    confidence: float | None = None,
) -> LyricLine:
    return LyricLine(
        id=line_id(index),
        text=text,
        start=_round_time(start),
        end=_round_time(end),
        words=words,
        source=source,
        confidence=confidence,
    )


def copy_line(line: Mapping[str, Any]) -> LyricLine:
    """Independent copy of a LyricLine-like mapping with every contract key present."""
    words = line.get("words")
    return LyricLine(
        id=str(line.get("id") or ""),
        text=str(line.get("text") or ""),
        start=_opt_float(line.get("start")),
        end=_opt_float(line.get("end")),
        words=[
            WordTiming(text=str(w.get("text") or ""), start=_opt_float(w.get("start")), end=_opt_float(w.get("end")))
            for w in words
            if isinstance(w, Mapping)
        ]
        if isinstance(words, list)
        else None,
        source=str(line.get("source") or "manual"),
        confidence=_opt_float(line.get("confidence")),
    )


def _opt_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _split_lines(text: str) -> list[str]:
    return _NEWLINES.split(text)


def _clean(text: str) -> str:
    """Strip surrounding whitespace (incl. full-width spaces) and stray BOMs only."""
    return text.strip().strip("\ufeff").strip()


# --------------------------------------------------------------------------- decoding


@dataclass(frozen=True)
class DecodedText:
    text: str
    encoding: str
    warnings: list[str]


def _japanese_plausibility(text: str) -> int:
    """Higher for real Japanese text; EUC-JP bytes read as CP932 become half-width katakana."""
    score = 0
    for ch in text:
        if "\uff61" <= ch <= "\uff9f" or "\ue000" <= ch <= "\uf8ff":
            score -= 2
        elif "\u3040" <= ch <= "\u30ff" or "\u4e00" <= ch <= "\u9fff":
            score += 1
    return score


def decode_bytes_with_info(data: bytes) -> DecodedText:
    """Decode an uploaded lyrics file and report the encoding used.

    Order: UTF-8 with BOM → UTF-16 with BOM → UTF-8 → CP932 (Windows superset of
    Shift_JIS) or EUC-JP, whichever reads as more plausible Japanese → UTF-8
    with replacement characters (warning). Raises ``LyricsParseError`` for
    binary data.
    """
    if data.startswith(b"\xef\xbb\xbf"):
        try:
            return DecodedText(data.decode("utf-8-sig"), "utf-8-sig", [])
        except UnicodeDecodeError:
            pass
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return DecodedText(data.decode("utf-16"), "utf-16", [])
        except UnicodeDecodeError:
            pass
    if b"\x00" in data:
        raise LyricsParseError("テキストファイルとして読み込めませんでした（バイナリデータが含まれています）")
    try:
        return DecodedText(data.decode("utf-8"), "utf-8", [])
    except UnicodeDecodeError:
        pass
    candidates: list[tuple[str, str]] = []
    for encoding in ("cp932", "euc_jp"):
        try:
            candidates.append((data.decode(encoding), encoding))
        except UnicodeDecodeError:
            continue
    if candidates:
        text, encoding = max(candidates, key=lambda item: _japanese_plausibility(item[0]))  # ties: CP932
        return DecodedText(text, encoding, [])
    return DecodedText(
        data.decode("utf-8", errors="replace"),
        "utf-8-replace",
        [
            "文字コードを判別できなかったため、読み込めない文字を「\ufffd」に置き換えました。UTF-8 で保存し直してください。"
        ],
    )


def decode_bytes(data: bytes) -> str:
    """Decode lyrics bytes (UTF-8 / UTF-8-BOM / Shift_JIS …); see ``decode_bytes_with_info``."""
    return decode_bytes_with_info(data).text


# --------------------------------------------------------------------------- format detection


def _is_lrc_meta(stripped: str) -> bool:
    match = _LRC_META.match(stripped)
    return match is not None and match.group(1).lower() in _LRC_META_KEYS


def detect_format(text: str) -> LyricsFormat:
    """``srt`` if any line is an SRT timecode, ``lrc`` if any line starts with an
    LRC time tag (or is LRC metadata such as ``[ti:…]``), else ``txt``."""
    lines = [line.strip().lstrip("\ufeff") for line in _split_lines(text)]
    if any(_SRT_CUE.match(line) for line in lines):
        return "srt"
    if any(_LRC_LEADING_TAGS.match(line) or _is_lrc_meta(line) for line in lines):
        return "lrc"
    return "txt"


def parse_lyrics(text: str, fmt: str = "auto") -> tuple[list[LyricLine], list[str]]:
    """Parse lyrics into LyricLines (+ Japanese warnings). ``fmt``: auto|txt|lrc|srt."""
    text = text.removeprefix("\ufeff")
    requested = (fmt or "auto").strip().lower()
    wanted = detect_format(text) if requested == "auto" else requested
    if wanted == "txt":
        return _parse_txt(text)
    if wanted == "lrc":
        return _parse_lrc(text)
    if wanted == "srt":
        return _parse_srt(text, explicit=requested == "srt")
    raise LyricsParseError(f"未対応の歌詞形式です: {fmt}（txt / lrc / srt のいずれかを指定してください）")


# --------------------------------------------------------------------------- TXT


def _parse_txt(text: str) -> tuple[list[LyricLine], list[str]]:
    lines: list[LyricLine] = []
    headings = 0
    for raw in _split_lines(text):
        cleaned = _clean(raw)
        if not cleaned:
            continue
        if _HEADING_LINE.match(cleaned):
            headings += 1
        lines.append(make_line(len(lines), cleaned, None, None, source="txt"))
    warnings: list[str] = []
    if headings:
        warnings.append(
            f"「[サビ]」のような見出しと思われる行が {headings} 行あります。"
            "歌詞として動画に表示されるため、不要な場合は削除してください。"
        )
    return lines, warnings


# --------------------------------------------------------------------------- LRC


def _lrc_seconds(minutes: str, rest: str) -> float:
    sec_part, _, frac = rest.replace(":", ".").partition(".")
    value = int(minutes) * 60 + int(sec_part)
    if frac:
        value += int(frac) / 10 ** len(frac)
    return float(value)


@dataclass
class _LrcEntry:
    key: float  # sort key (start time, or the preceding line's time for untimed lines)
    seq: int  # file order
    text: str
    start: float | None
    words: list[WordTiming] | None


def _parse_enhanced(body: str) -> tuple[str, list[WordTiming] | None]:
    """Split ``<mm:ss.xx>`` word tags off a line body. Word times are raw (no offset)."""
    tags = list(_LRC_WORD_TAG.finditer(body))
    if not tags:
        return body, None
    parts = [body[: tags[0].start()]]
    words: list[WordTiming] = []
    lead = parts[0].strip()
    if lead:
        words.append(WordTiming(text=lead, start=None, end=_lrc_seconds(tags[0].group(1), tags[0].group(2))))
    for index, tag in enumerate(tags):
        t = _lrc_seconds(tag.group(1), tag.group(2))
        if words and words[-1]["end"] is None:
            words[-1]["end"] = t
        segment = body[tag.end() : tags[index + 1].start() if index + 1 < len(tags) else len(body)]
        parts.append(segment)
        if segment.strip():
            words.append(WordTiming(text=segment.strip(), start=t, end=None))
    return "".join(parts), words or None


def _parse_lrc(text: str) -> tuple[list[LyricLine], list[str]]:
    raw_lines = _split_lines(text)
    warnings: list[str] = []

    offset_ms = 0
    length: float | None = None
    for raw in raw_lines:
        stripped = raw.strip()
        if _LRC_LEADING_TAGS.match(stripped) or not _is_lrc_meta(stripped):
            continue
        match = _LRC_META.match(stripped)
        assert match is not None
        key, value = match.group(1).lower(), match.group(2).strip()
        if key == "offset" and offset_ms == 0:
            try:
                offset_ms = int(value.replace(" ", ""))
            except ValueError:
                warnings.append(f"LRC の offset 指定「{value}」を解釈できなかったため無視しました。")
        elif key == "length":
            time_match = re.fullmatch(_LRC_TIME, value)
            if time_match:
                length = _lrc_seconds(time_match.group(1), time_match.group(2))
    # A positive [offset:] makes lyrics appear sooner (LRC convention).
    shift = -offset_ms / 1000.0

    entries: list[_LrcEntry] = []
    last_key = -math.inf
    untagged = 0
    clamped = False
    for seq, raw in enumerate(raw_lines):
        stripped = _clean(raw)
        if not stripped:
            continue
        lead = _LRC_LEADING_TAGS.match(stripped)
        if lead is None:
            if _is_lrc_meta(stripped):
                continue
            untagged += 1
            entries.append(_LrcEntry(key=last_key, seq=seq, text=stripped, start=None, words=None))
            continue
        times = list(dict.fromkeys(_lrc_seconds(m.group(1), m.group(2)) for m in _LRC_TIME_TAG.finditer(lead.group(0))))
        body, raw_words = _parse_enhanced(stripped[lead.end() :])
        line_text = _clean(body)
        for t in times:
            start = t + shift
            if start < 0:
                clamped = True
                start = 0.0
            words: list[WordTiming] | None = None
            if raw_words:
                # Word tags are absolute for the first time tag; repeats are shifted along.
                delta = (t - times[0]) + shift
                words = [
                    WordTiming(
                        text=w["text"],
                        start=None if w["start"] is None else max(0.0, w["start"] + delta),
                        end=None if w["end"] is None else max(0.0, w["end"] + delta),
                    )
                    for w in raw_words
                ]
            entries.append(_LrcEntry(key=start, seq=seq, text=line_text, start=start, words=words))
        last_key = min(times) + shift

    entries.sort(key=lambda e: (e.key, e.seq))
    timed_starts = [e.start for e in entries if e.start is not None]

    lines: list[LyricLine] = []
    long_holds = 0
    pointer = 0  # first timed entry whose start is > the current start
    for entry in entries:
        if not entry.text:
            continue  # empty timed lines only mark the end of the previous line
        if entry.start is None:
            lines.append(make_line(len(lines), entry.text, None, None, source="lrc"))
            continue
        while pointer < len(timed_starts) and timed_starts[pointer] <= entry.start:
            pointer += 1
        if pointer < len(timed_starts):
            end = timed_starts[pointer]
            if end - entry.start > LONG_HOLD_WARN_SEC:
                long_holds += 1
        else:
            end = entry.start + DEFAULT_LAST_LINE_SEC
            last_word_end = entry.words[-1]["end"] if entry.words else None
            if last_word_end is not None and last_word_end > end:
                end = last_word_end
            if length is not None and entry.start < length < end:
                end = length
        words = entry.words
        if words:
            for index, word in enumerate(words):
                if word["start"] is None:
                    word["start"] = entry.start
                if word["end"] is None:
                    word["end"] = words[index + 1]["start"] if index + 1 < len(words) else end
                word["start"] = _round_time(word["start"])
                word["end"] = _round_time(word["end"])
        lines.append(make_line(len(lines), entry.text, entry.start, end, source="lrc", words=words))

    if untagged:
        if not timed_starts:
            warnings.append(
                "LRC の時刻タグ（[mm:ss.xx]）が見つからなかったため、すべての行を時刻未設定として読み込みました。"
            )
        else:
            warnings.append(f"時刻タグのない行が {untagged} 行あったため、時刻未設定として読み込みました。")
    if offset_ms:
        warnings.append(f"LRC の offset（{offset_ms:+d} ms）を適用しました。")
    if clamped:
        warnings.append("offset 適用後に 0 秒より前になった時刻を 0 秒に補正しました。")
    if long_holds:
        warnings.append(
            f"次の行まで {LONG_HOLD_WARN_SEC:.0f} 秒以上表示され続ける行が {long_holds} 行あります。"
            "間奏で表示を消したい場合は終了時刻を調整してください。"
        )
    return lines, warnings


# --------------------------------------------------------------------------- SRT


def _srt_seconds(hours: str | None, minutes: str, seconds: str, frac: str | None) -> float:
    value = int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
    if frac:
        value += int(frac) / 10 ** len(frac)
    return float(value)


@dataclass
class _SrtCue:
    seq: int
    start: float
    end: float
    texts: list[str]


def _parse_srt(text: str, *, explicit: bool) -> tuple[list[LyricLine], list[str]]:
    raw_lines = _split_lines(text)
    cues: list[_SrtCue] = []
    stray_attached = 0
    stray_ignored = 0
    index = 0
    while index < len(raw_lines):
        line = raw_lines[index]
        match = _SRT_CUE.match(line)
        if match is None:
            stripped = _clean(line)
            is_index = (
                bool(_SRT_INDEX.match(line)) and index + 1 < len(raw_lines) and _SRT_CUE.match(raw_lines[index + 1])
            )
            if stripped and not is_index and not (index == 0 and stripped.upper().startswith("WEBVTT")):
                # Text after a blank line inside a cue: keep it with that cue rather than lose lyrics.
                if cues:
                    cues[-1].texts.append(stripped)
                    stray_attached += 1
                else:
                    stray_ignored += 1
            index += 1
            continue
        groups = match.groups()
        cue = _SrtCue(seq=len(cues), start=_srt_seconds(*groups[:4]), end=_srt_seconds(*groups[4:]), texts=[])
        index += 1
        while index < len(raw_lines) and raw_lines[index].strip():
            if _SRT_CUE.match(raw_lines[index]):
                break
            if (
                _SRT_INDEX.match(raw_lines[index])
                and index + 1 < len(raw_lines)
                and _SRT_CUE.match(raw_lines[index + 1])
            ):
                break  # next block without a separating blank line
            cue.texts.append(_clean(raw_lines[index]))
            index += 1
        cues.append(cue)

    if not cues and explicit and any(_clean(line) for line in raw_lines):
        raise LyricsParseError(
            "SRT として解析できる字幕ブロック（00:00:01,000 --> 00:00:04,000 の形式）が見つかりません。"
        )

    warnings: list[str] = []
    joined = 0
    tags_removed = 0
    bad_ends = 0
    timed: list[tuple[float, int, str, float | None]] = []
    for cue in cues:
        parts: list[str] = []
        for part in cue.texts:
            without_tags = _clean(_SRT_FORMAT_TAGS.sub("", part))
            if without_tags != part:
                tags_removed += 1
            if without_tags:
                parts.append(without_tags)
        if not parts:
            continue
        if len(parts) > 1:
            joined += 1
        end: float | None = cue.end
        if cue.end <= cue.start:
            bad_ends += 1
            end = None
        timed.append((cue.start, cue.seq, " ".join(parts), end))

    out_of_order = any(b[0] < a[0] for a, b in itertools.pairwise(timed))
    timed.sort(key=lambda item: (item[0], item[1]))
    starts = [item[0] for item in timed]
    lines: list[LyricLine] = []
    for start, _seq, line_text, end in timed:
        if end is None:
            later = bisect.bisect_right(starts, start)
            end = starts[later] if later < len(starts) else start + DEFAULT_LAST_LINE_SEC
        lines.append(make_line(len(lines), line_text, start, end, source="srt"))

    if joined:
        warnings.append(f"複数行の字幕 {joined} 件を、スペース区切りで1行に結合しました。")
    if tags_removed:
        warnings.append("SRT の書式タグ（<i> など）を除去しました。")
    if out_of_order:
        warnings.append("字幕ブロックが時刻順に並んでいなかったため、時刻順に並べ替えました。")
    if bad_ends:
        warnings.append(f"終了時刻が開始時刻以前になっている字幕 {bad_ends} 件の終了時刻を補正しました。")
    if stray_attached:
        warnings.append(f"字幕ブロック内の空行の後にあった {stray_attached} 行を、直前の字幕に含めました。")
    if stray_ignored:
        warnings.append(f"最初の字幕ブロックより前にある {stray_ignored} 行を無視しました。")
    return lines, warnings


# --------------------------------------------------------------------------- export


def _single_line(text: str) -> str:
    return " ".join(_NEWLINES.split(text)).strip()


def _fmt_lrc_time(seconds: float) -> str:
    cs = max(0, round(seconds * 100))
    minutes, cs = divmod(cs, 6000)
    secs, cs = divmod(cs, 100)
    return f"{minutes:02d}:{secs:02d}.{cs:02d}"


def _fmt_srt_time(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    secs, ms = divmod(ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _ordered_for_export(lines: Sequence[Mapping[str, Any]]) -> list[LyricLine]:
    """Time order; untimed lines stay right after the line that preceded them."""
    keyed: list[tuple[float, int, LyricLine]] = []
    last_key = -math.inf
    for seq, raw in enumerate(lines):
        line = copy_line(raw)
        line["text"] = _single_line(line["text"])
        if not line["text"]:
            continue
        if line["start"] is not None:
            last_key = line["start"]
        keyed.append((last_key, seq, line))
    keyed.sort(key=lambda item: (item[0], item[1]))
    return [line for *_, line in keyed]


def _enhanced_body(line: LyricLine) -> str | None:
    """Line text with ``<mm:ss.xx>`` word tags, or None when words don't map onto the text."""
    words = [w for w in (line["words"] or []) if w["text"].strip()]
    if not words or any(w["start"] is None for w in words):
        return None
    text = line["text"]
    positions: list[int] = []
    cursor = 0
    for word in words:
        found = text.find(word["text"].strip(), cursor)
        if found < 0:
            return None
        positions.append(found)
        cursor = found + len(word["text"].strip())
    pieces = [text[: positions[0]]]
    for index, (word, pos) in enumerate(zip(words, positions, strict=True)):
        nxt = positions[index + 1] if index + 1 < len(positions) else len(text)
        pieces.append(f"<{_fmt_lrc_time(cast(float, word['start']))}>{text[pos:nxt]}")
    last_end = words[-1]["end"]
    if last_end is not None:
        pieces.append(f"<{_fmt_lrc_time(last_end)}>")
    return "".join(pieces)


def to_lrc(
    lines: Sequence[Mapping[str, Any]],
    *,
    include_words: bool = False,
    metadata: Mapping[str, str] | None = None,
) -> str:
    """Export as LRC (``[mm:ss.xx]text``).

    An empty ``[mm:ss.xx]`` tag is written where a line ends before the next one
    starts (and after the last line), so end times survive a round trip. Untimed
    lines are written without a tag. ``include_words`` writes enhanced-LRC
    ``<mm:ss.xx>`` word tags for lines that have word timings.
    """
    out: list[str] = []
    for key, value in (metadata or {}).items():
        clean_key = re.sub(r"[^A-Za-z#]", "", key)
        clean_value = _single_line(str(value)).replace("[", "(").replace("]", ")")
        if clean_key and clean_value:
            out.append(f"[{clean_key}:{clean_value}]")
    ordered = _ordered_for_export(lines)
    starts = [line["start"] for line in ordered if line["start"] is not None]
    for line in ordered:
        start = line["start"]
        if start is None:
            out.append(line["text"])
            continue
        body = (_enhanced_body(line) if include_words else None) or line["text"]
        out.append(f"[{_fmt_lrc_time(start)}]{body}")
        later = bisect.bisect_right(starts, start)
        next_start = starts[later] if later < len(starts) else None
        end = line["end"]
        if end is not None and end > start and (next_start is None or end < next_start - 0.005):
            out.append(f"[{_fmt_lrc_time(end)}]")
    return "\n".join(out) + "\n" if out else ""


def to_srt(lines: Sequence[Mapping[str, Any]]) -> str:
    """Export timed lines as SRT (untimed lines cannot be represented and are skipped)."""
    timed = [line for line in _ordered_for_export(lines) if line["start"] is not None]
    starts = [cast(float, line["start"]) for line in timed]
    blocks: list[str] = []
    for line in timed:
        start = cast(float, line["start"])
        end = line["end"]
        if end is None or end <= start:
            later = bisect.bisect_right(starts, start)
            end = starts[later] if later < len(starts) else start + DEFAULT_LAST_LINE_SEC
        blocks.append(f"{len(blocks) + 1}\n{_fmt_srt_time(start)} --> {_fmt_srt_time(end)}\n{line['text']}\n")
    return "\n".join(blocks)
