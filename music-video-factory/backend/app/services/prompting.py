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

import logging
import re
import unicodedata
from collections.abc import Iterable
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "FALLBACK_BETA",
    "GLOSSARY_JA_EN",
    "LIKENESS_SAFETY_CLAUSE",
    "MAX_PROMPT_CHARS",
    "NO_TEXT_CLAUSE",
    "first_text_block",
    "glossary_keywords",
    "glossary_translate",
    "make_claude_client",
    "prompt_body_budget",
    "sanitize_prompt",
    "strip_forbidden_names",
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

    Removes the forbidden names (artist name, …) and real-person / celebrity /
    look-alike phrasing, then appends the likeness-safety and no-text clauses.
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

    found: list[str] = []
    for pattern in (_REAL_PERSON_EN, _REAL_PERSON_JA):
        found.extend(m.group(0) for m in pattern.finditer(body))
        body = pattern.sub(" ", body)
    for phrase in dict.fromkeys(p.strip() for p in found):
        warnings.append(f"実在の人物を示す表現「{phrase}」を削除しました（架空の人物のみを生成します）")

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
