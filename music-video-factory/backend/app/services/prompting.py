"""Prompt safety and Japanese→English translation (ARCHITECTURE.md §6.3).

``sanitize_prompt`` is applied to every *positive* video prompt before it is
sent to a provider: it removes the artist's name and real-person / celebrity /
look-alike phrasing (English and Japanese), and always appends the
likeness-safety and no-text clauses. Negative prompts are not passed through it
(they legitimately contain words such as "celebrity likeness").

``translate_to_english`` uses Claude when ``ANTHROPIC_API_KEY`` is configured
and otherwise a built-in glossary of common scene/mood words (best effort).
"""

from __future__ import annotations

import functools
import logging
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "FALLBACK_BETA",
    "GLOSSARY_JA_EN",
    "LIKENESS_SAFETY_CLAUSE",
    "MAX_PROMPT_CHARS",
    "NO_TEXT_CLAUSE",
    "PersonReferences",
    "artist_name_variants",
    "detect_person_names",
    "first_text_block",
    "glossary_keywords",
    "glossary_translate",
    "make_claude_client",
    "mood_keywords",
    "person_reference_warnings",
    "prompt_body_budget",
    "sanitize_prompt",
    "screen_free_text",
    "strip_forbidden_names",
    "strip_person_references",
    "translate_to_english",
]

LIKENESS_SAFETY_CLAUSE = "fictional characters only, no real or identifiable people, no celebrities"
NO_TEXT_CLAUSE = "no text, no captions, no watermark"
_SAFETY_SUFFIX = f"{LIKENESS_SAFETY_CLAUSE}, {NO_TEXT_CLAUSE}."

# Runway's promptText is limited to 1000 characters (docs/VIDEO_PROVIDERS.md);
# Kling allows 2500 and Veo more, so 1000 is the common ceiling.
MAX_PROMPT_CHARS = 1000

# Server-side refusal fallback ("default" routing) for claude-opus-5-5 requests.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# --- forbidden names & real-person phrasing --------------------------------------------

_NAME_SEPARATORS = r"[\s・･\-_.·,、]*"
_ASCII_WORD = re.compile(r"^[A-Za-z0-9]+$")

# Already-negated safety phrases (e.g. "no real people" written by Claude) are
# removed silently: the canonical clause is appended again at the end.
_NEGATED_SAFETY = re.compile(
    r"\b(?:no|not|never|without|avoid)\s+(?:any\s+)?(?:"
    r"real(?:[\s-]life)?\s+(?:or\s+identifiable\s+)?(?:persons?|people|humans?)"
    r"|identifiable\s+(?:persons?|people|faces?)"
    r"|celebrit(?:y|ies)(?:\s+likeness(?:es)?)?"
    r"|look[\s-]?alikes?"
    r"|text|captions?|subtitles?|watermarks?|logos?|letters?|typography"
    r")\b",
    re.IGNORECASE,
)
_CANONICAL_CLAUSES = re.compile(
    "|".join(
        re.escape(clause)
        for clause in (
            LIKENESS_SAFETY_CLAUSE,
            NO_TEXT_CLAUSE,
            "fictional characters only",
        )
    ),
    re.IGNORECASE,
)

_REAL_PERSON_EN = re.compile(
    r"\b(?:"
    r"celebrit(?:y|ies)"
    r"|famous\s+(?:persons?|people|singers?|actors?|actress(?:es)?|idols?|stars?|musicians?|artists?|faces?|rappers?)"
    r"|look[\s-]?alikes?"
    r"|doppelg(?:a|ä)ngers?"
    r"|real(?:[\s-]life)?\s+(?:persons?|people|humans?|individuals?)"
    r"|actual\s+(?:persons?|people)"
    r"|(?:identifiable|recognizable|recognisable)\s+(?:persons?|people|faces?|individuals?)"
    r"|impersonat(?:e|es|ed|ing|ion|ors?)"
    r"|deep[\s-]?fakes?"
    r"|(?:in\s+the\s+)?likeness\s+of"
    r"|(?:the\s+)?(?:artist|singer|band|musician|vocalist|rapper|idol)\s+(?:him|her|them|it)sel(?:f|ves)"
    r"|(?:the\s+)?(?:artist|singer|vocalist)'s\s+(?:own\s+)?(?:face|likeness)"
    r"|paparazzi"
    r")\b",
    re.IGNORECASE,
)
# 「本人」 but not 「日本人」.
_REAL_PERSON_JA = re.compile(
    r"(?<!日)ご?本人|芸能人|有名人|著名人|セレブ|そっくり|激似|瓜二つ|生き写し|似顔絵|肖像"
    r"|実在(?:の|する)?人物|実在の人|実在する人|モノマネ|ものまね|物真似|ディープフェイク"
)


def _name_pattern(name: str) -> re.Pattern[str] | None:
    normalized = unicodedata.normalize("NFKC", name).strip()
    tokens = [t for t in re.split(r"[\s・･\-_.·]+", normalized) if t]
    if not tokens or len("".join(tokens)) < 2:
        return None
    body = _NAME_SEPARATORS.join(re.escape(t) for t in tokens)
    # Word boundaries only make sense for Latin script; CJK names are matched anywhere.
    if _ASCII_WORD.match(tokens[0]):
        body = r"(?<![A-Za-z0-9])" + body
    if _ASCII_WORD.match(tokens[-1]):
        body = body + r"(?![A-Za-z0-9])"
    return re.compile(body, re.IGNORECASE)


def strip_forbidden_names(text: str, forbidden_names: Iterable[str]) -> tuple[str, list[str]]:
    """Remove every forbidden name (spacing/case/width-insensitive). Returns (text, removed names)."""
    text = unicodedata.normalize("NFKC", text or "")
    removed: list[str] = []
    for name in forbidden_names:
        if not name or not name.strip():
            continue
        pattern = _name_pattern(name)
        if pattern is None:
            continue
        text, count = pattern.subn(" ", text)
        if count:
            removed.append(name.strip())
    return _tidy(text), removed


# --- look-alike phrasing and probable real-person names in free text ----------------------------
#
# The artist's own name is forbidden by spelling (above). Users also type other
# people into free text — "Kenshi Yonezu singing on a rooftop", "a man who looks
# exactly like Keanu Reeves", "Billie Eilish vibes", "木村拓哉似の男性" — so
# ``strip_person_references`` removes (1) look-alike constructions together with
# the person they name and (2) Latin-script runs of capitalised words that are
# not known vocabulary (glossary, presets, common words, place names). Single
# names ("Aimyon") are only caught in a look-alike construction or as a
# forbidden name/alias (kana artist names also get their romaji spellings, see
# ``artist_name_variants``).

_CAP_WORD = r"[A-Z][A-Za-z0-9'’]*"
_CAP_WORD_HYPHEN = rf"{_CAP_WORD}(?:-[A-Za-z0-9'’]+)*"
_CAP_RUN = re.compile(rf"(?<![A-Za-z0-9'’\-]){_CAP_WORD_HYPHEN}(?:[ \t]+{_CAP_WORD_HYPHEN})+(?![A-Za-z0-9'’])")
_NAME_RUN = rf"(?<![A-Za-z0-9'’\-]){_CAP_WORD}(?:[ \t]+{_CAP_WORD}){{0,3}}"
_JA_NAME_RUN = (
    r"(?:[\u3400-\u4DBF\u4E00-\u9FFF\u30A0-\u30FF\uFF66-\uFF9Fー・]{1,20}"
    r"|[A-Za-z][A-Za-z0-9'’.\-]*(?:[ \t]+[A-Za-z][A-Za-z0-9'’.\-]*){0,3})"
)

# 「木村拓哉似の男性」「米津玄師に似た人」「〇〇そっくりな」「〇〇風の顔」: marker plus the name before it.
_RESEMBLE_JA = re.compile(
    rf"(?P<name>{_JA_NAME_RUN})?[ \t]*(?P<marker>"
    r"に?(?:そっくり|激似|瓜二つ|生き写し)(?:な|の)?"
    r"|に似(?:た|せた|て(?:い)?る)|(?<![類疑相近酷])似の"
    r"|風の顔(?:立ち)?|(?:みたい|のよう)な顔(?:立ち)?|っぽい顔"
    r"|みたいな(?=人|男|女|子|歌手|アイドル|俳優|女優|少年|少女))"
)
# "<Name> lookalike", "<Name> vibes", "<Name>-esque" …
_NAME_SUFFIX_EN = re.compile(
    rf"(?P<name>{_NAME_RUN})(?P<suffix>-?esque\b|(?:-|[ \t]+)(?i:(?P<strong>look[\s-]?alikes?|doppelg(?:a|ä)ngers?"
    r"|impersonators?|cosplay(?:ers?)?)|vibes?|style|type|inspired)\b)"
)
# Phrases that compare someone to a person; the person after them is removed too.
_LIKENESS_OBJECT_EN = re.compile(
    r"\b(?:(?:who|that|which)\s+)?(?:(?:is|are|was|were)\s+)?(?:"
    r"look(?:s|ing|ed)?\s+(?:(?:exactly|just|a\s+lot|very\s+much|kind\s+of|a\s+bit|somewhat|almost)\s+)?like"
    r"|resembl(?:e|es|ed|ing)|(?:bears?\s+)?(?:a\s+)?(?:strong\s+)?resemblance\s+to"
    r"|(?:a\s+)?dead\s+ringer\s+for|(?:the\s+)?spitting\s+image\s+of"
    r"|(?:in\s+the\s+)?likeness\s+of|modell?ed\s+(?:on|after)"
    r")\b",
    re.IGNORECASE,
)
# Style attributions: removed only when a capitalised name follows ("in the style of Taylor Swift").
_STYLE_OF_EN = re.compile(
    r"(?i:\b(?:in\s+the\s+(?:style|manner|vein|image)\s+of|(?:inspired|influenced)\s+by|(?:the\s+)?vibes?\s+of"
    r"|styled\s+after|channell?ing|(?:just\s+)?like|(?:à|a)\s+la))[ \t]+"
    rf"(?P<name>{_CAP_WORD}(?:[ \t]+{_CAP_WORD}){{0,3}})"
)


def _wordset(words: str) -> frozenset[str]:
    """Whitespace-separated word list → frozenset (keeps the long vocabularies readable)."""
    return frozenset(words.split())


_OBJECT_WORD = re.compile(r"[ \t]*([A-Za-z][A-Za-z0-9'’.\-]*|[\u3400-\u4DBF\u4E00-\u9FFF\u30A0-\u30FFー・]+)")
_OBJECT_LEAD = _wordset(
    "a an the some young younger old older famous popular real certain well-known known celebrity pop rock "
    "movie film tv k-pop j-pop star actor actress singer idol musician rapper artist model athlete"
)
_OBJECT_BOUNDARY = _wordset(
    "who that which and or but while with in on at by under near through from to into onto over across along "
    "wearing holding as during after before for of beside behind against"
)
_STOP_WORDS = _wordset(
    "a an the of and or but in on at by to for from with without into onto over under above below near "
    "through across along around behind beside between beyond inside outside during after before while "
    "as is are was were be this that these those it its his her their my your our one two three no not "
    "all every each some any very just only also then there here when where who what"
)
_COMMON_WORDS = _wordset(
    "man woman men women girl boy girls boys kid kids child person people couple friend friends lover lovers "
    "stranger hero heroine dancer singer guitarist drummer band crowd figure silhouette protagonist "
    "home house street streets road avenue lane alley square plaza market tower towers bridge station park "
    "garden river lake bay beach coast shore island mountain mount mt hill hills valley forest woods desert "
    "city town village downtown district harbor harbour port pier dock airport hotel cafe bar club hall "
    "museum church temple shrine castle palace school campus office studio stage theater theatre cinema "
    "rooftop subway metro train bus taxi car highway tunnel window door room bedroom kitchen "
    "sky sun moon star stars night day dawn dusk sunset sunrise morning evening noon midnight twilight "
    "rain snow fog mist storm wind cloud clouds light lights shadow shadows fire water ocean sea wave waves "
    "spring summer autumn fall winter season old new big small little great grand golden silver gold "
    "red blue green yellow orange purple pink white black grey gray brown neon dark bright soft warm cool "
    "cold hot quiet loud slow fast lonely happy sad young modern retro vintage classic future past "
    "time times dream dreams love heart soul life world earth heaven memory memories story "
    "north south east west central upper lower first last high low long short open closed "
    "monday tuesday wednesday thursday friday saturday sunday january february march april may june july "
    "august september october november december christmas halloween new year eve "
    "medium close-up close wide extreme shot shots angle view scene scenes camera lens footage film "
    "cinematic vertical setting evoking alternate take photorealistic live-action portrait"
)
_PLACE_WORDS = _wordset(
    "tokyo osaka kyoto nara kobe yokohama nagoya sapporo sendai fukuoka hiroshima nagasaki okinawa hokkaido "
    "kyushu shikoku honshu kamakura shibuya shinjuku harajuku akihabara ginza roppongi odaiba asakusa ueno "
    "ikebukuro shimokitazawa koenji kichijoji fuji japan japanese asia asian europe european america american "
    "africa african pacific atlantic mediterranean arctic antarctic alps himalaya himalayas amazon sahara "
    "york los angeles san francisco diego las vegas chicago miami seattle boston brooklyn manhattan hollywood "
    "texas california hawaii honolulu alaska mexico havana brazil rio janeiro buenos aires lima andes "
    "patagonia canada vancouver toronto london paris berlin rome venice milan florence barcelona madrid "
    "lisbon amsterdam prague vienna budapest istanbul athens moscow siberia iceland norway sweden finland "
    "denmark scandinavia nordic england scotland ireland france italy spain germany greece egypt cairo "
    "dubai india nepal tibet mongolia china chinese beijing shanghai hong kong taiwan taipei korea korean "
    "seoul busan thailand bangkok vietnam hanoi singapore bali indonesia philippines manila sydney "
    "melbourne australia zealand"
)
_PERSON_NAME_MAX = 60


@functools.cache
def _known_words() -> frozenset[str]:
    """Lower-case Latin words that are vocabulary, not names (glossary, presets, common words, places)."""
    texts: list[str] = list(GLOSSARY_JA_EN.values())
    try:
        from app.services import presets

        def walk(node: Any) -> None:
            if isinstance(node, str):
                texts.append(node)
            elif isinstance(node, dict):
                for value in node.values():
                    walk(value)
            elif isinstance(node, (list, tuple)):
                for value in node:
                    walk(value)

        for table in ("WORLD_PRESETS", "SCENE_TYPES", "CAMERA_ANGLES", "CAMERA_MOVES", "COLOR_GRADES", "LYRIC_STYLES"):
            walk(getattr(presets, table, {}))
    except ImportError:  # pragma: no cover - presets is part of the app
        pass
    words: set[str] = set(_STOP_WORDS | _COMMON_WORDS | _PLACE_WORDS | _OBJECT_LEAD)
    for text in texts:
        for token in re.findall(r"[A-Za-z0-9'’\-]+", text):
            token = token.lower()
            words.add(token)
            words.update(part for part in token.split("-") if part)
    return frozenset(words)


def _is_known(word: str) -> bool:
    low = word.lower().strip("'’.")
    if low in _known_words() or (low.endswith("s") and low[:-1] in _known_words()):
        return True
    if "-" in low and all(part in _known_words() for part in low.split("-") if part):
        return True
    return word.isupper() and len(word) <= 4  # acronyms: CGI, HDR, LUT, TV


def _title_case(text: str) -> bool:
    long_words = [w for w in re.findall(r"[A-Za-z][A-Za-z'’\-]*", text) if len(w) > 3]
    return len(long_words) >= 3 and sum(w[0].isupper() for w in long_words) / len(long_words) >= 0.6


def _name_spans(text: str) -> list[tuple[int, int]]:
    """Spans of capitalised-word runs that look like a person's name (see ``detect_person_names``)."""
    strict = _title_case(text)
    spans: list[tuple[int, int]] = []
    for match in _CAP_RUN.finditer(text):
        words = [(m.start(), m.end(), m.group(0)) for m in re.finditer(r"\S+", match.group(0))]
        while words and words[0][2].lower() in _STOP_WORDS:
            words.pop(0)
        while words and words[-1][2].lower() in _STOP_WORDS:
            words.pop()
        unknown = [w for w in words if not _is_known(w[2])]
        if len(words) >= 1 and len(unknown) >= (2 if strict else 1) and len(match.group(0).split()) >= 2:
            spans.append((match.start() + words[0][0], match.start() + words[-1][1]))
    return spans


def detect_person_names(text: str) -> list[str]:
    """Probable real-person names in Latin-script free text.

    A name is a run of two or more adjacent Capitalised words with at least one
    word that is not known vocabulary (presets, the JA→EN glossary, common
    scene words, place names); in Title Case text two unknown words are
    needed. Single words are not reported ("Aimyon" needs an alias).
    """
    normalized = unicodedata.normalize("NFKC", text or "")
    return list(dict.fromkeys(normalized[a:b] for a, b in _name_spans(normalized)))


def _likeness_object_end(text: str, pos: int) -> tuple[int, bool]:
    """End of the person named after a look-alike phrase, and whether it names someone (not a thing)."""
    end = pos
    for _ in range(6):
        match = _OBJECT_WORD.match(text, end)
        if not match:
            return end, False
        word = match.group(1)
        if word[0].isupper() or not word.isascii():
            end = match.end()
            names = [word]
            while (more := _OBJECT_WORD.match(text, end)) and (
                more.group(1)[0].isupper() or not more.group(1).isascii()
            ):
                end = more.end()
                names.append(more.group(1))
            return end, any(not _is_known(n) for n in names)
        low = word.lower()
        if low in _OBJECT_LEAD:
            end = match.end()
            continue
        if low in _OBJECT_BOUNDARY or low.endswith("ing"):
            return end, False
        end = match.end()
        words = [word]
        following = _OBJECT_WORD.match(text, end)
        if following and following.group(1).isascii() and following.group(1)[0].islower():
            nxt = following.group(1).lower()
            if nxt not in _OBJECT_BOUNDARY and not nxt.endswith("ing"):
                end = following.end()
                words.append(following.group(1))
        return end, any(not _is_known(w) for w in words)
    return end, False


@dataclass(frozen=True)
class PersonReferences:
    """Result of :func:`strip_person_references`."""

    text: str
    phrases: list[str]  # removed look-alike / real-person phrasing (with the person it named)
    names: list[str]  # every person name removed (also those inside ``phrases``)


def strip_person_references(text: str) -> PersonReferences:
    """Remove real-person references from free text (Japanese and English).

    Look-alike constructions go together with the person they name
    ("a man resembling Brad Pitt" → "a man", "Keanu Reeves lookalike",
    "in the style of Taylor Swift", "木村拓哉似の男性" → "男性"), generic
    real-person phrasing ("celebrity", 「芸能人」, …) is removed, and so are
    probable names (:func:`detect_person_names`). Artist names are handled by
    :func:`strip_forbidden_names`.
    """
    body = unicodedata.normalize("NFKC", text or "")
    phrases: list[str] = []
    names: list[str] = []

    def resemble_ja(match: re.Match[str]) -> str:
        phrases.append(match.group(0).strip())
        if match.group("name"):
            names.append(match.group("name").strip())
        return " "

    def name_suffix(match: re.Match[str]) -> str:
        words = match.group("name").split()
        keep: list[str] = []
        if not match.group("strong"):
            while words and (words[0].lower() in _STOP_WORDS or _is_known(words[0])):
                keep.append(words.pop(0))
            if not words:
                return match.group(0)
        phrases.append(f"{' '.join(words)}{match.group('suffix')}".strip())
        if words:
            names.append(" ".join(words))
        return f"{' '.join(keep)} " if keep else " "

    def style_of(match: re.Match[str]) -> str:
        name = match.group("name").split()
        while name and _is_known(name[-1]):
            name.pop()
        if not name or all(_is_known(w) for w in name):
            return match.group(0)
        removed = match.group(0)[: match.start("name") - match.start()] + " ".join(name)
        phrases.append(removed.strip())
        names.append(" ".join(name))
        rest = match.group("name").split()[len(name) :]
        return f" {' '.join(rest)} " if rest else " "

    for _ in range(3):  # removing one reference can expose another; converge
        before = body
        body = _RESEMBLE_JA.sub(resemble_ja, body)
        body = _NAME_SUFFIX_EN.sub(name_suffix, body)
        pieces: list[str] = []
        cursor = 0
        for match in _LIKENESS_OBJECT_EN.finditer(body):
            if match.start() < cursor:
                continue
            end, is_person = _likeness_object_end(body, match.end())
            if not is_person:
                continue
            phrases.append(body[match.start() : end].strip())
            obj = body[match.end() : end].split()
            caps = [w for w in obj if w[0].isupper() or not w.isascii()]
            if caps or obj:
                names.append(" ".join(caps or [w for w in obj if w.lower() not in _OBJECT_LEAD]))
            pieces.append(body[cursor : match.start()] + " ")
            cursor = end
        body = "".join(pieces) + body[cursor:]
        body = _STYLE_OF_EN.sub(style_of, body)
        for pattern in (_REAL_PERSON_EN, _REAL_PERSON_JA):
            phrases.extend(m.group(0) for m in pattern.finditer(body))
            body = pattern.sub(" ", body)
        for start, end in reversed(_name_spans(body)):
            names.append(body[start:end])
            body = f"{body[:start]} {body[end:]}"
        if body == before:
            break
    names = [n for n in dict.fromkeys(n.strip() for n in names) if 2 <= len(n) <= _PERSON_NAME_MAX]
    return PersonReferences(text=_tidy(body), phrases=list(dict.fromkeys(p for p in phrases if p)), names=names)


def person_reference_warnings(refs: PersonReferences, *, where: str = "") -> list[str]:
    """Japanese warnings for :func:`strip_person_references` (``where`` e.g. 「カスタム世界観から」)."""
    warnings = [
        f"{where}実在の人物を示す表現「{phrase}」を削除しました（架空の人物のみを生成します）"
        for phrase in refs.phrases
    ]
    for name in refs.names:
        if not any(name in phrase for phrase in refs.phrases):
            warnings.append(f"{where}人名と思われる「{name}」を削除しました（実在の人物を生成しないため）")
    return warnings


def screen_free_text(
    text: str | None, forbidden_names: Iterable[str], *, where: str = ""
) -> tuple[str, list[str], list[str]]:
    """Clean user free text (custom world, mood…) before it can reach a prompt.

    Removes the forbidden names and every real-person reference. Returns
    ``(text, removed person names, Japanese warnings)``.
    """
    if not text or not text.strip():
        return "", [], []
    cleaned, removed = strip_forbidden_names(text, forbidden_names)
    # The artist name is not echoed: it must not appear anywhere in the storyboard.
    warnings = [f"{where}アーティスト名を削除しました（実在人物の肖像を生成しないため）"] if removed else []
    refs = strip_person_references(cleaned)
    return refs.text, refs.names, warnings + person_reference_warnings(refs, where=where)


# Romaji for kana artist names (あいみょん → aimyon), so romanised spellings are forbidden too.
_KANA_ROMAJI: dict[str, str] = {
    "あ": "a", "い": "i", "う": "u", "え": "e", "お": "o",
    "か": "ka", "き": "ki", "く": "ku", "け": "ke", "こ": "ko",
    "が": "ga", "ぎ": "gi", "ぐ": "gu", "げ": "ge", "ご": "go",
    "さ": "sa", "し": "shi", "す": "su", "せ": "se", "そ": "so",
    "ざ": "za", "じ": "ji", "ず": "zu", "ぜ": "ze", "ぞ": "zo",
    "た": "ta", "ち": "chi", "つ": "tsu", "て": "te", "と": "to",
    "だ": "da", "ぢ": "ji", "づ": "zu", "で": "de", "ど": "do",
    "な": "na", "に": "ni", "ぬ": "nu", "ね": "ne", "の": "no",
    "は": "ha", "ひ": "hi", "ふ": "fu", "へ": "he", "ほ": "ho",
    "ば": "ba", "び": "bi", "ぶ": "bu", "べ": "be", "ぼ": "bo",
    "ぱ": "pa", "ぴ": "pi", "ぷ": "pu", "ぺ": "pe", "ぽ": "po",
    "ま": "ma", "み": "mi", "む": "mu", "め": "me", "も": "mo",
    "や": "ya", "ゆ": "yu", "よ": "yo", "ら": "ra", "り": "ri",
    "る": "ru", "れ": "re", "ろ": "ro", "わ": "wa", "ゐ": "i",
    "ゑ": "e", "を": "o", "ん": "n", "ぁ": "a", "ぃ": "i",
    "ぅ": "u", "ぇ": "e", "ぉ": "o", "ゔ": "vu",
}  # fmt: skip
_SMALL_Y = {"ゃ": "a", "ゅ": "u", "ょ": "o"}
_SMALL_VOWELS = {"ぁ": "a", "ぃ": "i", "ぅ": "u", "ぇ": "e", "ぉ": "o"}
_KANA_NAME = re.compile(r"^[\u3041-\u3096\u30A1-\u30FAー・･\s]+$")


def _romanize(kana: str) -> str:
    hira = "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in kana)
    out = ""
    double = False
    i = 0
    while i < len(hira):
        ch = hira[i]
        if ch == "っ":
            double = True
            i += 1
            continue
        if ch == "ー":
            out += out[-1] if out and out[-1] in "aeiou" else ""
            i += 1
            continue
        syllable = _KANA_ROMAJI.get(ch)
        if syllable is None:
            out += " " if ch in " ・･\u3000" else ""
            i += 1
            continue
        nxt = hira[i + 1] if i + 1 < len(hira) else ""
        if nxt in _SMALL_Y and syllable.endswith("i") and len(syllable) > 1:
            base = syllable[:-1]
            syllable = base + _SMALL_Y[nxt] if base in ("sh", "ch", "j") else base + "y" + _SMALL_Y[nxt]
            i += 1
        elif nxt in _SMALL_VOWELS and ch not in _SMALL_VOWELS:
            vowel = _SMALL_VOWELS[nxt]
            if syllable in ("shi", "chi", "ji"):
                syllable = syllable[:-1] + vowel
            elif syllable == "u":
                syllable = "w" + vowel
            elif len(syllable) > 1:
                syllable = syllable[:-1] + vowel
            i += 1
        if double and syllable[0] not in "aeiou":
            out += "t" if syllable.startswith("ch") else syllable[0]
        double = False
        out += syllable
        i += 1
    return re.sub(r"\s+", " ", out).strip()


def artist_name_variants(name: str) -> list[str]:
    """The name plus its romaji spellings when it is written in kana (あいみょん → aimyon)."""
    name = unicodedata.normalize("NFKC", name or "").strip()
    if not name:
        return []
    variants = [name]
    if _KANA_NAME.match(name):
        base = _romanize(name)
        collapsed = re.sub(r"ou|oo", "o", re.sub(r"uu", "u", base))
        nasal = re.sub(r"n(?=[bmp])", "m", base)
        for variant in (base, collapsed, nasal, base.replace(" ", "")):
            if len(variant.replace(" ", "")) >= 3 and variant not in variants:
                variants.append(variant)
    return variants


def _strongest_punctuation(match: re.Match[str]) -> str:
    run = match.group(0)
    if "." in run:
        return "."
    return "。" if "。" in run else run[0]


def prompt_body_budget(max_chars: int = MAX_PROMPT_CHARS) -> int:
    """Characters available for the prompt body once ``sanitize_prompt`` appends its clauses."""
    return max(0, max_chars - len(_SAFETY_SUFFIX) - 2)


def _tidy(text: str) -> str:
    text = re.sub(r"\(\s*\)|（\s*）|「\s*」|\"\s*\"", " ", text)
    text = re.sub(r"[ \t\r\n]+", " ", text)
    text = re.sub(r"\s+([,.;:!?、。])", r"\1", text)
    text = re.sub(r"[.,;、。](?:\s*[.,;、。])+", _strongest_punctuation, text)
    text = re.sub(r"(?:\b(?:of|by|like|as|and|or|with|featuring|feat\.?)\s*)+([,.;])", r"\1", text)
    text = re.sub(r"^(?:(?:and|or|with|of|by|featuring|feat\.?)\b\s*)+", "", text.strip(" ,;:、"), flags=re.IGNORECASE)
    text = re.sub(r"^[がをにはでとのもへや]+(?=\S)", "", text)
    return text.strip(" ,;:、")


def _fit(body: str, limit: int) -> tuple[str, bool]:
    """Shorten ``body`` to ``limit`` characters at the latest sentence/clause/word boundary."""
    if len(body) <= limit:
        return body, False
    cut = body[:limit]
    for boundary, min_keep in ((". ", 0.6), (", ", 0.6), (" ", 0.0)):
        pos = cut.rfind(boundary)
        if pos >= limit * min_keep and pos > 0:
            return cut[:pos].rstrip(" ,;:"), True
    return cut.rstrip(" ,;:"), True


def sanitize_prompt(
    text: str, *, forbidden_names: list[str], max_chars: int = MAX_PROMPT_CHARS
) -> tuple[str, list[str]]:
    """Make ``text`` safe to send to a video model.

    Removes the forbidden names (artist name, aliases, …), real-person /
    celebrity / look-alike phrasing together with the person it names, and
    probable real-person names (:func:`strip_person_references`), then appends
    the likeness-safety and no-text clauses.
    The result never exceeds ``max_chars`` (the body is shortened at a
    sentence/clause boundary when needed). Idempotent. Returns
    ``(prompt, warnings)`` with Japanese warnings.
    """
    warnings: list[str] = []
    body = unicodedata.normalize("NFKC", text or "")
    body = _CANONICAL_CLAUSES.sub(" ", body)
    body = _NEGATED_SAFETY.sub(" ", body)

    body, removed_names = strip_forbidden_names(body, forbidden_names)
    for name in removed_names:
        warnings.append(
            f"アーティスト名「{name}」はプロンプトに使えないため削除しました（実在人物の肖像を生成しないため）"
        )

    refs = strip_person_references(body)
    body = refs.text
    warnings.extend(person_reference_warnings(refs))

    body = _tidy(body).rstrip(" .")
    body, shortened = _fit(body, prompt_body_budget(max_chars))
    if shortened:
        warnings.append(f"プロンプトが長すぎるため、{max_chars}文字に収まるよう末尾を省略しました")
    prompt = f"{body}. {_SAFETY_SUFFIX}" if body else _SAFETY_SUFFIX
    return prompt, list(dict.fromkeys(warnings))


# --- Japanese → English ----------------------------------------------------------------------

# Best-effort glossary for scene / mood vocabulary (used when Claude is not
# configured). Longest match wins, so compounds are listed alongside single
# kanji to avoid wrong splits (e.g. 空港 vs 空).
GLOSSARY_JA_EN: dict[str, str] = {
    # time of day / weather
    "夜明け前": "pre-dawn", "夜明け": "dawn", "朝焼け": "sunrise glow", "朝日": "morning sun", "朝": "morning",
    "昼下がり": "early afternoon", "昼": "daytime", "午後": "afternoon", "夕焼け": "sunset glow", "夕暮れ": "dusk",
    "夕方": "evening", "夕日": "setting sun", "黄昏": "twilight", "日没": "sunset", "真夜中": "midnight",
    "深夜": "late night", "夜空": "night sky", "夜景": "city lights at night", "夜": "night",
    "ブルーアワー": "blue hour", "マジックアワー": "golden hour", "雨上がり": "after the rain", "小雨": "drizzle",
    "大雨": "heavy rain", "雨": "rain", "雪景色": "snowy landscape", "雪": "snow", "霧": "fog", "霞": "haze",
    "嵐": "storm", "雷": "lightning", "虹": "rainbow", "曇り": "overcast", "晴れ": "clear sky", "風": "wind",
    "春": "spring", "夏": "summer", "秋": "autumn", "冬": "winter", "梅雨": "rainy season",
    # places
    "都会": "big city", "都市": "city", "街並み": "townscape", "街角": "street corner", "街灯": "streetlights",
    "街": "city streets", "町": "small town", "路地裏": "back alley", "路地": "narrow alley", "交差点": "crossroads",
    "歩道橋": "pedestrian bridge", "歩道": "sidewalk", "商店街": "shopping street", "高速道路": "highway",
    "道": "road", "橋": "bridge", "駅": "train station", "ホーム": "platform", "電車": "train",
    "線路": "railway tracks",
    "踏切": "railroad crossing", "車窓": "train window", "バス停": "bus stop", "空港": "airport",
    "屋上": "rooftop", "ビル": "buildings", "高層ビル": "skyscrapers", "部屋": "room", "窓辺": "window side",
    "窓": "window", "教室": "classroom", "学校": "school", "廊下": "corridor", "階段": "stairs", "図書館": "library",
    "カフェ": "cafe", "喫茶店": "old coffee shop", "公園": "park", "遊園地": "amusement park", "観覧車": "ferris wheel",
    "神社": "shrine", "鳥居": "torii gate", "寺": "temple", "廃墟": "abandoned ruins", "工場": "factory",
    "港": "harbor", "灯台": "lighthouse", "海辺": "seaside", "海岸": "coastline", "砂浜": "sandy beach",
    "浜辺": "beach", "海": "ocean", "波": "waves", "水平線": "horizon", "地平線": "horizon", "島": "island",
    "湖": "lake", "川": "river", "滝": "waterfall", "池": "pond", "水面": "water surface", "水中": "underwater",
    "森": "forest", "林": "woods", "木漏れ日": "dappled sunlight", "木": "trees", "草原": "grassland meadow",
    "野原": "open field", "花畑": "flower field", "田んぼ": "rice fields", "畑": "farm fields", "田舎": "countryside",
    "山": "mountains", "丘": "hills", "崖": "cliff", "砂漠": "desert", "洞窟": "cave", "宇宙": "outer space",
    "空": "sky", "雲": "clouds", "星空": "starry sky", "星": "stars", "月明かり": "moonlight", "月": "moon",
    "太陽": "sun", "日差し": "sunlight", "東京": "Tokyo", "大阪": "Osaka", "京都": "Kyoto", "渋谷": "Shibuya",
    "新宿": "Shinjuku", "ネオン": "neon lights", "ライト": "lights", "提灯": "paper lanterns", "花火": "fireworks",
    "祭り": "summer festival", "車": "car", "自転車": "bicycle", "バイク": "motorcycle", "船": "boat",
    # nature / objects
    "桜": "cherry blossoms", "紅葉": "autumn leaves", "落ち葉": "fallen leaves", "花びら": "petals", "花": "flowers",
    "向日葵": "sunflowers", "ひまわり": "sunflowers", "葉": "leaves", "草": "grass", "鳥": "birds", "猫": "cat",
    "犬": "dog", "蝶": "butterflies", "炎": "flames", "火": "fire", "煙": "smoke", "水": "water", "氷": "ice",
    "鏡": "mirror", "ガラス": "glass", "傘": "umbrella", "手紙": "letter", "写真": "photograph", "ギター": "guitar",
    "ピアノ": "piano", "カーテン": "curtains", "キャンドル": "candlelight", "ろうそく": "candlelight",
    "光の粒": "light particles", "光": "light", "影": "shadows", "闇": "darkness", "シルエット": "silhouette",
    # people (always fictional adults)
    "少女": "young woman", "少年": "young man", "女の子": "young woman", "男の子": "young man", "女性": "woman",
    "男性": "man", "二人": "two people", "ふたり": "two people", "恋人": "couple", "友達": "friends",
    "主人公": "protagonist", "ひとり": "alone", "一人": "alone", "群衆": "crowd", "人混み": "crowd",
    # actions
    "歩": "walking", "走": "running", "踊": "dancing", "泣": "tears", "涙": "tears", "笑顔": "smiling",
    "笑": "laughing", "飛": "flying", "泳": "swimming", "眠": "sleeping", "叫": "crying out", "抱": "embracing",
    "見つめ": "gazing", "振り返": "looking back", "待": "waiting", "旅": "journey", "別れ": "farewell",
    "出会い": "encounter", "約束": "promise", "記憶": "memories", "思い出": "memories", "夢": "dream",
    # mood
    "切な": "bittersweet", "懐かし": "nostalgic", "ノスタルジック": "nostalgic", "エモ": "emotional",
    "寂し": "lonely", "孤独": "solitude", "悲し": "melancholic", "哀愁": "melancholy", "希望": "hopeful",
    "優し": "gentle", "温か": "warm", "暖か": "warm", "冷た": "cold", "静か": "quiet", "穏やか": "calm",
    "激し": "intense", "力強": "powerful", "爽やか": "refreshing", "眩し": "dazzling", "明る": "bright",
    "暗": "dark", "儚": "fleeting", "幻想的": "dreamlike", "幻想": "fantasy", "神秘的": "mysterious",
    "不思議": "mysterious", "ミステリアス": "mysterious", "ロマンチック": "romantic", "クール": "cool",
    "かっこい": "stylish", "可愛": "cute", "美し": "beautiful", "綺麗": "beautiful", "きれい": "beautiful",
    "壮大": "epic", "疾走感": "sense of speed", "青春": "youthful", "未来": "futuristic", "近未来": "near-future",
    "レトロ": "retro", "ビンテージ": "vintage", "ヴィンテージ": "vintage", "昭和": "Showa-era retro",
    "サイバーパンク": "cyberpunk", "映画的": "cinematic", "映画": "cinematic", "シネマティック": "cinematic",
    "ドキュメンタリー": "documentary", "フィルム": "film grain", "モノクロ": "black and white",
    "白黒": "black and white",
    "スローモーション": "slow motion", "ゆっくり": "slowly", "静寂": "silence", "自由": "freedom", "愛": "love",
    "永遠": "eternal", "最後": "final", "始まり": "beginning", "終わり": "ending",
    # colours
    "青い": "blue", "青": "blue", "水色": "light blue", "赤い": "red", "赤": "red", "白い": "white", "白": "white",
    "黒い": "black", "黒": "black", "金色": "golden", "銀色": "silver", "緑": "green", "紫": "purple",
    "ピンク": "pink", "オレンジ": "orange", "黄色": "yellow", "茶色": "brown", "灰色": "grey", "パステル": "pastel",
    "モノトーン": "monochrome", "セピア": "sepia",
}  # fmt: skip

_GLOSSARY_MAX_KEY = max(len(key) for key in GLOSSARY_JA_EN)
_ASCII_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-]*")
_GROUP_SEPARATORS = frozenset("、。,.!?！？\n\r\t 　／/・;；:：「」『』（）()")
# Conjunctive particles ("A と B", "A や B") also split phrases, but only where
# no glossary word starts at that position (e.g. ひとり is matched first).
_SOFT_SEPARATORS = frozenset("とや")


def _glossary_groups(text: str, *, include_ascii: bool = True) -> list[list[str]]:
    """Phrase groups of English words (split at punctuation, spaces and と/や); unknown words are dropped."""
    text = unicodedata.normalize("NFKC", text or "")
    groups: list[list[str]] = [[]]
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in _GROUP_SEPARATORS:
            if groups[-1]:
                groups.append([])
            i += 1
            continue
        ascii_match = _ASCII_TOKEN.match(text, i)
        if ascii_match:
            if include_ascii:
                groups[-1].append(ascii_match.group(0))
            i = ascii_match.end()
            continue
        for length in range(min(_GLOSSARY_MAX_KEY, len(text) - i), 0, -1):
            english = GLOSSARY_JA_EN.get(text[i : i + length])
            if english:
                if english not in groups[-1]:
                    groups[-1].append(english)
                i += length
                break
        else:
            if ch in _SOFT_SEPARATORS and groups[-1]:
                groups.append([])
            i += 1
    return [g for g in groups if g]


def glossary_translate(text: str) -> str:
    """Glossary translation: phrase groups joined with ", " (e.g. "dawn seaside, quiet waves")."""
    phrases = list(dict.fromkeys(" ".join(group) for group in _glossary_groups(text)))
    return ", ".join(phrases)


def glossary_keywords(text: str, limit: int | None = None, *, include_ascii: bool = True) -> list[str]:
    """Distinct English keywords found in ``text`` (in order of appearance).

    ``include_ascii=False`` keeps only glossary translations: Latin-script words
    copied from free text (lyrics, moods) could be names of real people.
    """
    groups = _glossary_groups(text, include_ascii=include_ascii)
    words = list(dict.fromkeys(word for group in groups for word in group))
    return words[:limit] if limit is not None else words


# English mood words that may be copied from free text (anything else Latin-script
# could be a name: "Billie Eilish vibes").
_MOOD_WORDS_EN = _wordset(
    "moody dreamy dreamlike melancholic melancholy nostalgic bittersweet romantic energetic upbeat calm quiet "
    "dark bright hopeful uplifting sad happy joyful cheerful lonely warm cold gentle tender intense powerful "
    "epic mysterious dramatic chill mellow cinematic retro vintage futuristic euphoric serene peaceful tense "
    "playful emotional fleeting ethereal gloomy wistful fierce edgy sensual nocturnal urban rainy sunny "
    "stormy misty hazy soft heavy fast slow lively wild free youthful elegant raw gritty lush vivid "
    "pastel neon festive solemn somber sombre haunting hypnotic groovy funky jazzy cozy cosy"
)


def mood_keywords(text: str, limit: int | None = None) -> list[str]:
    """English mood words for a prompt: glossary translations plus allow-listed English adjectives.

    Other Latin-script words are never copied (they could name a real person).
    """
    words = glossary_keywords(text, include_ascii=False)
    for token in _ASCII_TOKEN.findall(unicodedata.normalize("NFKC", text or "")):
        low = token.lower()
        if low in _MOOD_WORDS_EN and low not in words:
            words.append(low)
    return words[:limit] if limit is not None else words


def make_claude_client() -> Any | None:
    """An ``anthropic.Anthropic`` client when ``ANTHROPIC_API_KEY`` is configured, else ``None``."""
    settings = get_settings()
    if not settings.anthropic_configured:
        return None
    import anthropic

    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=300.0, max_retries=2)


_TRANSLATE_SYSTEM = (
    "You translate short Japanese creative briefs (world-building notes, moods, scene ideas) into concise, "
    "vivid English for a text-to-video prompt. Keep concrete visual details (places, light, colours, weather, "
    "objects, actions). Never add the names of real people. Output only the English translation, no quotes "
    "and no commentary."
)


def first_text_block(response: Any) -> str | None:
    """Text of the first ``text`` content block of a Messages API response (thinking blocks are skipped)."""
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text":
            return str(getattr(block, "text", ""))
    return None


def _claude_translate(text: str, client: Any) -> str | None:
    import anthropic

    settings = get_settings()
    try:
        response = client.beta.messages.create(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=4000,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            output_config={"effort": "low"},
            system=_TRANSLATE_SYSTEM,
            messages=[{"role": "user", "content": text}],
        )
    except anthropic.BadRequestError as exc:
        logger.warning("Claude translation rejected (400): %s", exc.message)
        return None
    except anthropic.RateLimitError:
        logger.warning("Claude translation rate limited; using glossary")
        return None
    except anthropic.APIStatusError as exc:
        logger.warning("Claude translation failed with HTTP %s; using glossary", exc.status_code)
        return None
    except anthropic.APIConnectionError:
        logger.warning("Claude translation: connection error; using glossary")
        return None
    if getattr(response, "stop_reason", None) == "refusal":
        logger.warning("Claude declined the translation request; using glossary")
        return None
    translated = (first_text_block(response) or "").strip().strip("\"'“”")
    return translated or None


def translate_to_english(text: str, *, use_claude: bool = True, client: Any | None = None) -> tuple[str, str]:
    """Translate Japanese ``text`` to English. Returns ``(english, source)``.

    ``source`` is ``"passthrough"`` (empty or already ASCII), ``"claude"`` or
    ``"glossary"`` (best-effort keyword translation; unknown words are dropped,
    so the result can be empty). ``client`` overrides the Anthropic client
    (tests); Claude is only used when ``use_claude`` is true and a client is
    available.
    """
    stripped = (text or "").strip()
    if not stripped:
        return "", "passthrough"
    if stripped.isascii():
        return stripped, "passthrough"
    if use_claude:
        claude = client if client is not None else make_claude_client()
        if claude is not None:
            translated = _claude_translate(stripped, claude)
            if translated:
                return translated, "claude"
    return glossary_translate(stripped), "glossary"
