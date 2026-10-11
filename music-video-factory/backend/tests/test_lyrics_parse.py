"""Tests for ``app.services.lyrics_parse`` (ARCHITECTURE.md §6.2): TXT/LRC/SRT parsing,
byte decoding (UTF-8 / BOM / Shift_JIS) and LRC/SRT export round trips."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from app.services.errors import LyricsParseError
from app.services.lyrics_parse import (
    decode_bytes,
    decode_bytes_with_info,
    detect_format,
    is_heading_line,
    parse_lyrics,
    to_lrc,
    to_srt,
)

LINE_KEYS = {"id", "text", "start", "end", "words", "source", "confidence"}


def texts(lines: list[Any]) -> list[str]:
    return [line["text"] for line in lines]


def times(lines: list[Any]) -> list[tuple[float | None, float | None]]:
    return [(line["start"], line["end"]) for line in lines]


# --------------------------------------------------------------------------- detect_format


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", "txt"),
        ("夜明け前の街を\n二行目", "txt"),
        ("[サビ]\n歌詞", "txt"),
        ("[Chorus: Someone]\n歌詞", "txt"),
        ("[00:12.00]歌詞", "lrc"),
        ("  [01:02]歌詞", "lrc"),
        ("\ufeff[ti:Song]\n歌詞", "lrc"),
        ("1\r\n00:00:01,000 --> 00:00:02,500\r\n歌詞\r\n", "srt"),
        ("WEBVTT\n\n00:01.000 --> 00:02.000\n歌詞\n", "srt"),
    ],
)
def test_detect_format(text: str, expected: str) -> None:
    assert detect_format(text) == expected


# --------------------------------------------------------------------------- TXT


def test_txt_preserves_japanese_text_exactly() -> None:
    text = (
        "\ufeff  夜明け前の街を ひとりで歩いた  \r\n"
        "\r\n"
        "\u3000消えそうな光を\u3000両手で包んで\u3000\r\n"
        "\n"
        " Hello,  world!  don't stop\t\n"
        "叫べ！ 今、この瞬間を…\n"
    )
    lines, warnings = parse_lyrics(text)
    assert texts(lines) == [
        "夜明け前の街を ひとりで歩いた",
        "消えそうな光を\u3000両手で包んで",  # internal full-width space kept
        "Hello,  world!  don't stop",  # double spaces kept
        "叫べ！ 今、この瞬間を…",
    ]
    assert [line["id"] for line in lines] == ["l001", "l002", "l003", "l004"]
    for line in lines:
        assert set(line) == LINE_KEYS
        assert line["start"] is None and line["end"] is None
        assert line["words"] is None and line["confidence"] is None
        assert line["source"] == "txt"
    assert warnings == []


def test_txt_heading_lines_are_not_lyric_lines() -> None:
    # Regression: headings were kept as lines, so auto-timing gave them vocal time (delaying
    # the next line by about a second) and they were burned into the video.
    text = (
        "[サビ]\n叫べ 今この瞬間を\n【Aメロ】\n夜明け前の街を\n[Chorus]\nShout it out\nVerse 1:\n"
        "＜間奏＞\n<Bridge>\nＡメロ：\nサビ 2:\nPre-Chorus:\n〔Cメロ〕\n(Oh oh)\n（ラララ）\nVerse 1: we walk alone\n"
    )
    lines, warnings = parse_lyrics(text)
    assert texts(lines) == [
        "叫べ 今この瞬間を", "夜明け前の街を", "Shout it out",
        "(Oh oh)", "（ラララ）",  # sung backing vocals stay
        "Verse 1: we walk alone",  # a label followed by lyrics is a lyric line
    ]  # fmt: skip
    assert [line["id"] for line in lines] == [f"l{i:03d}" for i in range(1, 7)]
    assert len(warnings) == 1
    assert warnings[0].startswith("見出しと思われる行 10 行（「[サビ]」「【Aメロ】」「[Chorus]」「Verse 1:」「＜間奏＞」 ほか 5 行）")
    assert "歌詞の本文はそのまま保存" in warnings[0]


@pytest.mark.parametrize(
    "line", ["[サビ]", "【Aメロ】", "[Chorus]", "[Verse 1: Someone]", "Verse 1:", "＜間奏＞", "<Chorus>", "サビ：",
             "Ｂメロ:", "大サビ:", "Hook:", "Pre-Chorus:", "2番:", "Chorus x2:", "《アウトロ》", "〈イントロ〉"],
)  # fmt: skip
def test_heading_lines_are_detected(line: str) -> None:
    assert is_heading_line(line)


@pytest.mark.parametrize(
    "line", ["(Oh oh)", "（ラララ）", "叫べ 今この瞬間を", "Verse 1: we walk alone", "サビ", "Chorus", "君と見た空を:",
             "Hello: world", "[サビ] 叫べ", "Oh, chorus of birds:"],
)  # fmt: skip
def test_lyric_lines_are_not_headings(line: str) -> None:
    assert not is_heading_line(line)


def test_auto_timing_ignores_headings(demo_dir: Path) -> None:
    # The heading used to take a slot of its own, so every line after it started late.
    from app.services.lyrics_timing import auto_time

    analysis = {"duration": 20.0, "vocal_activity": {"hop": 0.1, "values": ([1.0] * 30 + [0.0] * 10) * 5}}
    plain, _ = parse_lyrics("一行目の歌詞です\n二行目の歌詞です\n三行目の歌詞です\n")
    headed, _ = parse_lyrics("[Aメロ]\n一行目の歌詞です\n二行目の歌詞です\n【サビ】\n三行目の歌詞です\n")
    timed_plain, _ = auto_time(plain, analysis)
    timed_headed, _ = auto_time(headed, analysis)
    assert times(timed_headed) == times(timed_plain)


def test_empty_text_gives_no_lines() -> None:
    assert parse_lyrics("") == ([], [])
    assert parse_lyrics(" \r\n\u3000\n") == ([], [])


# --------------------------------------------------------------------------- LRC


def test_lrc_time_tag_variants_and_metadata() -> None:
    text = "[ti:曲名]\n[ar:アーティスト]\n[al:Album]\n[by:me]\n[length: 01:20]\n[00:01]一\n[00:02.5]二\n[00:03.25]三\n[00:04.125]四\n[01:05:50]五\n"
    lines, warnings = parse_lyrics(text)
    assert texts(lines) == ["一", "二", "三", "四", "五"]
    # 四 is followed by a 61 s gap: it ends after its (minimum) estimated sung time, not at 65.5 s
    assert times(lines) == [(1.0, 2.5), (2.5, 3.25), (3.25, 4.125), (4.125, 6.625), (65.5, 69.5)]
    assert all(line["source"] == "lrc" for line in lines)
    assert len(warnings) == 1 and "10 秒以上空く 1 行" in warnings[0] and "約 2.5 秒" in warnings[0]


def test_lrc_last_line_end_is_capped_by_length_tag() -> None:
    lines, _ = parse_lyrics("[length:00:31.00]\n[00:28.00]最後の行\n")
    assert times(lines) == [(28.0, 31.0)]


def test_lrc_multiple_tags_sorted_with_end_markers() -> None:
    text = "[00:30.00]サビ\n[00:10.00][00:50.00]繰り返し\n[00:20.00]二番\n[00:25.00]\n"
    lines, warnings = parse_lyrics(text)
    assert texts(lines) == ["繰り返し", "二番", "サビ", "繰り返し"]
    # the empty [00:25.00] line only ends 二番 (an explicit end); 繰り返し and サビ are 10 s and
    # 20 s away from the next line and end after their estimated sung time; the last line is held 4 s
    assert times(lines) == [(10.0, 12.5), (20.0, 25.0), (30.0, 32.5), (50.0, 54.0)]
    assert [line["id"] for line in lines] == ["l001", "l002", "l003", "l004"]
    assert any("10 秒以上空く 2 行" in w for w in warnings)


def test_lrc_same_timestamp_lines_warn_and_share_their_span() -> None:
    # Bilingual / duet LRC: the original and its translation share a timestamp.
    text = (
        "[00:00.50]叫べ 今この瞬間を\n[00:00.50]Shout out this very moment\n"
        "[00:02.50]忘れないように 胸に刻んで\n[00:02.52]So that we never forget it\n[00:06.50]次の行\n"
    )
    lines, warnings = parse_lyrics(text)
    assert times(lines)[:4] == [(0.5, 2.5), (0.5, 2.5), (2.5, 6.5), (2.52, 6.5)]
    assert [w for w in warnings if "同じ時刻の行" in w] == [
        "同じ時刻の行が 2 組あります（対訳・デュエットなど）。動画では上下に並べて表示します。"
    ]
    _, plain = parse_lyrics("[00:01.00]一\n[00:02.00]二\n[00:02.10]三\n")
    assert not any("同じ時刻" in w for w in plain)


@pytest.mark.parametrize(
    ("offset", "expected", "clamped"),
    [("+500", (9.5, 11.5), False), ("-250", (10.25, 12.25), False), ("+10500", (0.0, 1.5), True)],
)
def test_lrc_offset_is_applied(offset: str, expected: tuple[float, float], clamped: bool) -> None:
    lines, warnings = parse_lyrics(f"[offset:{offset}]\n[00:10.00]一\n[00:12.00]二\n")
    assert [line["start"] for line in lines] == list(expected)
    assert any("offset" in w for w in warnings)
    assert any("0 秒に補正" in w for w in warnings) is clamped


def test_lrc_enhanced_word_tags() -> None:
    text = "[00:12.00]<00:12.00>夜明け <00:12.80>前の<00:13.50>街を<00:14.20>\n[00:16.00]次の行\n"
    lines, _ = parse_lyrics(text)
    assert texts(lines) == ["夜明け 前の街を", "次の行"]
    assert lines[0]["words"] == [
        {"text": "夜明け", "start": 12.0, "end": 12.8},
        {"text": "前の", "start": 12.8, "end": 13.5},
        {"text": "街を", "start": 13.5, "end": 14.2},
    ]
    assert times(lines)[0] == (12.0, 16.0)
    assert lines[1]["words"] is None


def test_lrc_enhanced_words_without_closing_tag_end_with_the_line() -> None:
    lines, _ = parse_lyrics("[00:01.00]<00:01.00>Hello <00:01.50>world\n[00:03.00]next\n")
    assert lines[0]["words"] == [
        {"text": "Hello", "start": 1.0, "end": 1.5},
        {"text": "world", "start": 1.5, "end": 3.0},
    ]


def test_lrc_lines_without_tags_are_kept_untimed() -> None:
    lines, warnings = parse_lyrics("[00:01.00]一\n時刻なし\n[00:03.00]三\n")
    assert texts(lines) == ["一", "時刻なし", "三"]
    assert times(lines) == [(1.0, 3.0), (None, None), (3.0, 7.0)]
    assert any("時刻タグのない行が 1 行" in w for w in warnings)


def test_forced_lrc_without_time_tags() -> None:
    lines, warnings = parse_lyrics("一\n二\n", "lrc")
    assert texts(lines) == ["一", "二"]
    assert times(lines) == [(None, None), (None, None)]
    assert any("見つからなかった" in w for w in warnings)


def test_demo_lrc_matches_demo_txt(demo_dir: Path) -> None:
    lrc_lines, warnings = parse_lyrics((demo_dir / "demo_lyrics_ja.lrc").read_text(encoding="utf-8"))
    txt_lines, _ = parse_lyrics((demo_dir / "demo_lyrics_ja.txt").read_text(encoding="utf-8"))
    assert texts(lrc_lines) == texts(txt_lines)
    assert [line["start"] for line in lrc_lines] == [8.0, 12.0, 16.0, 20.0, 24.0, 28.0, 32.0, 36.0, 48.0, 52.0]
    # The line before the 40–48 s bridge is not held through it (it is sung 36–39.6 s).
    assert lrc_lines[7]["end"] == pytest.approx(41.5)
    assert all(line["end"] == lrc_lines[i + 1]["start"] for i, line in enumerate(lrc_lines[:7]))
    assert len(warnings) == 1 and "1 行" in warnings[0]


def test_lrc_line_before_an_instrumental_break_ends_after_it_is_sung() -> None:
    # Regression: without an end tag a line stayed on screen until the next line, even 20 s later.
    text = (
        "[00:10.00]夜明け前の街を ひとりで歩いた\n"  # 16.5 morae (kanji 1.5) → 0.3 s × 16.5 + 1 s = 5.95 s
        "[00:30.00]Shout it out loud tonight\n"  # 6 syllables → 2.8 s, next line 15 s later
        "[00:45.00]短い\n"  # 2.5 morae → the 2.5 s minimum, next line 12 s later
        "[00:57.00]次の行\n[00:61.00]最後\n"
    )
    lines, warnings = parse_lyrics(text)
    assert times(lines) == [(10.0, 15.95), (30.0, 32.8), (45.0, 47.5), (57.0, 61.0), (61.0, 65.0)]
    held = [w for w in warnings if "間奏" in w]
    assert held == [
        "次の行まで間奏などで 10 秒以上空く 3 行は、間奏中も表示され続けないよう、文字数から推定した歌唱時間"
        "（約 2.5〜6.0 秒）で表示を終えるようにしました。表示時間を変えたい場合は、終了時刻を手動で調整するか、"
        "LRC に終了の時刻タグ（歌詞のない [mm:ss.xx] の行）を入れてください。"
    ]


def test_lrc_explicit_end_tags_and_short_gaps_are_respected() -> None:
    # An explicit (empty) end tag is the author's choice, even 20 s later; ordinary gaps keep
    # the line until the next one; a long line is held for its whole estimate before a break.
    text = (
        "[00:10.00]夜明け前の街を ひとりで歩いた\n[00:30.00]\n"
        "[00:31.00]消えそうな光を 両手で包んで\n[00:40.00]名前も知らない 風に吹かれて\n"
        "[00:52.00]それでも僕らは 前を向いていた 叫べ 今この瞬間を 忘れないように\n[01:05.00]最後\n"
    )
    lines, warnings = parse_lyrics(text)
    assert times(lines)[:3] == [(10.0, 30.0), (31.0, 40.0), (40.0, 45.65)]  # 9 s gap: kept
    assert times(lines)[3] == (52.0, 60.0)  # long line: the 8 s maximum, then a 5 s break
    assert next(w for w in warnings if "間奏" in w).startswith("次の行まで間奏などで 10 秒以上空く 2 行は")
    assert parse_lyrics(to_lrc(lines))[0] == lines  # the shortened ends survive a round trip


def test_lrc_enhanced_words_and_same_time_groups_end_together_before_a_break() -> None:
    text = (
        "[00:10.00]<00:10.00>夜明け <00:13.00>前の <00:17.50>街を\n"  # sung until at least 17.5 s
        "[00:30.00]叫べ\n[00:30.00]Shout out this very moment and never let it go\n"
        "[00:50.00]次\n"
    )
    lines, _ = parse_lyrics(text)
    assert times(lines)[0] == (10.0, 18.5)  # last word start + 1 s, beyond the 3.7 s text estimate
    assert lines[0]["words"][-1] == {"text": "街を", "start": 17.5, "end": 18.5}
    assert times(lines)[1] == times(lines)[2] == (30.0, 34.9)  # the longer (translated) line decides


def test_lrc_heading_lines_are_dropped_and_do_not_hold_the_previous_line() -> None:
    text = "[00:10.00]一行目の歌詞です\n[00:30.00]【サビ】\n[00:31.00]サビの歌詞\n[Chorus]\n[00:35.00]最後\n"
    lines, warnings = parse_lyrics(text)
    assert texts(lines) == ["一行目の歌詞です", "サビの歌詞", "最後"]
    assert times(lines) == [(10.0, 14.15), (31.0, 35.0), (35.0, 39.0)]
    assert any("「【サビ】」「[Chorus]」" in w for w in warnings)


# --------------------------------------------------------------------------- SRT


def test_srt_bom_crlf_multiline_tags_and_order() -> None:
    text = (
        "\ufeff2\r\n00:00:05.500 --> 00:00:08.000\r\n二行目の\r\n字幕\r\n\r\n"
        "1\r\n00:00:01,000 --> 00:00:04,250\r\n<i>最初の</i>\u3000行\r\n\r\n"
        "3\r\n00:00:09,000 --> 00:00:10,000 X1:0 X2:10\r\nHello  world\r\n"
    )
    assert detect_format(text) == "srt"
    lines, warnings = parse_lyrics(text)
    assert texts(lines) == ["最初の\u3000行", "二行目の 字幕", "Hello  world"]
    assert times(lines) == [(1.0, 4.25), (5.5, 8.0), (9.0, 10.0)]
    assert [line["id"] for line in lines] == ["l001", "l002", "l003"]
    assert all(line["source"] == "srt" for line in lines)
    joined = " / ".join(warnings)
    assert "結合" in joined and "書式タグ" in joined and "並べ替え" in joined


def test_srt_without_blank_lines_or_hours() -> None:
    lines, _ = parse_lyrics("1\n00:01,000 --> 00:02,000\nA\n2\n00:03.5 --> 00:04\nB\n")
    assert texts(lines) == ["A", "B"]
    assert times(lines) == [(1.0, 2.0), (3.5, 4.0)]


def test_srt_text_after_blank_line_stays_with_its_cue() -> None:
    lines, warnings = parse_lyrics(
        "1\n00:00:01,000 --> 00:00:02,000\n一行目\n\n続き\n\n2\n00:00:03,000 --> 00:00:04,000\n二\n"
    )
    assert texts(lines) == ["一行目 続き", "二"]
    assert any("直前の字幕に含めました" in w for w in warnings)


def test_srt_bad_end_is_repaired() -> None:
    lines, warnings = parse_lyrics("1\n00:00:05,000 --> 00:00:04,000\nA\n\n2\n00:00:07,000 --> 00:00:09,000\nB\n")
    assert times(lines) == [(5.0, 7.0), (7.0, 9.0)]
    assert any("補正" in w for w in warnings)


def test_forced_srt_without_cues_raises() -> None:
    with pytest.raises(LyricsParseError):
        parse_lyrics("ただの歌詞\n二行目", "srt")


def test_unknown_format_raises() -> None:
    with pytest.raises(LyricsParseError):
        parse_lyrics("歌詞", "ass")


# --------------------------------------------------------------------------- decoding

JA_SAMPLE = "夜明け前の街を ひとりで歩いた\r\n～①髙\u3000テスト\n"  # ～ ① 髙 exist only in CP932


@pytest.mark.parametrize(
    ("data", "encoding"),
    [
        (JA_SAMPLE.encode("utf-8"), "utf-8"),
        (b"\xef\xbb\xbf" + JA_SAMPLE.encode("utf-8"), "utf-8-sig"),
        (JA_SAMPLE.encode("cp932"), "cp932"),
        (JA_SAMPLE.encode("utf-16"), "utf-16"),
    ],
)
def test_decode_bytes_encodings(data: bytes, encoding: str) -> None:
    info = decode_bytes_with_info(data)
    assert info.text == JA_SAMPLE
    assert info.encoding == encoding
    assert info.warnings == []
    assert decode_bytes(data) == JA_SAMPLE


def test_decode_plain_shift_jis() -> None:
    text = "消えそうな光を\u3000両手で包んで\n"
    assert decode_bytes(text.encode("shift_jis")) == text


@pytest.mark.parametrize("text", ["ありがとう\n", "叫べ\n", "夜明け前の街を ひとりで歩いた\nカタカナだけ\n"])
def test_decode_euc_jp(text: str) -> None:
    # Short kana-only EUC-JP is also valid CP932 (as half-width katakana mojibake).
    info = decode_bytes_with_info(text.encode("euc_jp"))
    assert info.text == text
    assert info.encoding == "euc_jp"


def test_decode_shift_jis_with_half_width_katakana_stays_cp932() -> None:
    text = "ｶﾞﾝﾊﾞﾚ 僕らの未来\n"
    info = decode_bytes_with_info(text.encode("cp932"))
    assert (info.text, info.encoding) == (text, "cp932")


def test_decode_undecodable_bytes_uses_replacement_with_warning() -> None:
    info = decode_bytes_with_info(b"abc \x81\x20 def")  # 0x81 0x20: invalid in UTF-8, CP932, Shift_JIS and EUC-JP
    assert info.encoding == "utf-8-replace"
    assert "�" in info.text and info.text.startswith("abc ")
    assert info.warnings


def test_decode_binary_raises() -> None:
    with pytest.raises(LyricsParseError):
        decode_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00")


def test_shift_jis_lrc_upload_end_to_end() -> None:
    data = "[ti:テスト]\r\n[00:08.00]夜明け前の街を\u3000ひとりで歩いた\r\n[00:12.00]消えそうな光を 両手で包んで\r\n".encode(
        "cp932"
    )
    lines, warnings = parse_lyrics(decode_bytes(data))
    assert warnings == []
    assert texts(lines) == ["夜明け前の街を\u3000ひとりで歩いた", "消えそうな光を 両手で包んで"]
    assert times(lines) == [(8.0, 12.0), (12.0, 16.0)]


# --------------------------------------------------------------------------- export & round trips


def sample_lines() -> list[dict[str, Any]]:
    return [
        {
            "id": "l001",
            "text": "夜明け前の街を ひとりで歩いた",
            "start": 8.0,
            "end": 11.6,
            "words": None,
            "source": "manual",
            "confidence": None,
        },
        {
            "id": "l002",
            "text": "消えそうな光を\u3000両手で包んで",
            "start": 12.0,
            "end": 16.0,
            "words": None,
            "source": "heuristic",
            "confidence": 0.3,
        },
        {
            "id": "l003",
            "text": "Hello, world! don't stop",
            "start": 65.25,
            "end": 70.5,
            "words": None,
            "source": "manual",
            "confidence": None,
        },
    ]


def test_to_lrc_format() -> None:
    out = to_lrc(sample_lines(), metadata={"ti": "曲名 [demo]", "ar": "アーティスト"})
    assert out == (
        "[ti:曲名 (demo)]\n"
        "[ar:アーティスト]\n"
        "[00:08.00]夜明け前の街を ひとりで歩いた\n"
        "[00:11.60]\n"
        "[00:12.00]消えそうな光を\u3000両手で包んで\n"
        "[00:16.00]\n"
        "[01:05.25]Hello, world! don't stop\n"
        "[01:10.50]\n"
    )


def test_to_srt_format() -> None:
    lines = sample_lines()
    lines.append(
        {
            "id": "l004",
            "text": "一時間後",
            "start": 3725.5,
            "end": None,
            "words": None,
            "source": "manual",
            "confidence": None,
        }
    )
    out = to_srt(lines)
    assert out == (
        "1\n00:00:08,000 --> 00:00:11,600\n夜明け前の街を ひとりで歩いた\n\n"
        "2\n00:00:12,000 --> 00:00:16,000\n消えそうな光を\u3000両手で包んで\n\n"
        "3\n00:01:05,250 --> 00:01:10,500\nHello, world! don't stop\n\n"
        "4\n01:02:05,500 --> 01:02:09,500\n一時間後\n"
    )


def test_lrc_round_trip_keeps_text_times_and_untimed_lines() -> None:
    lines = sample_lines()
    lines.insert(
        2,
        {
            "id": "x",
            "text": "時刻なしの行",
            "start": None,
            "end": None,
            "words": None,
            "source": "txt",
            "confidence": None,
        },
    )
    before = copy.deepcopy(lines)
    parsed, warnings = parse_lyrics(to_lrc(lines))
    assert lines == before  # export does not mutate its input
    assert texts(parsed) == texts(lines)
    assert times(parsed) == times(lines)
    assert any("時刻タグのない行" in w for w in warnings)


def test_lrc_round_trip_with_enhanced_words() -> None:
    lines, _ = parse_lyrics(
        "[00:12.00]<00:12.00>夜明け <00:12.80>前の<00:13.50>街を<00:14.20>\n[00:16.00]<00:16.00>Hello, <00:16.40>world\n"
    )
    exported = to_lrc(lines, include_words=True)
    assert "<00:12.80>" in exported
    again, _ = parse_lyrics(exported)
    assert again == lines
    plain, _ = parse_lyrics(to_lrc(lines))
    assert texts(plain) == texts(lines) and all(line["words"] is None for line in plain)


def test_srt_round_trip() -> None:
    lines = sample_lines()
    parsed, warnings = parse_lyrics(to_srt(lines))
    assert warnings == []
    assert texts(parsed) == texts(lines)
    assert times(parsed) == times(lines)


def test_srt_export_skips_untimed_lines_and_parse_round_trips_srt_input() -> None:
    source = "1\n00:00:01,234 --> 00:00:02,345\n一\n\n2\n00:00:03,000 --> 00:00:05,000\n二 行目\n"
    lines, _ = parse_lyrics(source)
    assert to_srt(lines) == source
    with_untimed = [*lines, {"id": "l9", "text": "未設定", "start": None, "end": None}]
    assert to_srt(with_untimed) == source
