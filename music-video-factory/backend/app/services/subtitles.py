"""ASS subtitle generation for burned-in lyrics (ARCHITECTURE.md §6.6).

:func:`build_ass` turns absolute-time :data:`LyricLine` dicts into an ASS
script whose times are relative to the rendered segment. Design rules:

* Five lyric styles (``simple``, ``cinematic``, ``minimal``, ``karaoke``,
  ``fade``) with distinct typography; every :data:`TextStyle` key overrides the
  style default.
* Horizontal text only. Line breaking is done here (``WrapStyle: 2`` disables
  libass wrapping): Japanese-aware (kinsoku shori, bunsetsu-ish boundaries,
  balanced lengths), never inside a Latin word, at most two lines. Text that
  cannot fit in two lines is shrunk rather than cut.
* Every visual line is its own ``Dialogue`` event with an explicit ``\\pos`` so
  the block geometry is exact: nothing is drawn above ``safe_top`` or below
  ``safe_bottom`` (Instagram Stories safe area, 270 px / 380 px on
  1080×1920, scaled proportionally for other sizes). One event per line also
  gives clean, non-overlapping semi-transparent boxes: with ``bg_opacity`` a
  box-only event (``LyricBox``, ``BorderStyle=3``, always black like the
  editor preview) is drawn under the text event, which keeps its own outline.
* Lines that start at the same time (bilingual / duet LRC) are shown as one
  stacked block (one part per lyric line, each wrapped on its own) instead of
  being drawn on top of each other.
* ``font_size`` is the ASS ``Fontsize`` in ``PlayRes`` pixels (a 1080×1920
  canvas); with Noto CJK the visible glyph width is about 0.69 × font size.
  ``max_chars_per_line`` counts full-width characters; half-width Latin
  characters count as roughly half a character.
"""

from __future__ import annotations

import itertools
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

__all__ = [
    "BASE_HEIGHT",
    "BASE_WIDTH",
    "LYRIC_STYLE_IDS",
    "STYLE_DEFAULTS",
    "SafeArea",
    "TextLayout",
    "build_ass",
    "escape_ass_text",
    "layout_text",
    "resolve_text_style",
    "safe_area",
    "text_width_units",
    "wrap_text",
]

BASE_WIDTH = 1080
BASE_HEIGHT = 1920
SAFE_TOP_PX = 270  # Instagram Stories: top 14 % hidden behind the profile bar
SAFE_BOTTOM_PX = 380  # bottom ~20 % hidden behind the reply bar
SIDE_MARGIN_RATIO = 0.08

LYRIC_STYLE_IDS: tuple[str, ...] = ("simple", "cinematic", "minimal", "karaoke", "fade")

Position = Literal["top", "center", "bottom"]
ShadowMode = Literal["hard", "soft"]

_FONT_SANS = "Noto Sans CJK JP"
_FONT_SERIF = "Noto Serif CJK JP"

# Weight → (fontconfig family, ASS Bold flag). Names checked with ``fc-list``:
# the CJK collections expose each weight as its own family.
_WEIGHT_FAMILIES: dict[str, dict[str, tuple[str, bool]]] = {
    _FONT_SANS: {
        "thin": (f"{_FONT_SANS} Thin", False),
        "light": (f"{_FONT_SANS} Light", False),
        "demilight": (f"{_FONT_SANS} DemiLight", False),
        "regular": (_FONT_SANS, False),
        "medium": (f"{_FONT_SANS} Medium", False),
        "bold": (_FONT_SANS, True),
        "black": (f"{_FONT_SANS} Black", False),
    },
    _FONT_SERIF: {
        "light": (f"{_FONT_SERIF} Light", False),
        "regular": (_FONT_SERIF, False),
        "medium": (f"{_FONT_SERIF} Medium", False),
        "semibold": (f"{_FONT_SERIF} SemiBold", False),
        "bold": (_FONT_SERIF, True),
        "black": (f"{_FONT_SERIF} Black", False),
    },
}
_FONT_WEIGHTS = frozenset({"thin", "light", "demilight", "regular", "medium", "semibold", "bold", "black"})


@dataclass(frozen=True)
class StyleSpec:
    """Fully resolved look of one lyric style (TextStyle keys + fixed design params)."""

    # --- TextStyle (user-overridable) ---
    font_family: str
    font_weight: str
    font_size: float
    primary_color: str
    highlight_color: str
    outline_color: str
    outline_width: float
    shadow: float
    position: Position
    bg_opacity: float
    letter_spacing: float
    max_chars_per_line: int
    # --- design parameters (per style, not user-overridable) ---
    shadow_mode: ShadowMode = "hard"
    shadow_opacity: float = 0.5
    outline_opacity: float = 1.0
    edge_blur: float = 0.0  # \blur on the outline (soft edge)
    soft_shadow_blur: float = 5.0
    line_spacing: float = 1.0  # line pitch as a multiple of font size
    inset: float = 40.0  # distance between the safe-area edge and the text block
    fade_in: float = 0.12  # seconds
    fade_out: float = 0.12
    rise: float = 0.0  # px the text drifts up while fading in
    box_padding: float = 14.0


STYLE_DEFAULTS: dict[str, StyleSpec] = {
    # White bold sans, thin outline, soft drop shadow, lower third.
    "simple": StyleSpec(
        font_family=_FONT_SANS, font_weight="bold", font_size=68, primary_color="#FFFFFF",
        highlight_color="#4DA3FF", outline_color="#000000", outline_width=3, shadow=2,
        position="bottom", bg_opacity=0.0, letter_spacing=1, max_chars_per_line=14,
        shadow_mode="hard", shadow_opacity=0.45, outline_opacity=0.85, edge_blur=0.8,
        line_spacing=1.08, inset=60, fade_in=0.12, fade_out=0.12,
    ),
    # Serif, smaller, warm white on a semi-transparent black box, low position.
    "cinematic": StyleSpec(
        font_family=_FONT_SERIF, font_weight="medium", font_size=54, primary_color="#F4EFE6",
        highlight_color="#E8C27A", outline_color="#000000", outline_width=0, shadow=0,
        position="bottom", bg_opacity=0.55, letter_spacing=3, max_chars_per_line=16,
        shadow_mode="hard", shadow_opacity=0.0, line_spacing=1.0, inset=20,
        fade_in=0.25, fade_out=0.25, box_padding=16,
    ),
    # Light weight, small, airy letter-spacing, no outline, subtle blurred shadow.
    "minimal": StyleSpec(
        font_family=_FONT_SANS, font_weight="light", font_size=56, primary_color="#FFFFFF",
        highlight_color="#FFFFFF", outline_color="#000000", outline_width=0, shadow=2,
        position="center", bg_opacity=0.0, letter_spacing=8, max_chars_per_line=13,
        shadow_mode="soft", shadow_opacity=0.85, soft_shadow_blur=4.5, line_spacing=1.3,
        inset=40, fade_in=0.18, fade_out=0.18,
    ),
    # Bold sans with a left-to-right highlight sweep (\kf) per word/character.
    "karaoke": StyleSpec(
        font_family=_FONT_SANS, font_weight="bold", font_size=70, primary_color="#FFFFFF",
        highlight_color="#4DA3FF", outline_color="#000000", outline_width=4, shadow=2,
        position="bottom", bg_opacity=0.0, letter_spacing=1, max_chars_per_line=13,
        shadow_mode="hard", shadow_opacity=0.4, outline_opacity=0.9, edge_blur=0.6,
        line_spacing=1.1, inset=60, fade_in=0.08, fade_out=0.12,
    ),
    # Medium sans, soft shadow, beat-aligned fade in/out with a slight rise.
    "fade": StyleSpec(
        font_family=_FONT_SANS, font_weight="medium", font_size=64, primary_color="#FFFFFF",
        highlight_color="#FFFFFF", outline_color="#000000", outline_width=0, shadow=3,
        position="center", bg_opacity=0.0, letter_spacing=2, max_chars_per_line=13,
        shadow_mode="soft", shadow_opacity=0.6, soft_shadow_blur=7, line_spacing=1.22,
        inset=40, fade_in=0.3, fade_out=0.35, rise=14,
    ),
}

_DEFAULT_FADE_IN = 0.30
_DEFAULT_FADE_OUT = 0.35
_FADE_WINDOW = (0.25, 0.42)  # acceptable beat-aligned fade length range (s)
_DEFAULT_LINE_HOLD = 4.0  # end time for an untimed-end last line
_MIN_EVENT = 0.05
_SAME_START = 0.05  # lines starting this close together are shown as one stacked block
_MIN_SHRINK = 0.6  # never shrink a line below 60 % of the style font size


# --------------------------------------------------------------------------- geometry


@dataclass(frozen=True)
class SafeArea:
    top: int
    bottom: int
    left: int
    right: int

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2


def safe_area(width: int = BASE_WIDTH, height: int = BASE_HEIGHT) -> SafeArea:
    """Instagram Stories safe area scaled to ``width``×``height``."""
    side = round(width * SIDE_MARGIN_RATIO)
    return SafeArea(
        top=round(SAFE_TOP_PX * height / BASE_HEIGHT),
        bottom=height - round(SAFE_BOTTOM_PX * height / BASE_HEIGHT),
        left=side,
        right=width - side,
    )


def _ui_scale(width: int, height: int) -> float:
    return min(width / BASE_WIDTH, height / BASE_HEIGHT)


# --------------------------------------------------------------------------- styles


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


_HEX_COLOR = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def _parse_color(value: Any) -> tuple[int, int, int, float] | None:
    """``#RGB``/``#RRGGBB``/``#RRGGBBAA`` → (r, g, b, opacity 0..1)."""
    if not isinstance(value, str):
        return None
    match = _HEX_COLOR.match(value.strip())
    if not match:
        return None
    hexstr = match.group(1)
    if len(hexstr) == 3:
        hexstr = "".join(ch * 2 for ch in hexstr)
    r, g, b = (int(hexstr[i : i + 2], 16) for i in (0, 2, 4))
    opacity = int(hexstr[6:8], 16) / 255 if len(hexstr) == 8 else 1.0
    return r, g, b, opacity


def _clean_font_family(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    # Commas separate Style fields; braces/backslashes would break override tags.
    cleaned = re.sub(r"[,{}\\\r\n\t]", " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:80] or None


def resolve_text_style(style_id: str, text_style: Mapping[str, Any] | None = None) -> StyleSpec:
    """Merge the style defaults with user :data:`TextStyle` overrides (invalid values ignored)."""
    base = STYLE_DEFAULTS.get(style_id) or STYLE_DEFAULTS["simple"]
    overrides: dict[str, Any] = {}
    ts = dict(text_style or {})

    family = _clean_font_family(ts.get("font_family"))
    if family:
        overrides["font_family"] = family
    weight = ts.get("font_weight")
    if isinstance(weight, str) and weight.strip().lower() in _FONT_WEIGHTS:
        overrides["font_weight"] = weight.strip().lower()
    for key in ("primary_color", "highlight_color", "outline_color"):
        if _parse_color(ts.get(key)) is not None:
            overrides[key] = ts[key].strip()
    numeric_ranges: dict[str, tuple[float, float]] = {
        "font_size": (16, 200),
        "outline_width": (0, 20),
        "shadow": (0, 20),
        "bg_opacity": (0, 1),
        "letter_spacing": (-5, 40),
    }
    for key, (low, high) in numeric_ranges.items():
        number = _num(ts.get(key))
        if number is not None:
            overrides[key] = _clamp(number, low, high)
    max_chars = _num(ts.get("max_chars_per_line"))
    if max_chars is not None:
        overrides["max_chars_per_line"] = int(_clamp(round(max_chars), 4, 40))
    position = ts.get("position")
    if position in ("top", "center", "bottom"):
        overrides["position"] = position
    return replace(base, **overrides)


def _font_for(spec: StyleSpec) -> tuple[str, bool]:
    table = _WEIGHT_FAMILIES.get(spec.font_family)
    if table:
        if spec.font_weight in table:
            return table[spec.font_weight]
        nearest = {"thin": "light", "demilight": "light", "semibold": "bold", "extralight": "light"}
        fallback = nearest.get(spec.font_weight, "regular")
        return table.get(fallback, table["regular"])
    return spec.font_family, spec.font_weight in ("bold", "black", "semibold")


def _ass_color(color: str, opacity: float = 1.0) -> str:
    parsed = _parse_color(color) or (255, 255, 255, 1.0)
    r, g, b, own_opacity = parsed
    alpha = round(255 * (1 - _clamp(opacity * own_opacity, 0, 1)))
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def _ass_rgb(color: str) -> str:
    """Colour for a ``\\1c`` override tag (``&HBBGGRR&``; alpha is not part of it)."""
    r, g, b, _ = _parse_color(color) or (255, 255, 255, 1.0)
    return f"&H{b:02X}{g:02X}{r:02X}&"


def _ass_alpha(opacity: float) -> str:
    return f"&H{round(255 * (1 - _clamp(opacity, 0, 1))):02X}&"


# --------------------------------------------------------------------------- text measurement

# Approximate advance widths as a fraction of the ASS font size (Noto CJK: the
# em box is ~0.69 × Fontsize). Slightly generous so estimated lines never
# overflow in practice.
_W_FULL = 0.70
_W_HALF_KANA = 0.36
_W_SPACE = 0.17
_W_UPPER = 0.49
_W_LOWER = 0.41
_W_DIGIT = 0.41
_W_PUNCT = 0.30
_W_OTHER = 0.45


def _is_wide(ch: str) -> bool:
    return unicodedata.east_asian_width(ch) in ("W", "F")


def _char_width(ch: str) -> float:
    """Advance of ``ch`` as a multiple of the font size (excluding letter spacing)."""
    if ch == " " or ch == " ":
        return _W_SPACE
    if _is_wide(ch):
        return _W_FULL
    if "｡" <= ch <= "ﾟ":
        return _W_HALF_KANA
    if ch.isascii():
        if ch.isupper():
            return _W_UPPER
        if ch.islower():
            return _W_LOWER
        if ch.isdigit():
            return _W_DIGIT
        return _W_PUNCT
    if unicodedata.category(ch).startswith("M") or ch in _ZERO_WIDTH:
        return 0.0
    return _W_OTHER


_ZERO_WIDTH = frozenset("​‌‍⁠﻿")


def _line_width_px(text: str, font_size: float, spacing: float) -> float:
    if not text:
        return 0.0
    return sum(_char_width(ch) for ch in text) * font_size + spacing * len(text)


def text_width_units(text: str) -> float:
    """Width of ``text`` in full-width-character units (``max_chars_per_line`` scale)."""
    return sum(_char_width(ch) for ch in text) / _W_FULL


# --------------------------------------------------------------------------- escaping


def _normalize_text(text: str) -> str:
    """Single-line, printable text (newlines/tabs → space). Internal spaces are kept."""
    text = re.sub(r"[\r\n\t  \v\f]+", " ", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cc")
    return text.strip()


def escape_ass_text(text: str) -> str:
    """Escape user text for an ASS ``Dialogue`` text field.

    ``{``/``}`` would open/close override blocks (libass renders ``\\{``/``\\}``
    literally) and a backslash before ``N``/``n``/``h`` would become a line
    break or hard space; libass has no escape for a backslash, so a WORD
    JOINER (zero-width, invisible) is inserted after it. Newlines become spaces.
    """
    text = re.sub(r"[\r\n\t  \v\f]", " ", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cc")
    text = text.replace("\\", "\\⁠")
    return text.replace("{", "\\{").replace("}", "\\}")


# --------------------------------------------------------------------------- line breaking

# 行頭禁則: characters that must not start a line.
_NO_LINE_START = frozenset(
    "、。，．,.!?！？)）]］}｝」』】〉》〕〙〗〟’”»・：；:;ーｰァィゥェォッャュョヮヵヶ"
    "ぁぃぅぇぉっゃゅょゎゕゖ々〻‐゠–—〜～…‥ヽヾゝゞ%％"
)
# 行末禁則: characters that must not end a line.
_NO_LINE_END = frozenset("([（［｛「『【〈《〔〘〖〝‘“«")
_BREAK_AFTER = frozenset("、。，．,.!?！？…‥」』）)】")
_STRONG_PARTICLES = frozenset("はがをにへでも")  # almost always end a bunsetsu
_WEAK_PARTICLES = frozenset("とのやかねよ")  # often particles, but also inside words


def _script(ch: str) -> str:
    if ch.isspace():
        return "space"
    code = ord(ch)
    if 0x3040 <= code <= 0x309F:
        return "hira"
    if 0x30A0 <= code <= 0x30FF or 0xFF66 <= code <= 0xFF9F or 0x31F0 <= code <= 0x31FF:
        return "kata"
    if 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF or 0xF900 <= code <= 0xFAFF or ch in "々〆ヶ":
        return "kanji"
    if ch.isalnum() and not _is_wide(ch):
        return "latin"
    if ch.isalnum():
        return "wide_alnum"
    return "punct"


def _is_word_char(ch: str) -> bool:
    return (ch.isalnum() and not _is_wide(ch)) or ch in "'’-_"


def _break_cost(text: str, pos: int, forbidden: frozenset[int]) -> float | None:
    """Cost of breaking before ``text[pos]`` (None = not allowed). Lower is better."""
    if pos <= 0 or pos >= len(text) or pos in forbidden:
        return None
    prev, nxt = text[pos - 1], text[pos]
    if nxt.isspace():
        return None  # breaks happen *after* a space run (the run is dropped)
    if prev.isspace():
        return -0.8  # the lyricist's own phrase break
    if nxt in _NO_LINE_START or prev in _NO_LINE_END:
        return None
    if _is_word_char(prev) and _is_word_char(nxt):
        return None  # never inside a Latin word / number
    if prev in _BREAK_AFTER:
        return -0.6
    sp, sn = _script(prev), _script(nxt)
    if sp == "hira" and sn in ("kanji", "kata", "latin", "wide_alnum"):
        # Kana → content word: most likely a bunsetsu boundary (e.g. 知らない|風に).
        bonus = 0.15 if prev in _STRONG_PARTICLES else 0.05 if prev in _WEAK_PARTICLES else 0.0
        return -0.3 - bonus
    if sp == "hira" and sn == "hira":
        run_start = pos - 1
        while run_start > 0 and _script(text[run_start - 1]) == "hira":
            run_start -= 1
        inflection = run_start > 0 and _script(text[run_start - 1]) in ("kanji", "kata")
        if prev in _STRONG_PARTICLES:
            return 0.1 if inflection else -0.05
        if inflection:
            return 0.5  # inside okurigana / a verb ending (吹か|れて, 言え|なかった)
        return 0.15 if prev in _WEAK_PARTICLES else 0.3
    if sp in ("kanji", "kata") and sn == "hira":
        return 0.45  # okurigana / particle attached to its word
    if sp == "kata" and sn == "kata":
        return 0.6  # inside a loanword
    if sp == "kanji" and sn == "kanji":
        return 0.4  # inside a compound
    if sp != sn:
        return 0.0
    return 0.2


def _split_at(text: str, pos: int) -> tuple[str, str, int]:
    """Split before ``pos``, dropping the whitespace run that precedes it."""
    left_end = pos
    while left_end > 0 and text[left_end - 1].isspace():
        left_end -= 1
    return text[:left_end], text[pos:], left_end


@dataclass(frozen=True)
class TextLayout:
    lines: list[str]
    spans: list[tuple[int, int]]  # [start, end) offsets of each line in the source text
    font_size: float


def wrap_text(
    text: str,
    *,
    max_width: float,
    font_size: float = 1.0,
    spacing: float = 0.0,
    forbidden_breaks: Iterable[int] = (),
) -> tuple[list[str], list[tuple[int, int]]]:
    """Break ``text`` into one or two balanced lines no wider than ``max_width``.

    Width is estimated in the same unit as ``max_width`` (``font_size``-scaled;
    with ``font_size=1`` widths are multiples of the font size). ``max_width``
    is a soft limit once two lines are needed: a natural break slightly over
    it beats an ugly one under it (the caller shrinks the font only when a
    line exceeds the safe width). Returns the lines and their ``[start, end)``
    spans in ``text``.
    """
    text = text.strip()
    if not text:
        return [], []
    if _line_width_px(text, font_size, spacing) <= max_width + 1e-6:
        return [text], [(0, len(text))]
    forbidden = frozenset(forbidden_breaks)
    best: tuple[float, int] | None = None
    for pos in range(1, len(text)):
        cost = _break_cost(text, pos, forbidden)
        if cost is None:
            continue
        left, right, _ = _split_at(text, pos)
        w1 = _line_width_px(left, font_size, spacing)
        w2 = _line_width_px(right, font_size, spacing)
        overflow = max(0.0, max(w1, w2) - max_width) / max(max_width, 1e-6)
        # Balance vs. boundary quality; ``max_width`` is a soft limit (the caller
        # shrinks the font only past the safe width), and a slightly longer
        # second line (pyramid shape) reads better than the reverse.
        score = abs(w1 - w2) / max(w1 + w2, 1e-6) + 0.35 * cost + 0.35 * overflow + (0.02 if w1 > w2 else 0.0)
        if best is None or score < best[0]:
            best = (score, pos)
    chosen = best
    if chosen is None:
        return [text], [(0, len(text))]
    pos = chosen[1]
    left, right, left_end = _split_at(text, pos)
    return [left, right], [(0, left_end), (pos, len(text))]


def _greedy_wrap(
    text: str, max_width: float, font_size: float, spacing: float, forbidden: frozenset[int]
) -> tuple[list[str], list[tuple[int, int]]]:
    """Last resort for extremely long lines: as many lines as needed, never overflow."""
    spans: list[tuple[int, int]] = []
    start = 0
    n = len(text)
    while start < n:
        while start < n and text[start].isspace():
            start += 1
        if start >= n:
            break
        last_ok: int | None = None
        pos = start + 1
        while pos <= n:
            if _line_width_px(text[start:pos].rstrip(), font_size, spacing) > max_width and last_ok is not None:
                break
            if pos == n or _break_cost(text, pos, forbidden) is not None:
                last_ok = pos
            pos += 1
        end = last_ok if last_ok is not None else n
        left_end = end
        while left_end > start and text[left_end - 1].isspace():
            left_end -= 1
        spans.append((start, left_end))
        start = end
    return [text[a:b] for a, b in spans], spans


def layout_text(
    text: str,
    spec: StyleSpec,
    *,
    width: int = BASE_WIDTH,
    height: int = BASE_HEIGHT,
    forbidden_breaks: Iterable[int] = (),
) -> TextLayout:
    """Wrap ``text`` for ``spec`` and pick the font size that fits the safe width."""
    scale = _ui_scale(width, height)
    area = safe_area(width, height)
    font_size = spec.font_size * scale
    spacing = spec.letter_spacing * scale
    extra = 2 * _horizontal_extent(spec, scale)
    available = (area.right - area.left) - extra
    preferred = min(spec.max_chars_per_line * (_W_FULL * font_size + spacing), available)
    forbidden = frozenset(forbidden_breaks)

    lines, spans = wrap_text(
        text, max_width=preferred, font_size=font_size, spacing=spacing, forbidden_breaks=forbidden
    )
    widest = max((_line_width_px(line, font_size, spacing) for line in lines), default=0.0)
    if widest > available:
        # Shrink until the chosen lines fit the safe width (text is never cut).
        factor = max(_MIN_SHRINK, available / widest)
        font_size *= factor
        spacing *= factor
        widest = max(_line_width_px(line, font_size, spacing) for line in lines)
        if widest > available + 0.5:
            lines, spans = _greedy_wrap(text.strip(), available, font_size, spacing, forbidden)
    return TextLayout(lines=lines, spans=spans, font_size=round(font_size, 1))


def _layout_block(
    text: str,
    spec: StyleSpec,
    *,
    width: int,
    height: int,
    forbidden_breaks: Iterable[int] = (),
) -> TextLayout:
    """:func:`layout_text` with ``"\\n"`` as a forced break between stacked parts.

    Each part (one lyric line of a same-time group) is wrapped on its own; the
    block uses the smallest part font size so every line has the same pitch.
    Spans are offsets in the joined ``text``.
    """
    parts = text.split("\n")
    if len(parts) == 1:
        return layout_text(text, spec, width=width, height=height, forbidden_breaks=forbidden_breaks)
    forbidden = frozenset(forbidden_breaks)
    lines: list[str] = []
    spans: list[tuple[int, int]] = []
    sizes: list[float] = []
    offset = 0
    for part in parts:
        local = {i - offset for i in forbidden if offset < i < offset + len(part)}
        layout = layout_text(part, spec, width=width, height=height, forbidden_breaks=local)
        lines += layout.lines
        spans += [(a + offset, b + offset) for a, b in layout.spans]
        if layout.lines:
            sizes.append(layout.font_size)
        offset += len(part) + 1
    return TextLayout(lines=lines, spans=spans, font_size=min(sizes, default=round(spec.font_size, 1)))


# --------------------------------------------------------------------------- vertical layout


def _blur_extent(blur: float) -> float:
    return 2.5 * blur


def _horizontal_extent(spec: StyleSpec, scale: float) -> float:
    if spec.bg_opacity > 0:
        return _box_padding(spec, scale)
    soft = 0.0
    if spec.shadow_mode == "soft" and spec.shadow > 0:
        soft = spec.shadow * scale + _blur_extent(spec.soft_shadow_blur * scale)
    hard = spec.shadow * scale if spec.shadow_mode == "hard" else 0.0
    return spec.outline_width * scale + _blur_extent(spec.edge_blur * scale) + max(soft, hard)


def _box_padding(spec: StyleSpec, scale: float) -> float:
    # The text keeps its outline inside the box, so the box always encloses it.
    return max(spec.box_padding, spec.outline_width + _blur_extent(spec.edge_blur)) * scale


def _vertical_extents(spec: StyleSpec, scale: float) -> tuple[float, float]:
    """Pixels drawn above the first line box and below the last line box."""
    if spec.bg_opacity > 0:
        pad = _box_padding(spec, scale)
        return pad, pad
    outline = spec.outline_width * scale + _blur_extent(spec.edge_blur * scale)
    if spec.shadow_mode == "soft" and spec.shadow > 0:
        below_shadow = spec.shadow * scale + _blur_extent(spec.soft_shadow_blur * scale)
        above_shadow = max(0.0, _blur_extent(spec.soft_shadow_blur * scale) - spec.shadow * scale)
    else:
        below_shadow = spec.shadow * scale
        above_shadow = 0.0
    above = max(outline, above_shadow)
    below = max(outline + below_shadow, below_shadow) + spec.rise * scale
    return above, below


def _line_tops(n_lines: int, font_size: float, spec: StyleSpec, width: int, height: int) -> list[float]:
    """Top y of each line box so the whole block (incl. effects) sits inside the safe area."""
    scale = _ui_scale(width, height)
    area = safe_area(width, height)
    if spec.bg_opacity > 0:
        pitch = font_size + 2 * _box_padding(spec, scale)  # boxes touch, never overlap
    else:
        pitch = font_size * spec.line_spacing
    block = font_size + (n_lines - 1) * pitch
    above, below = _vertical_extents(spec, scale)
    inset = spec.inset * scale
    low = area.top + above
    high = area.bottom - below - block
    if spec.position == "top":
        top = low + inset
    elif spec.position == "center":
        top = area.center_y - block / 2 + (above - below) / 2
    else:
        top = high - inset
    if high >= low:
        top = _clamp(top, low, high)
    else:  # taller than the safe area (only with absurd overrides): keep the top visible
        top = low
    return [top + i * pitch for i in range(n_lines)]


# --------------------------------------------------------------------------- timing helpers


@dataclass(frozen=True)
class _Timed:
    text: str
    start: float  # absolute song seconds
    end: float
    words: list[dict[str, Any]] | None


def _timed_lines(lines: Sequence[Mapping[str, Any]]) -> list[_Timed]:
    """Timed, non-overlapping display units.

    Lines starting within ``_SAME_START`` of each other (an LRC with the
    translation or a second singer on the same timestamp) become one unit whose
    text joins the lines with ``"\\n"`` (laid out as stacked parts); its end is
    the latest end, and word timings are kept for the first line only.
    """
    raw: list[tuple[float, float | None, str, list[dict[str, Any]] | None]] = []
    for line in lines:
        start = _num(line.get("start"))
        text = _normalize_text(str(line.get("text") or ""))
        if start is None or not text:
            continue
        words = line.get("words")
        raw.append((start, _num(line.get("end")), text, words if isinstance(words, list) else None))
    raw.sort(key=lambda item: item[0])
    groups: list[list[tuple[float, float | None, str, list[dict[str, Any]] | None]]] = []
    for item in raw:
        if groups and item[0] - groups[-1][0][0] <= _SAME_START:
            groups[-1].append(item)
        else:
            groups.append([item])
    result: list[_Timed] = []
    for index, group in enumerate(groups):
        start = group[0][0]
        next_start = groups[index + 1][0][0] if index + 1 < len(groups) else None
        ends = [end for _, end, _, _ in group if end is not None and end > start]
        end = max(ends) if ends else (next_start if next_start is not None else start + _DEFAULT_LINE_HOLD)
        if next_start is not None:
            end = min(end, next_start)  # overlapping lines would collide visually
        text = "\n".join(dict.fromkeys(item[2] for item in group))
        if end - start >= _MIN_EVENT:
            result.append(_Timed(text=text, start=start, end=end, words=group[0][3]))
    return result


def _beat_fade(event_start: float, event_end: float, beats: Sequence[float] | None) -> tuple[float, float]:
    """Fade lengths whose ends land on a beat (or half beat) near the line edges."""
    fade_in, fade_out = _DEFAULT_FADE_IN, _DEFAULT_FADE_OUT
    if beats:
        grid = sorted(b for b in beats if math.isfinite(b))
        points = list(grid)
        points += [(a + b) / 2 for a, b in itertools.pairwise(grid) if 0 < b - a < 2.0]
        low, high = _FADE_WINDOW
        ins = [p - event_start for p in points if low <= p - event_start <= high]
        outs = [event_end - p for p in points if low <= event_end - p <= high]
        if ins:
            fade_in = min(ins, key=lambda d: abs(d - _DEFAULT_FADE_IN))
        if outs:
            fade_out = min(outs, key=lambda d: abs(d - _DEFAULT_FADE_OUT))
    total = event_end - event_start
    budget = 0.7 * total
    if fade_in + fade_out > budget > 0:
        ratio = budget / (fade_in + fade_out)
        fade_in, fade_out = fade_in * ratio, fade_out * ratio
    return fade_in, fade_out


def _ass_time(seconds: float) -> str:
    cs = max(0, round(seconds * 100))
    hours, rem = divmod(cs, 360000)
    minutes, rem = divmod(rem, 6000)
    secs, cs = divmod(rem, 100)
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{cs:02d}"


# --------------------------------------------------------------------------- karaoke


@dataclass(frozen=True)
class _Syllable:
    start_idx: int  # [start_idx, end_idx) in the line text
    end_idx: int
    start: float  # absolute seconds
    end: float


def _map_words(text: str, words: Sequence[Any]) -> list[_Syllable] | None:
    """Locate each timed word in ``text``. None when words don't match the text."""
    syllables: list[_Syllable] = []
    pos = 0
    for word in words:
        if not isinstance(word, Mapping):
            return None
        wt = _normalize_text(str(word.get("text") or ""))
        start, end = _num(word.get("start")), _num(word.get("end"))
        if not wt:
            continue
        if start is None:
            return None
        if end is None or end < start:
            end = start
        found = text.find(wt, pos)
        if found < 0:
            return None
        skipped = text[pos:found]
        if any(ch.isalnum() for ch in skipped):  # an unmatched real character
            return None
        syllables.append(_Syllable(found, found + len(wt), start, end))
        pos = found + len(wt)
    if not syllables or any(ch.isalnum() for ch in text[pos:]):
        return None
    return syllables


def _even_syllables(text: str, start: float, end: float) -> list[_Syllable]:
    """Even split: one syllable per CJK character, per Latin word; weighted by width."""
    units: list[tuple[int, int]] = []
    index = 0
    n = len(text)
    while index < n:
        ch = text[index]
        if ch.isspace():
            index += 1
            continue
        if _is_word_char(ch):
            stop = index
            while stop < n and _is_word_char(text[stop]):
                stop += 1
            units.append((index, stop))
            index = stop
            continue
        units.append((index, index + 1))
        index += 1
    if not units:
        return []
    weights = [max(sum(_char_width(c) for c in text[a:b]), 0.15) for a, b in units]
    total = sum(weights)
    syllables: list[_Syllable] = []
    acc = 0.0
    for (a, b), weight in zip(units, weights, strict=True):
        s = start + (end - start) * acc / total
        acc += weight
        e = start + (end - start) * acc / total
        syllables.append(_Syllable(a, b, s, e))
    return syllables


def _syllables_for(timed: _Timed) -> list[_Syllable]:
    """Karaoke syllables of the first part (stacked parts after it are shown without a sweep)."""
    first = timed.text.split("\n", 1)[0]
    if timed.words:
        mapped = _map_words(first, timed.words)
        if mapped:
            return mapped
    return _even_syllables(first, timed.start, timed.end)


def _karaoke_body(
    text: str,
    span: tuple[int, int],
    syllables: Sequence[_Syllable],
    event_start: float,
    event_end: float,
) -> str:
    """``{\\k..}{\\kf..}`` text for one visual line; times relative to the event start."""
    total_cs = max(0, round((event_end - event_start) * 100))

    def mark(t: float) -> int:
        return int(_clamp(round((t - event_start) * 100), 0, total_cs))

    a, b = span
    inside = [s for s in syllables if s.start_idx < b and s.end_idx > a]
    parts: list[str] = []
    cursor = 0
    pos = a
    for index, syl in enumerate(inside):
        # Text not covered by a syllable (spaces, punctuation) rides with the
        # following syllable when it leads the line, otherwise with the previous one.
        piece_start = pos if index == 0 else max(syl.start_idx, a)
        next_start = inside[index + 1].start_idx if index + 1 < len(inside) else b
        piece_end = min(max(next_start, syl.end_idx), b)
        piece = text[piece_start:piece_end]
        pos = piece_end
        s_cs, e_cs = max(mark(syl.start), cursor), mark(syl.end)
        e_cs = max(e_cs, s_cs)
        if s_cs > cursor:
            parts.append(f"{{\\k{s_cs - cursor}}}")
        parts.append(f"{{\\kf{e_cs - s_cs}}}{escape_ass_text(piece)}")
        cursor = e_cs
    if not inside:
        parts.append(escape_ass_text(text[a:b]))
    return "".join(parts)


# --------------------------------------------------------------------------- ASS document


def _style_line(name: str, spec: StyleSpec, scale: float, *, karaoke: bool, box: bool = False) -> str:
    """ASS style for the text (``box=False``) or for the background box under it (``box=True``).

    The box is always black at ``bg_opacity`` (as in the editor preview) with an
    invisible glyph fill; the text style keeps the user's outline colour and
    width in box mode too.
    """
    fontname, bold = _font_for(spec)
    font_size = round(spec.font_size * scale, 1)
    spacing = round(spec.letter_spacing * scale, 2)
    if box:
        border_style = 3
        outline = round(_box_padding(spec, scale), 1)
        outline_colour = _ass_color("#000000", spec.bg_opacity)
        shadow = 0
    else:
        border_style = 1
        outline = round(spec.outline_width * scale, 2)
        outline_colour = _ass_color(spec.outline_color, spec.outline_opacity)
        hard = spec.shadow_mode == "hard" and spec.bg_opacity <= 0
        shadow = round(spec.shadow * scale, 2) if hard else 0
    if box:
        primary = secondary = _ass_color("#000000", 0.0)  # glyphs invisible: only the box is drawn
    elif karaoke:
        primary = _ass_color(spec.highlight_color)  # sung (sweep target)
        secondary = _ass_color(spec.primary_color)  # not yet sung
    else:
        primary = _ass_color(spec.primary_color)
        secondary = _ass_color(spec.highlight_color)
    back = _ass_color("#000000", spec.shadow_opacity)
    fields = [
        name, fontname, f"{font_size:g}", primary, secondary, outline_colour, back,
        "-1" if bold else "0", "0", "0", "0", "100", "100", f"{spacing:g}", "0",
        str(border_style), f"{outline:g}", f"{shadow:g}", "2", "0", "0", "0", "1",
    ]  # fmt: skip
    return "Style: " + ",".join(fields)


def build_ass(
    lines: Sequence[Mapping[str, Any]],
    *,
    style_id: str,
    text_style: Mapping[str, Any] | None,
    segment_start: float,
    duration: float,
    width: int = BASE_WIDTH,
    height: int = BASE_HEIGHT,
    beats: Sequence[float] | None = None,
) -> str:
    """Build an ASS script for the lyric lines visible in ``[segment_start, segment_start+duration]``.

    ``lines`` are :data:`LyricLine` dicts with absolute song times; untimed
    lines are skipped. Event times are relative to ``segment_start`` and
    clipped to ``[0, duration]``. ``beats`` (absolute seconds) align the
    ``fade`` style's fade-in/out ends to the music.
    """
    if style_id not in STYLE_DEFAULTS:
        style_id = "simple"
    spec = resolve_text_style(style_id, text_style)
    scale = _ui_scale(width, height)
    karaoke = style_id == "karaoke"
    boxed = spec.bg_opacity > 0
    soft_shadow = spec.shadow_mode == "soft" and spec.shadow > 0 and not boxed

    header = [
        "[Script Info]",
        "; Generated by AI Music Video Factory",
        "Title: Lyrics",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: None",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        _style_line("Lyric", spec, scale, karaoke=karaoke),
        *([_style_line("LyricBox", spec, scale, karaoke=False, box=True)] if boxed else []),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    events: list[str] = []
    seg_end = segment_start + duration
    center_x = width / 2

    for timed in _timed_lines(lines):
        if timed.end <= segment_start or timed.start >= seg_end:
            continue
        rel_start = max(0.0, timed.start - segment_start)
        rel_end = min(duration, timed.end - segment_start)
        if rel_end - rel_start < _MIN_EVENT:
            continue
        syllables = _syllables_for(timed) if karaoke else []
        forbidden = {i for s in syllables for i in range(s.start_idx + 1, s.end_idx)}
        layout = _layout_block(timed.text, spec, width=width, height=height, forbidden_breaks=forbidden)
        if not layout.lines:
            continue
        tops = _line_tops(len(layout.lines), layout.font_size, spec, width, height)

        if style_id == "fade":
            fade_in, fade_out = _beat_fade(rel_start + segment_start, rel_end + segment_start, beats)
            if timed.start < segment_start:
                fade_in = 0.0  # already on screen when the clip starts
        else:
            fade_in, fade_out = spec.fade_in, spec.fade_out
            budget = 0.5 * (rel_end - rel_start)
            fade_in, fade_out = min(fade_in, budget), min(fade_out, budget)
            if timed.start < segment_start:
                fade_in = 0.0
        fad = f"\\fad({round(fade_in * 1000)},{round(fade_out * 1000)})"
        fs_tag = f"\\fs{layout.font_size:g}" if abs(layout.font_size - round(spec.font_size * scale, 1)) > 0.05 else ""
        if fs_tag and spec.letter_spacing:
            ratio = layout.font_size / (spec.font_size * scale)
            fs_tag += f"\\fsp{round(spec.letter_spacing * scale * ratio, 2):g}"
        rise = spec.rise * scale
        start_s, end_s = _ass_time(rel_start), _ass_time(rel_end)

        event_start_abs, event_end_abs = rel_start + segment_start, rel_end + segment_start
        first_part_end = timed.text.find("\n") if "\n" in timed.text else len(timed.text)
        for line_text, span, top in zip(layout.lines, layout.spans, tops, strict=True):
            x = round(center_x, 1)
            y = round(top, 1)
            if rise > 0 and fade_in > 0:
                placement = f"\\an8\\move({x:g},{y + rise:g},{x:g},{y:g},0,{round(fade_in * 1000)})"
            else:
                placement = f"\\an8\\pos({x:g},{y:g})"
            # \\blur softens only the outline while a border exists; without one it would blur the fill.
            has_edge = spec.edge_blur > 0 and spec.outline_width > 0
            blur = f"\\blur{spec.edge_blur * scale:g}" if has_edge else ""
            colour = ""
            if karaoke and span[0] < first_part_end:
                body = _karaoke_body(timed.text, span, syllables, event_start_abs, event_end_abs)
            else:
                body = escape_ass_text(line_text)
                if karaoke:  # stacked translation/duet part: static, in the not-yet-sung colour
                    colour = f"\\1c{_ass_rgb(spec.primary_color)}"
            events.append(
                f"Dialogue: 1,{start_s},{end_s},Lyric,,0,0,0,,{{{placement}{fad}{fs_tag}{blur}{colour}}}{body}"
            )
            if boxed:
                events.append(
                    f"Dialogue: 0,{start_s},{end_s},LyricBox,,0,0,0,,{{{placement}{fad}{fs_tag}}}"
                    f"{escape_ass_text(line_text)}"
                )
            if soft_shadow:
                offset = spec.shadow * scale
                if rise > 0 and fade_in > 0:
                    shadow_place = (
                        f"\\an8\\move({x + offset:g},{y + rise + offset:g},{x + offset:g},{y + offset:g},"
                        f"0,{round(fade_in * 1000)})"
                    )
                else:
                    shadow_place = f"\\an8\\pos({x + offset:g},{y + offset:g})"
                shadow_tags = (
                    f"{shadow_place}{fad}{fs_tag}\\bord0\\shad0\\1c&H000000&\\1a{_ass_alpha(spec.shadow_opacity)}"
                    f"\\blur{spec.soft_shadow_blur * scale:g}"
                )
                events.append(
                    f"Dialogue: 0,{start_s},{end_s},Lyric,,0,0,0,,{{{shadow_tags}}}{escape_ass_text(line_text)}"
                )
    return "\n".join(header + events) + "\n"
