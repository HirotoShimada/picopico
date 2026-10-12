"""Storyboard generation: Claude or deterministic template (ARCHITECTURE.md §6.3).

A storyboard is one *world bible* (fictional protagonist, location, palette,
look) plus N scenes mapped onto the song's sections and lyric lines. Every
scene carries a complete English text-to-video prompt, so each clip can be
generated independently and still look like part of the same music video.

* **Claude** (``use_claude`` and a route configured — Anthropic directly with
  ``ANTHROPIC_API_KEY``, else OpenRouter with ``OPENROUTER_API_KEY``, see
  :mod:`app.services.llm`): one structured-output request with the same
  ``STORYBOARD_SCHEMA`` on both routes; the system prompt is stable
  (cacheable) and all song-specific data goes into the user message.
  Refusals (incl. OpenRouter ``content_filter``), truncated answers, API
  errors and unparsable output fall back to the template with a Japanese
  warning. The result says which route was used (``route``) and which model
  answered (``model``, e.g. ``anthropic/claude-opus-5.5`` via OpenRouter).
* **Template**: deterministic for a given seed; picks scene types, camera
  angles and moves from the preset vocabulary according to each section's
  energy.

Every ``prompt_en`` passes through :func:`prompting.sanitize_prompt` with the
artist name (its kana romanisations and any aliases) forbidden, plus every
person named in the user's free text (custom world, mood, purpose) and every
real person Claude reports in ``detected_real_people``. In the template path
free text is screened first (:func:`prompting.screen_free_text`) and the mood
only contributes allow-listed mood words, never raw Latin-script text.

:func:`estimate_world` merges the audio mood, the lyrics and the user's
mood/purpose into an emotion estimate with recommended world presets (included
in the storyboard as ``world_estimate`` and sent to Claude as context; the
template uses it as the mood when the user gave none).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.services.llm import (
    LlmCallError,
    LlmReply,
    api_label,
    complete,
    pricing_model_id,
    route_of,
)
from app.services.presets import (
    CAMERA_ANGLES,
    CAMERA_MOVES,
    SCENE_TYPES,
    SECTION_LABELS_JA,
    WORLD_PRESETS,
    WorldPreset,
    get_preset,
)
from app.services.prompting import (
    MAX_PROMPT_CHARS,
    artist_name_variants,
    glossary_keywords,
    make_claude_client,
    mood_keywords,
    prompt_body_budget,
    sanitize_prompt,
    screen_free_text,
    strip_forbidden_names,
    strip_person_references,
    translate_to_english,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BASE_NEGATIVE_EN",
    "DEFAULT_SCENES",
    "MAX_SCENES",
    "REALISM_EN",
    "STORYBOARD_PROMPT_CHARS",
    "STORYBOARD_SCHEMA",
    "SYSTEM_PROMPT",
    "claude_cost_usd",
    "estimate_world",
    "generate_storyboard",
]

DEFAULT_SCENES = 8
MAX_SCENES = 30
# Leaves room for the planner's "alternate take" suffix under MAX_PROMPT_CHARS.
STORYBOARD_PROMPT_CHARS = MAX_PROMPT_CHARS - 50
MAX_NEGATIVE_CHARS = 700
SECTION_LABELS: tuple[str, ...] = ("intro", "verse", "chorus", "bridge", "outro", "break")

REALISM_EN = (
    "Vertical 9:16 composition, photorealistic live-action footage, natural skin texture, "
    "realistic hands, subtle natural motion"
)
BASE_NEGATIVE_EN = (
    "deformed hands, extra fingers, fused fingers, missing fingers, extra limbs, warped face, "
    "distorted facial features, asymmetrical eyes, morphing, melting, flicker, jitter, frame stutter, "
    "sudden cuts, text, letters, subtitles, captions, watermark, logo, signature, cartoon, anime, "
    "illustration, painting, CGI, 3D render, video game look, plastic skin, waxy skin, oversaturated, "
    "overexposed, blurry, low resolution, compression artifacts, duplicated subjects, "
    "celebrity likeness, real person likeness"
)

# $ per million tokens: (input, output, cache read). Cache writes (5 min TTL) cost 1.25× input.
# OpenRouter ids (``anthropic/claude-opus-5.5``) are mapped by ``llm.pricing_model_id``.
_CLAUDE_PRICING: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),  # Anthropic list price (docs/VIDEO_PROVIDERS.md §8.4, unverified)
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
}
_DEFAULT_PRICING = _CLAUDE_PRICING["claude-opus-5-5"]

# Shots that should stay intimate: no big crane/drone moves; wide/static scenes
# don't suit aggressive pushes. Overhead framing only where it reads naturally.
_INTIMATE_SCENES = frozenset(
    {
        "close_portrait",
        "profile_window",
        "ambient_detail",
        "nature_macro",
        "water_reflection",
        "train_window",
        "lying_down",
    }
)
_BIG_MOVES = frozenset({"drone_rise", "crane_up", "follow_behind"})
_CALM_SCENES = frozenset({"establishing_wide", "sky_drift", "back_view"})
_AGGRESSIVE_MOVES = frozenset({"fast_push", "follow_behind"})
_OVERHEAD_OK = frozenset(
    {
        "lying_down",
        "nature_macro",
        "establishing_wide",
        "water_reflection",
        "spin_sway",
        "ambient_detail",
        "street_flow",
    }
)
_DEFAULT_STRUCTURE = ("intro", "verse", "chorus", "verse", "chorus", "bridge", "chorus", "outro")


# --- shared helpers ------------------------------------------------------------------------


def _stable_int(*parts: object) -> int:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _sentence(text: str) -> str:
    text = text.strip().rstrip(".")
    return f"{text[:1].upper()}{text[1:]}." if text else ""


def _verbatim(text: str) -> str:
    """A sentence that keeps ``text`` exactly (consistency tokens must appear verbatim in every prompt)."""
    text = text.strip().rstrip(".")
    return f"{text}." if text else ""


def _compose(segments: Sequence[tuple[int, str]], budget: int) -> str:
    """Join sentences in order, dropping the least important (highest number) until the text fits."""
    kept = [(prio, text) for prio, text in segments if text]
    while len(" ".join(t for _, t in kept)) > budget and len(kept) > 1:
        worst = max(range(len(kept)), key=lambda i: (kept[i][0], i))
        if kept[worst][0] == 0:
            break
        kept.pop(worst)
    return " ".join(t for _, t in kept)


def _merge_negative(*parts: str) -> str:
    seen: dict[str, str] = {}
    for part in parts:
        for item in re.split(r"[,\n;]+", part or ""):
            item = item.strip().strip(".")
            if item and item.lower() not in seen:
                seen[item.lower()] = item
    result = ""
    for item in seen.values():
        candidate = f"{result}, {item}" if result else item
        if len(candidate) > MAX_NEGATIVE_CHARS:
            break
        result = candidate
    return result


def claude_cost_usd(model: str | None, usage: Mapping[str, int] | None) -> float:
    """USD cost of one Claude call from its ``usage`` (Opus 5.5: $4/MTok in, $20/MTok out).

    ``model`` may be an Anthropic id (``claude-opus-5-5``) or an OpenRouter id
    (``anthropic/claude-opus-5.5``); unknown models are priced like Opus 5.5.
    """
    if not usage:
        return 0.0
    price_in, price_out, price_cache_read = _CLAUDE_PRICING.get(pricing_model_id(model), _DEFAULT_PRICING)
    cost = (
        usage.get("input_tokens", 0) * price_in
        + usage.get("output_tokens", 0) * price_out
        + usage.get("cache_creation_input_tokens", 0) * price_in * 1.25
        + usage.get("cache_read_input_tokens", 0) * price_cache_read
    ) / 1_000_000
    return round(cost, 6)


# --- song structure ----------------------------------------------------------------------------


@dataclass
class _Slot:
    section: str
    start: float | None
    end: float | None
    energy: float


def _energy_class(slot: _Slot) -> str:
    if slot.section == "chorus" or slot.energy >= 0.7:
        return "high"
    if slot.section in ("intro", "outro", "bridge", "break") or slot.energy < 0.4:
        return "low"
    return "mid"


def _song_sections(analysis: Mapping[str, Any] | None) -> list[_Slot]:
    if not analysis:
        return []
    sections: list[_Slot] = []
    for raw in analysis.get("sections") or []:
        start, end = _num(raw.get("start")), _num(raw.get("end"))
        if start is None or end is None or end - start <= 0.5:
            continue
        label = raw.get("label") if raw.get("label") in SECTION_LABELS else "verse"
        sections.append(_Slot(label, start, end, _num(raw.get("energy")) or 0.5))
    if not sections:
        duration = _num(analysis.get("duration"))
        if duration and duration > 0:
            sections.append(_Slot("verse", 0.0, duration, 0.5))
    return sorted(sections, key=lambda s: s.start or 0.0)


def _scene_slots(analysis: Mapping[str, Any] | None, n: int) -> list[_Slot]:
    """``n`` time slots following the song structure (proportional to section length)."""
    sections = _song_sections(analysis)
    if not sections:
        return [_Slot(_DEFAULT_STRUCTURE[round(i * (len(_DEFAULT_STRUCTURE) - 1) / max(1, n - 1))], None, None, 0.5)
                for i in range(n)]  # fmt: skip
    if n < len(sections):

        def importance(index: int) -> float:
            sec = sections[index]
            return (2.0 if sec.section == "chorus" else 0.0) + sec.energy + ((sec.end or 0) - (sec.start or 0)) / 30

        chosen = sorted(sorted(range(len(sections)), key=importance, reverse=True)[:n])
        sections = [sections[i] for i in chosen]
        alloc = [1] * n
    else:
        alloc = [1] * len(sections)
        for _ in range(n - len(sections)):
            best = max(
                range(len(sections)), key=lambda i: ((sections[i].end or 0) - (sections[i].start or 0)) / alloc[i]
            )
            alloc[best] += 1
    slots: list[_Slot] = []
    for sec, count in zip(sections, alloc, strict=True):
        assert sec.start is not None and sec.end is not None
        step = (sec.end - sec.start) / count
        for k in range(count):
            slots.append(
                _Slot(sec.section, round(sec.start + k * step, 3), round(sec.start + (k + 1) * step, 3), sec.energy)
            )
    return slots


def _lyric_refs(slots: list[_Slot], lyrics: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any] | None]:
    lines = [line for line in lyrics if str(line.get("text") or "").strip()]
    if not lines:
        return [None] * len(slots)
    timed = [line for line in lines if _num(line.get("start")) is not None]
    refs: list[Mapping[str, Any] | None] = []
    if timed and all(slot.start is not None for slot in slots):
        for slot in slots:
            assert slot.start is not None and slot.end is not None
            best, best_overlap = None, 0.0
            for line in timed:
                start = _num(line.get("start")) or 0.0
                end = _num(line.get("end")) or start + 4.0
                overlap = min(end, slot.end) - max(start, slot.start)
                if overlap > best_overlap + 1e-6:
                    best, best_overlap = line, overlap
            refs.append(best)
        return refs
    # Untimed lyrics: spread the lines over the vocal (non-instrumental) slots in order.
    vocal = [i for i, slot in enumerate(slots) if slot.section not in ("intro", "outro", "break")] or list(
        range(len(slots))
    )
    refs = [None] * len(slots)
    for k, index in enumerate(vocal):
        refs[index] = lines[min(len(lines) - 1, round(k * (len(lines) - 1) / max(1, len(vocal) - 1)))]
    return refs


# --- emotion / world estimate ----------------------------------------------------------------------
#
# 感情・世界観推定: audio mood (analysis.mood), a small lexicon over the lyrics and
# the user's mood/purpose text are merged into emotion scores, which map to the
# world presets that suit the song. Deterministic, no API call; Claude receives
# the estimate as context.

_EMOTIONS = ("melancholic", "nostalgic", "hopeful", "energetic", "romantic", "calm", "dark", "dreamy")
_THEMES = ("night", "nature")
_EMOTION_JA = {
    "melancholic": "切ない", "nostalgic": "懐かしい", "hopeful": "前向き", "energetic": "エネルギッシュ",
    "romantic": "ロマンチック", "calm": "穏やか", "dark": "ダーク", "dreamy": "幻想的", "night": "夜・都会",
    "nature": "自然",
}  # fmt: skip
_LEXICON_JA: dict[str, tuple[str, ...]] = {
    "melancholic": ("切な", "涙", "泣", "悲し", "寂し", "さみし", "孤独", "別れ", "さよなら", "サヨナラ", "消え",
                    "痛", "哀", "失", "傷", "戻れない", "届かない"),
    "nostalgic": ("懐かし", "思い出", "記憶", "あの頃", "あの日", "昔", "帰り道", "故郷", "ふるさと", "放課後",
                  "夏祭り", "セピア", "レトロ", "昭和"),
    "hopeful": ("希望", "未来", "明日", "信じ", "輝", "笑顔", "前を向", "羽ばた", "翼", "光", "夜明け", "始まり",
                "叶", "前向き"),
    "energetic": ("叫", "走", "燃", "炎", "熱", "衝動", "瞬間", "爆", "踊", "跳", "全力", "疾走", "激し", "力強"),
    "romantic": ("愛", "恋", "好き", "キス", "抱きしめ", "君と", "二人", "ふたり", "手を繋", "ときめ", "ロマンチック"),
    "calm": ("静か", "穏やか", "優し", "そっと", "ゆっくり", "眠", "安らぎ", "やすらぎ", "凪", "癒"),
    "dark": ("闇", "嘘", "罪", "壊", "狂", "呪", "毒", "絶望", "憎", "裏切", "ダーク"),
    "dreamy": ("夢", "幻", "浮", "宇宙", "天使", "魔法", "銀河", "ふわふわ", "不思議", "幻想"),
    "night": ("夜", "ネオン", "都会", "終電", "深夜", "交差点", "ビル", "東京", "渋谷", "路地", "街"),
    "nature": ("海", "空", "森", "風", "花", "桜", "山", "川", "雨", "雪", "波", "草原", "季節"),
}  # fmt: skip
_LEXICON_EN: dict[str, tuple[str, ...]] = {
    "melancholic": ("sad", "tears", "cry", "lonely", "goodbye", "lost", "broken", "melancholy", "melancholic",
                    "bittersweet", "pain", "emotional"),
    "nostalgic": ("memories", "memory", "remember", "yesterday", "nostalgic", "nostalgia", "retro", "vintage"),
    "hopeful": ("hope", "hopeful", "future", "tomorrow", "believe", "shine", "smile", "rise", "uplifting", "bright"),
    "energetic": ("run", "running", "fire", "burn", "dance", "dancing", "shout", "energetic", "upbeat", "wild",
                  "intense", "powerful"),
    "romantic": ("love", "kiss", "romantic", "together", "darling"),
    "calm": ("calm", "quiet", "gentle", "peaceful", "sleep", "chill", "mellow", "serene"),
    "dark": ("dark", "darkness", "lies", "sin", "poison", "evil", "moody", "gloomy"),
    "dreamy": ("dream", "dreams", "dreamy", "galaxy", "magic", "floating", "ethereal"),
    "night": ("night", "city", "neon", "midnight", "street", "streets", "urban"),
    "nature": ("sea", "ocean", "forest", "flower", "flowers", "rain", "snow", "wind", "river", "mountain"),
}  # fmt: skip
_PRESET_AFFINITY: dict[str, dict[str, float]] = {
    "melancholic": {"emotional": 1.0, "nostalgic": 0.6, "dark": 0.4, "cinematic": 0.3},
    "nostalgic": {"nostalgic": 1.0, "emotional": 0.5, "documentary": 0.3},
    "hopeful": {"cinematic": 0.8, "nature": 0.7, "emotional": 0.4, "documentary": 0.3},
    "energetic": {"urban_night": 0.9, "cinematic": 0.7, "documentary": 0.3},
    "romantic": {"emotional": 0.8, "dreamy": 0.7, "nostalgic": 0.3},
    "calm": {"nature": 0.8, "dreamy": 0.6, "emotional": 0.4},
    "dark": {"dark": 1.0, "urban_night": 0.5, "cinematic": 0.3},
    "dreamy": {"dreamy": 1.0, "nature": 0.4},
    "night": {"urban_night": 1.0, "dark": 0.4, "cinematic": 0.3},
    "nature": {"nature": 1.0, "dreamy": 0.3, "emotional": 0.2},
}
_DOCUMENTARY_HINT = re.compile(
    r"ライブ|ツアー|密着|ドキュメン|舞台裏|リハーサル|\blive\b|\btour\b|backstage|documentary", re.IGNORECASE
)


def _lexicon_hits(text: str) -> dict[str, int]:
    hits: dict[str, int] = {}
    lowered = text.lower()
    words = re.findall(r"[a-z]+", lowered)
    for key in (*_EMOTIONS, *_THEMES):
        count = sum(text.count(term) for term in _LEXICON_JA[key])
        count += sum(1 for word in words if word in _LEXICON_EN[key])
        if count:
            hits[key] = count
    return hits


def _audio_emotions(mood: Mapping[str, Any] | None) -> dict[str, float]:
    if not isinstance(mood, Mapping):
        return {}
    tags = {str(t) for t in mood.get("tags") or []}
    energy = _num(mood.get("energy"))
    scores: dict[str, float] = {}

    def add(key: str, value: float) -> None:
        scores[key] = scores.get(key, 0.0) + value

    if "energetic" in tags or (energy is not None and energy >= 0.65):
        add("energetic", 1.0)
    if tags & {"upbeat", "danceable"}:
        add("energetic", 0.5)
    if "calm" in tags or (energy is not None and energy <= 0.35):
        add("calm", 1.0)
    if "slow" in tags:
        add("calm", 0.5)
    if mood.get("mode_guess") == "minor" or "melancholic" in tags:
        add("melancholic", 0.8)
    if mood.get("mode_guess") == "major" or "uplifting" in tags:
        add("hopeful", 0.8)
    if "dark" in tags:
        add("dark", 0.5)
        add("night", 0.3)
    if "bright" in tags:
        add("hopeful", 0.4)
    if "dynamic" in tags:
        add("energetic", 0.2)
    return scores


def estimate_world(
    analysis: Mapping[str, Any] | None,
    lyrics: Sequence[Mapping[str, Any]],
    mood: str | None,
    purpose: str | None,
) -> dict[str, Any]:
    """Emotion/world estimate from audio mood, lyrics and the user's mood/purpose (感情・世界観推定).

    Returns ``{"emotions": [ids], "themes": [ids], "mood_en": str,
    "recommended_presets": [preset ids, best first], "sources": {"audio",
    "lyrics", "user": [ids]}, "rationale_ja": str}``. Emotion ids:
    ``melancholic nostalgic hopeful energetic romantic calm dark dreamy``;
    themes ``night nature``. Explicit user input weighs most, then lyrics,
    then audio.
    """
    audio = _audio_emotions((analysis or {}).get("mood"))
    lyric_text = " ".join(str(line.get("text") or "") for line in lyrics)
    lyric_hits = _lexicon_hits(lyric_text)
    top_hits = max(lyric_hits.values(), default=0)
    lyric_scores = {k: 1.5 * v / top_hits for k, v in lyric_hits.items()} if top_hits else {}
    user_text = " ".join(t for t in (mood, purpose) if t)
    user_scores = {k: min(2.0, 2.0 * v) for k, v in _lexicon_hits(user_text).items()}

    totals: dict[str, float] = {}
    for scores in (audio, lyric_scores, user_scores):
        for key, value in scores.items():
            totals[key] = totals.get(key, 0.0) + value

    def ranked(keys: Sequence[str], limit: int) -> list[str]:
        kept = [k for k in keys if totals.get(k, 0.0) >= 0.5]
        return sorted(kept, key=lambda k: (-totals[k], keys.index(k)))[:limit]

    emotions, themes = ranked(_EMOTIONS, 3), ranked(_THEMES, 2)
    preset_scores: dict[str, float] = {}
    for key, value in totals.items():
        for preset_id, affinity in _PRESET_AFFINITY.get(key, {}).items():
            preset_scores[preset_id] = preset_scores.get(preset_id, 0.0) + value * affinity
    if _DOCUMENTARY_HINT.search(user_text):
        preset_scores["documentary"] = preset_scores.get("documentary", 0.0) + 2.0
    order = list(WORLD_PRESETS)
    recommended = sorted((p for p, v in preset_scores.items() if v > 0 and p in WORLD_PRESETS and p != "custom"),
                         key=lambda p: (-preset_scores[p], order.index(p)))[:3] or ["cinematic"]  # fmt: skip

    def labels(scores: Mapping[str, float]) -> list[str]:
        return [k for k in (*_EMOTIONS, *_THEMES) if scores.get(k, 0.0) >= 0.5]

    sources = {"audio": labels(audio), "lyrics": labels(lyric_scores), "user": labels(user_scores)}
    parts = []
    for name, keys in (("音源", sources["audio"]), ("歌詞", sources["lyrics"]), ("入力", sources["user"])):
        if keys:
            parts.append(f"{name}: {'・'.join(_EMOTION_JA[k] for k in keys)}")
    preset_names = "・".join(WORLD_PRESETS[p]["name_ja"] for p in recommended)
    basis = "／".join(parts) if parts else "手がかりが少ないため汎用的な推定"
    return {
        "emotions": emotions,
        "themes": themes,
        "mood_en": ", ".join(emotions),
        "recommended_presets": recommended,
        "sources": sources,
        "rationale_ja": f"{basis} → おすすめの世界観: {preset_names}",
    }


# --- template generator ---------------------------------------------------------------------------


def _pick(
    candidates: Sequence[str],
    *,
    rng: random.Random,
    uses: dict[str, int],
    previous: str | None,
    bonus: Mapping[str, float] | None = None,
) -> str:
    def score(item: str) -> float:
        return uses.get(item, 0) * 10 + (100 if item == previous else 0) - (bonus or {}).get(item, 0.0) + rng.random()

    choice = min(candidates, key=score)
    uses[choice] = uses.get(choice, 0) + 1
    return choice


_ENERGY_OK = {"high": ("high", "mid"), "mid": ("mid", "low", "high"), "low": ("low", "mid")}


def _template_world(
    preset: WorldPreset,
    *,
    rng: random.Random,
    custom_world: str | None,
    custom_world_en: str,
    mood: str | None,
    estimated_mood_ja: str = "",
) -> dict[str, Any]:
    protagonist = rng.choice(preset["protagonists"])
    location = rng.choice(preset["locations"])
    location_en, location_ja = location["en"], location["ja"]
    look_en = preset["look_en"]
    if preset["id"] == "custom" and custom_world_en:
        location_en = "the setting"
        location_ja = (custom_world or "").strip()[:40]
        look_en = f"{custom_world_en}, {look_en}"
    mood_ja = f"ムードは「{mood.strip()[:40]}」。" if mood and mood.strip() else ""
    if not mood_ja and estimated_mood_ja:
        mood_ja = f"ムードは推定で「{estimated_mood_ja}」。"
    return {
        "summary_ja": (
            f"{preset['name_ja']}の世界観。主人公は{protagonist['ja']}。舞台は{location_ja}。"
            f"色調は{preset['palette_ja']}。{mood_ja}全シーンで同じ主人公・衣装・色調を保ちます。"
        ),
        "look_en": look_en,
        "protagonist_en": protagonist["en"],
        "location_en": location_en,
        "palette": list(preset["palette"]),
        "consistency_tokens_en": f"{preset['palette_en']} color palette, {preset['film_en']}",
        "protagonist_ja": protagonist["ja"],
        "location_ja": location_ja,
    }


def _template_scenes(
    preset: WorldPreset,
    world: Mapping[str, Any],
    *,
    rng: random.Random,
    slots: list[_Slot],
    refs: list[Mapping[str, Any] | None],
    custom_world_en: str,
    mood_en: str,
    forbidden: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    scenes: list[dict[str, Any]] = []
    warnings: list[str] = []
    scene_uses: dict[str, int] = {}
    move_uses: dict[str, int] = {}
    angle_uses: dict[str, int] = {}
    prev_scene = prev_move = prev_angle = None
    no_subject_budget = max(1, math.ceil(len(slots) * 0.35))
    no_subject_used = 0

    for index, (slot, ref) in enumerate(zip(slots, refs, strict=True)):
        energy = _energy_class(slot)
        allowed = _ENERGY_OK[energy]
        scene_candidates = [s for s in preset["scene_types"] if SCENE_TYPES[s]["energy"] in allowed]
        if no_subject_used >= no_subject_budget:
            scene_candidates = [s for s in scene_candidates if SCENE_TYPES[s]["has_subject"]] or scene_candidates
        scene_bonus = {s: 3.0 for s in scene_candidates if SCENE_TYPES[s]["energy"] == energy}
        if index == 0 and "establishing_wide" in scene_candidates:
            scene_bonus["establishing_wide"] = 50.0
        scene_type = _pick(scene_candidates, rng=rng, uses=scene_uses, previous=prev_scene, bonus=scene_bonus)
        scene = SCENE_TYPES[scene_type]
        no_subject_used += 0 if scene["has_subject"] else 1

        move_candidates = [m for m in preset["camera_moves"] if CAMERA_MOVES[m].get("energy", "mid") in allowed]
        if scene_type in _INTIMATE_SCENES:
            move_candidates = [m for m in move_candidates if m not in _BIG_MOVES] or move_candidates
        if scene_type in _CALM_SCENES:
            move_candidates = [m for m in move_candidates if m not in _AGGRESSIVE_MOVES] or move_candidates
        move_bonus = {m: 3.0 for m in move_candidates if CAMERA_MOVES[m].get("energy") == energy}
        move = _pick(
            move_candidates or preset["camera_moves"], rng=rng, uses=move_uses, previous=prev_move, bonus=move_bonus
        )

        angle_candidates = [a for a in preset["camera_angles"] if a != "overhead" or scene_type in _OVERHEAD_OK]
        angle = _pick(angle_candidates, rng=rng, uses=angle_uses, previous=prev_angle)
        prev_scene, prev_move, prev_angle = scene_type, move, angle

        lyric_text = str(ref.get("text") or "").strip() if ref else ""
        motifs = glossary_keywords(lyric_text, limit=4, include_ascii=False) if lyric_text else []
        action = scene["prompt_en"].format(subject=world["protagonist_en"], place=world["location_en"])
        atmosphere = preset["atmosphere_en"] + (f", {mood_en} mood" if mood_en else "")
        segments: list[tuple[int, str]] = [
            (1, _sentence(f"Setting: {custom_world_en}") if custom_world_en else ""),
            (0, _sentence(f"Cinematic live-action shot: {action}")),
            (1, _sentence(f"{CAMERA_ANGLES[angle]['prompt_en']}, {CAMERA_MOVES[move]['prompt_en']}")),
            (2, _sentence(preset["lighting_en"])),
            (3, _sentence(atmosphere)),
            (5, _sentence(f"Evoking {', '.join(motifs)}") if motifs else ""),
            (4, _sentence(f"Shot on {preset['lens_en']}")),
            (0, _sentence(REALISM_EN)),
            (1, _verbatim(world["consistency_tokens_en"])),
        ]
        body = _compose(segments, prompt_body_budget(STORYBOARD_PROMPT_CHARS))
        prompt, prompt_warnings = sanitize_prompt(body, forbidden_names=forbidden, max_chars=STORYBOARD_PROMPT_CHARS)
        warnings.extend(prompt_warnings)

        section_ja = SECTION_LABELS_JA.get(slot.section, slot.section)
        description = scene["desc_ja"].format(subject="主人公", place=world["location_ja"] or "舞台")
        lyric_note = ""
        if lyric_text:
            excerpt, _ = strip_forbidden_names(lyric_text, forbidden)
            lyric_note = f"／歌詞「{excerpt[:24]}{'…' if len(excerpt) > 24 else ''}」"
        scenes.append(
            {
                "id": f"s{index + 1:02d}",
                "section": slot.section,
                "lyric_ref": str(ref.get("id")) if ref and ref.get("id") is not None else None,
                "scene_type": scene_type,
                "camera_angle": angle,
                "camera_move": move,
                "description_ja": (
                    f"【{section_ja}】{description}。{CAMERA_ANGLES[angle]['name_ja']}・"
                    f"{CAMERA_MOVES[move]['name_ja']}{lyric_note}"
                ),
                "prompt_en": prompt,
                "negative_prompt_en": _merge_negative(BASE_NEGATIVE_EN, preset["negative_en"]),
                "duration_hint": {"high": 4, "mid": 5, "low": 6}[energy],
                "start": slot.start,
                "end": slot.end,
            }
        )
    return scenes, warnings


# --- Claude ---------------------------------------------------------------------------------------


def _vocab_lines(vocab: Mapping[str, Mapping[str, Any]]) -> str:
    return "\n".join(f"- {key}: {value['prompt_en'].replace('{subject}', 'PROTAGONIST').replace('{place}', 'LOCATION')}"
                     for key, value in vocab.items())  # fmt: skip


SYSTEM_PROMPT = f"""You are a music-video director and an expert prompt writer for text-to-video models \
(Kling, Runway, Veo). You plan storyboards for short vertical 9:16 music videos for Instagram Stories. \
Each scene becomes one independently generated 4-10 second photorealistic clip, so every prompt must \
stand on its own while all clips of a song look like one coherent film.

World bible (shared by every scene):
- protagonist_en: ONE fictional adult protagonist (20s-30s) with a concrete, generic description: \
age range, hair, wardrobe with colours and materials. Never a real or famous person, never anyone's \
likeness, never a minor.
- location_en: one recurring location family that fits the song.
- palette: exactly three hex colours (#RRGGBB). look_en: lighting, lens (e.g. "35mm anamorphic"), \
film stock look and atmosphere.
- consistency_tokens_en: at most 200 characters of shared style descriptors (wardrobe, palette, film look) \
to repeat in every prompt.
- summary_ja: 2-3 Japanese sentences describing the world for the artist.

Scenes:
- Exactly the requested number, in song order, distributed over the song sections; lyric_ref is the id of \
the lyric line the scene illustrates (or null for instrumental parts). Interpret lyrics visually and \
metaphorically; never show the lyrics as on-screen text.
- Vary scene types, camera angles and camera moves between neighbouring scenes. Choruses get more \
movement and energy; intros, bridges and outros are slower, wider and more intimate. Mix protagonist \
scenes with atmospheric shots without people.
- prompt_en (max 650 characters, one continuous shot, no cuts) in this order: shot type and subject with a \
simple physically plausible action -> environment -> lighting -> camera angle and movement -> lens -> \
colour palette and film look -> "vertical 9:16 composition" -> realism cues ("photorealistic live-action \
footage, natural skin texture, realistic hands, subtle natural motion") -> the consistency tokens. Repeat \
the full protagonist description in every scene where the protagonist appears.
- Never mention real people, celebrities, the artist or any name listed under forbidden_likeness, brands, \
logos, readable signs or any text in frame.
- song.audio_mood (from audio analysis) and song.world_estimate (emotions merged from audio, lyrics and the \
user's mood) describe the emotional tone; combine them with the lyrics and the user's mood.

detected_real_people: every real person (the artist, celebrities, public figures, private individuals) named \
or alluded to anywhere in the input, in every spelling you know (original script, romanization, katakana, \
English name), including the romanized and kana spellings of the names under forbidden_likeness. Empty list \
if none. These names are removed from all prompts.
- negative_prompt_en: comma-separated, max 300 characters (artifacts to avoid: deformed hands, extra \
fingers, warped face, morphing, flicker, text, watermark, cartoon, CGI, oversaturated, plus scene-specific \
items).
- description_ja: one or two Japanese sentences for the artist describing the shot.
- duration_hint: seconds of action (4-8).

Scene type ids:
{_vocab_lines(SCENE_TYPES)}

Camera angle ids:
{_vocab_lines(CAMERA_ANGLES)}

Camera move ids:
{_vocab_lines(CAMERA_MOVES)}

Answer only with JSON matching the provided schema."""


def _nullable_string() -> dict[str, Any]:
    return {"anyOf": [{"type": "string"}, {"type": "null"}]}


STORYBOARD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "world_bible": {
            "type": "object",
            "properties": {
                "summary_ja": {"type": "string"},
                "look_en": {"type": "string"},
                "protagonist_en": {"type": "string"},
                "location_en": {"type": "string"},
                "palette": {"type": "array", "items": {"type": "string"}},
                "consistency_tokens_en": {"type": "string"},
            },
            "required": ["summary_ja", "look_en", "protagonist_en", "location_en", "palette", "consistency_tokens_en"],
            "additionalProperties": False,
        },
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section": {"type": "string", "enum": list(SECTION_LABELS)},
                    "lyric_ref": _nullable_string(),
                    "scene_type": {"type": "string", "enum": list(SCENE_TYPES)},
                    "camera_angle": {"type": "string", "enum": list(CAMERA_ANGLES)},
                    "camera_move": {"type": "string", "enum": list(CAMERA_MOVES)},
                    "description_ja": {"type": "string"},
                    "prompt_en": {"type": "string"},
                    "negative_prompt_en": {"type": "string"},
                    "duration_hint": {"type": "number"},
                },
                "required": [
                    "section",
                    "lyric_ref",
                    "scene_type",
                    "camera_angle",
                    "camera_move",
                    "description_ja",
                    "prompt_en",
                    "negative_prompt_en",
                    "duration_hint",
                ],
                "additionalProperties": False,
            },
        },
        "detected_real_people": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["world_bible", "scenes", "detected_real_people"],
    "additionalProperties": False,
}


class _ClaudeFailure(Exception):
    """Claude produced no usable storyboard; ``warning`` is shown to the user.

    ``model``/``usage``/``cost_usd`` describe a call that was answered (and billed) but unusable.
    """

    def __init__(
        self,
        warning: str,
        *,
        route: str | None = None,
        model: str | None = None,
        usage: dict[str, int] | None = None,
        cost_usd: float | None = None,
    ):
        super().__init__(warning)
        self.warning = warning
        self.route = route
        self.model = model
        self.usage = usage
        self.cost_usd = cost_usd


def _reply_cost(reply: LlmReply) -> float:
    """OpenRouter's reported ``usage.cost`` when present, else the tokens at Anthropic list prices.

    BYOK (``is_byok``: the vendor bills the user's own key) [B]: ``usage.cost`` may be OpenRouter's fee
    only, so the reported upstream cost is added, or — when none is reported — at least the token
    price is booked (the spend is never under-counted).
    """
    if reply.cost_usd is None:
        return claude_cost_usd(reply.model, reply.usage)
    cost = float(reply.cost_usd)
    if reply.is_byok:
        if reply.upstream_cost_usd is not None:
            cost += float(reply.upstream_cost_usd)
        else:
            cost = max(cost, claude_cost_usd(reply.model, reply.usage))
    return round(cost, 6)


def _call_failure_warning(exc: LlmCallError) -> str:
    """Japanese fallback warning for a request that got no answer (identical texts on the Anthropic route)."""
    api = api_label(exc.route)
    tail = "、テンプレートで絵コンテを作成しました"
    messages = {
        "bad_request": f"{api} がリクエストを受け付けなかったため{tail}",
        "rate_limited": f"{api} の利用上限に達したため{tail}",
        "connection": f"{api} に接続できなかったため{tail}",
        "credits": f"OpenRouter のクレジットが不足しているため{tail}",
        "moderation": f"OpenRouter のモデレーション（ガードレール）でリクエストが拒否されたため{tail}",
        "no_provider": (
            "データを収集・保持しない提供元（data_collection: deny・ZDR）で Claude を使える提供元が"
            f"見つからなかったため{tail}"
        ),
        "auth": f"OpenRouter の API キーが無効なため{tail}",
        "bad_response": f"{api} の応答を読み取れなかったため{tail}",
    }
    return messages.get(exc.kind) or f"{api} でエラーが発生したため（HTTP {exc.status}）{tail}"


def _call_claude(
    client: Any, *, user_message: str, end_user: str | None = None
) -> tuple[dict[str, Any], LlmReply]:
    """One storyboard request on the client's route → ``(storyboard JSON, reply)``; else :class:`_ClaudeFailure`."""
    try:
        reply = complete(
            client, system=SYSTEM_PROMPT, user=user_message, max_tokens=16000, json_schema=STORYBOARD_SCHEMA,
            schema_name="storyboard", cache_system=True, end_user=end_user, purpose="storyboard",
        )  # fmt: skip
    except LlmCallError as exc:
        raise _ClaudeFailure(_call_failure_warning(exc), route=exc.route) from exc

    def billed(warning: str) -> _ClaudeFailure:  # an answered (billed) call whose answer is unusable
        return _ClaudeFailure(warning, route=reply.route, model=reply.model, usage=reply.usage,
                              cost_usd=_reply_cost(reply))  # fmt: skip

    if reply.finish == "refusal":
        who = "Claude（OpenRouter 経由）" if reply.route == "openrouter" else "Claude"
        raise billed(f"{who} がこの内容の絵コンテ作成を見送ったため、テンプレートで作成しました")
    if reply.finish == "length":
        raise billed("Claude の応答が長さの上限で途中までになったため、テンプレートで絵コンテを作成しました")
    if reply.finish == "error":
        raise billed("OpenRouter の提供元でエラーが発生したため、テンプレートで絵コンテを作成しました")
    try:
        data = json.loads(reply.text or "")
        if not isinstance(data, dict) or not isinstance(data.get("world_bible"), dict):
            raise ValueError("world_bible missing")
        if not isinstance(data.get("scenes"), list) or not data["scenes"]:
            raise ValueError("scenes missing")
    except (TypeError, ValueError) as exc:
        logger.warning("Claude storyboard output was not valid storyboard JSON (route=%s)", reply.route)
        raise billed("Claude の応答を解析できなかったため、テンプレートで絵コンテを作成しました") from exc
    return data, reply


def _user_message(
    *,
    title: str,
    mood: str | None,
    purpose: str | None,
    lyrics: Sequence[Mapping[str, Any]],
    analysis: Mapping[str, Any] | None,
    preset: WorldPreset,
    custom_world: str | None,
    n_scenes: int,
    forbidden: list[str],
    estimate: Mapping[str, Any] | None = None,
) -> str:
    """User message for Claude. ``forbidden`` (artist name + spellings + people named
    in the user's text) is removed from every field and sent only as forbidden_likeness."""

    def clean(value: str | None) -> str | None:
        return strip_forbidden_names(value, forbidden)[0] or None if value else None

    sections = [
        {"label": s.section, "start": s.start, "end": s.end, "energy": round(s.energy, 2)}
        for s in _song_sections(analysis)
    ]
    lyric_items = [
        {
            "id": str(line.get("id")),
            "text": clean(str(line.get("text") or "")[:200]) or "",
            "start": _num(line.get("start")),
            "end": _num(line.get("end")),
        }
        for line in lyrics[:150]
        if str(line.get("text") or "").strip()
    ]
    payload = {
        "task": f"Create a storyboard with exactly {n_scenes} scenes.",
        "song": {
            "title": clean(title),
            "mood": clean(mood),
            "purpose": clean(purpose),
            "duration_sec": _num((analysis or {}).get("duration")),
            "bpm": _num((analysis or {}).get("bpm")),
            "sections": sections,
            "audio_mood": (analysis or {}).get("mood") if isinstance((analysis or {}).get("mood"), Mapping) else None,
            "world_estimate": (
                {k: estimate[k] for k in ("emotions", "themes", "recommended_presets") if k in estimate}
                if estimate
                else None
            ),
        },
        "lyrics": lyric_items,
        "world_preset": {
            "id": preset["id"],
            "name": preset["name_en"],
            "look_en": preset["look_en"],
            "palette": preset["palette"],
            "palette_en": preset["palette_en"],
            "suggested_scene_types": preset["scene_types"],
            "suggested_camera_angles": preset["camera_angles"],
            "suggested_camera_moves": preset["camera_moves"],
            "custom_world_ja": clean(custom_world) if preset["id"] == "custom" else None,
            "extra_notes_ja": clean(custom_world) if preset["id"] != "custom" else None,
        },
        "forbidden_likeness": {
            "names": list(forbidden),
            "rule": "Never depict, name or resemble these people (the performing artist) or any real person.",
        },
    }
    return json.dumps(payload, ensure_ascii=False)


_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _normalize_claude(
    data: Mapping[str, Any],
    *,
    preset: WorldPreset,
    lyrics: Sequence[Mapping[str, Any]],
    n_scenes: int,
    forbidden: list[str],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    raw_world = data["world_bible"]
    detected = [
        str(name).strip() for name in data.get("detected_real_people") or []
        if isinstance(name, str) and 2 <= len(name.strip()) <= 60
    ][:30]  # fmt: skip
    extra = [name for name in dict.fromkeys(detected) if name not in forbidden]
    if extra:
        forbidden = [*forbidden, *extra]
        warnings.append(f"Claude が実在の人物名を {len(extra)} 件検出したため、プロンプトに使わないよう除外しました")

    def text(key: str, fallback: str = "") -> str:
        value = raw_world.get(key)
        return str(value).strip() if isinstance(value, str) and value.strip() else fallback

    def clean_en(value: str) -> str:
        """World-bible text without forbidden names or real-person references (kept verbatim in prompts)."""
        return strip_person_references(strip_forbidden_names(value, forbidden)[0]).text

    protagonist = clean_en(text("protagonist_en", preset["protagonists"][0]["en"]))
    if "fictional" not in protagonist.lower():
        protagonist = (
            f"a fictional {protagonist[:1].lower()}{protagonist[1:]}"
            if protagonist
            else preset["protagonists"][0]["en"]
        )
    palette = [str(c).upper() for c in raw_world.get("palette") or [] if isinstance(c, str) and _HEX.match(c.strip())]
    world = {
        "summary_ja": strip_forbidden_names(text("summary_ja", preset["description_ja"]), forbidden)[0],
        "look_en": clean_en(text("look_en", preset["look_en"])),
        "protagonist_en": protagonist,
        "location_en": clean_en(text("location_en", preset["locations"][0]["en"])),
        "palette": palette[:3] if len(palette) >= 3 else list(preset["palette"]),
        "consistency_tokens_en": clean_en(
            text("consistency_tokens_en", f"{preset['palette_en']} color palette, {preset['film_en']}")
        )[:240],
    }

    line_by_id = {str(line.get("id")): line for line in lyrics if line.get("id") is not None}
    scenes: list[dict[str, Any]] = []
    raw_scenes = [s for s in data["scenes"] if isinstance(s, dict)][:n_scenes]
    if len(raw_scenes) < n_scenes:
        warnings.append(f"Claude の絵コンテは {len(raw_scenes)} シーンでした（指定 {n_scenes} シーン）")
    for index, raw in enumerate(raw_scenes):
        scene_type = raw.get("scene_type") if raw.get("scene_type") in SCENE_TYPES else preset["scene_types"][0]
        angle = raw.get("camera_angle") if raw.get("camera_angle") in CAMERA_ANGLES else preset["camera_angles"][0]
        move = raw.get("camera_move") if raw.get("camera_move") in CAMERA_MOVES else preset["camera_moves"][0]
        section = raw.get("section") if raw.get("section") in SECTION_LABELS else "verse"
        lyric_ref = (
            str(raw["lyric_ref"]) if raw.get("lyric_ref") is not None and str(raw["lyric_ref"]) in line_by_id else None
        )
        prompt_body = str(raw.get("prompt_en") or "").strip()
        if not prompt_body:
            prompt_body = SCENE_TYPES[scene_type]["prompt_en"].format(
                subject=world["protagonist_en"], place=world["location_en"]
            )
        segments: list[tuple[int, str]] = [(0, _sentence(prompt_body))]
        if "9:16" not in prompt_body:
            segments.append((0, _sentence(REALISM_EN)))
        if world["consistency_tokens_en"].lower() not in prompt_body.lower():
            segments.append((2, _verbatim(world["consistency_tokens_en"])))
        prompt, prompt_warnings = sanitize_prompt(
            _compose(segments, prompt_body_budget(STORYBOARD_PROMPT_CHARS)), forbidden_names=forbidden,
            max_chars=STORYBOARD_PROMPT_CHARS,
        )  # fmt: skip
        warnings.extend(prompt_warnings)
        line = line_by_id.get(lyric_ref) if lyric_ref else None
        start = _num(line.get("start")) if line else None
        end = _num(line.get("end")) if line else None
        duration_hint = _num(raw.get("duration_hint")) or 5.0
        scenes.append(
            {
                "id": f"s{index + 1:02d}",
                "section": section,
                "lyric_ref": lyric_ref,
                "scene_type": scene_type,
                "camera_angle": angle,
                "camera_move": move,
                "description_ja": strip_forbidden_names(str(raw.get("description_ja") or ""), forbidden)[0]
                or SCENE_TYPES[scene_type]["desc_ja"].format(subject="主人公", place="舞台"),
                "prompt_en": prompt,
                "negative_prompt_en": _merge_negative(
                    str(raw.get("negative_prompt_en") or ""), BASE_NEGATIVE_EN, preset["negative_en"]
                ),
                "duration_hint": max(2.0, min(10.0, duration_hint)),
                "start": start,
                "end": end,
            }
        )
    return world, scenes, warnings


# --- public API --------------------------------------------------------------------------------------


def _forbidden_names(artist_name: str, aliases: Sequence[str] | None) -> list[str]:
    """Artist name and aliases with their kana romanisations (あいみょん → aimyon)."""
    names: list[str] = []
    for name in (artist_name, *(aliases or ())):
        for variant in artist_name_variants(str(name or "")):
            if variant not in names:
                names.append(variant)
    return names


def generate_storyboard(
    *,
    title: str,
    artist_name: str,
    mood: str | None,
    purpose: str | None,
    lyrics: Sequence[Mapping[str, Any]],
    analysis: Mapping[str, Any] | None,
    preset_id: str,
    custom_world: str | None,
    n_scenes: int | None,
    seed: int | None = None,
    use_claude: bool = True,
    client: Any | None = None,
    artist_aliases: Sequence[str] | None = None,
    end_user: str | None = None,
) -> dict[str, Any]:
    """Build a Storyboard (ARCHITECTURE.md §6.3).

    Claude is used when ``use_claude`` is true and a Claude client is
    available: ``client`` (an ``anthropic.Anthropic`` or an OpenRouter chat
    client — used by tests) or the configured route (``ANTHROPIC_API_KEY``,
    else ``OPENROUTER_API_KEY``; :func:`app.services.llm.make_llm_client`).
    ``end_user`` (the owner id) becomes OpenRouter's pseudonymous ``user``
    (an HMAC; the raw id is never sent). The result's ``route`` is
    ``"anthropic"`` / ``"openrouter"`` when Claude was called (also when its
    answer was unusable), else ``None``. The template path is deterministic for a given ``seed`` (``None`` derives
    a stable seed from title/preset/custom world). The artist name (plus
    ``artist_aliases``, e.g. romanised or English names, and kana
    romanisations) is only ever used as a forbidden-likeness constraint, as
    are people named in the custom world / mood / purpose. The result also
    carries ``world_estimate`` (:func:`estimate_world`).
    """
    preset = get_preset(preset_id)
    n = max(1, min(MAX_SCENES, int(n_scenes or DEFAULT_SCENES)))
    forbidden = _forbidden_names(artist_name, artist_aliases)
    lyrics = list(lyrics or [])
    warnings: list[str] = []
    claude_route: str | None = None
    claude_model: str | None = None
    claude_usage: dict[str, int] | None = None
    claude_cost = 0.0

    # People named in the user's free text are forbidden everywhere (prompts, Claude, lyric excerpts).
    custom_world_clean, world_names, world_warnings = screen_free_text(
        custom_world, forbidden, where="カスタム世界観から"
    )
    mood_clean, mood_names, mood_warnings = screen_free_text(mood, forbidden, where="雰囲気から")
    _, purpose_names, _ = screen_free_text(purpose, forbidden)
    for name in (*world_names, *mood_names, *purpose_names):
        if name not in forbidden:
            forbidden.append(name)
    screen_warnings = [*world_warnings, *mood_warnings]
    estimate = estimate_world(analysis, lyrics, mood_clean, purpose)

    if use_claude:
        claude = client if client is not None else make_claude_client()
        if claude is not None:
            message = _user_message(
                title=title, mood=mood, purpose=purpose, lyrics=lyrics, analysis=analysis, preset=preset,
                custom_world=custom_world, n_scenes=n, forbidden=forbidden, estimate=estimate,
            )  # fmt: skip
            try:
                data, reply = _call_claude(claude, user_message=message, end_user=end_user)
                world, scenes, normalize_warnings = _normalize_claude(
                    data, preset=preset, lyrics=lyrics, n_scenes=n, forbidden=forbidden
                )
                return {
                    "source": "claude",
                    "route": reply.route,
                    "model": reply.model,
                    "usage": reply.usage,
                    "cost_usd": _reply_cost(reply),
                    "world_bible": world,
                    "scenes": scenes,
                    "warnings": list(dict.fromkeys([*screen_warnings, *normalize_warnings])),
                    "preset": preset["id"],
                    "seed": seed,
                    "world_estimate": estimate,
                }
            except _ClaudeFailure as failure:
                warnings.append(failure.warning)
                claude_route = failure.route or route_of(claude)
                claude_model, claude_usage = failure.model, failure.usage
                claude_cost = float(failure.cost_usd or 0.0)

    effective_seed = seed if seed is not None else _stable_int(title, preset["id"], custom_world or "")
    rng = random.Random(_stable_int("storyboard", effective_seed, preset["id"]))
    warnings.extend(screen_warnings)

    custom_world_en = ""
    if preset["id"] == "custom":
        if custom_world and custom_world.strip():
            custom_world_en, source = translate_to_english(custom_world_clean, use_claude=False)
            # Names were removed before translation (a glossary may split them into separate words).
            custom_world_en = screen_free_text(custom_world_en, forbidden)[0][:300]
            if not custom_world_en:
                warnings.append(
                    "カスタム世界観を英語に変換できなかったため、汎用的な設定で作成しました。"
                    "Claude API を設定すると自由記述をそのまま反映できます"
                )
            elif source == "glossary":
                warnings.append("カスタム世界観は簡易辞書で英訳しました（Claude API を設定すると精度が上がります）")
        else:
            warnings.append("カスタム世界観が未入力のため、汎用的な設定で作成しました")
    # Mood: glossary translations and allow-listed English mood words only (never raw
    # Latin-script text, which could name a person); without a usable mood, the estimate.
    mood_en = ", ".join(mood_keywords(mood_clean, limit=4)) if mood_clean else ""
    estimated_mood_ja = ""
    if not mood_en and estimate["emotions"]:
        mood_en = estimate["mood_en"]
        estimated_mood_ja = "・".join(_EMOTION_JA[e] for e in estimate["emotions"])

    if not _song_sections(analysis):
        warnings.append("楽曲解析の結果がないため、一般的な曲構成を想定してシーンを割り当てました")
    slots = _scene_slots(analysis, n)
    refs = _lyric_refs(slots, lyrics)
    world = _template_world(
        preset, rng=rng, custom_world=custom_world_clean, custom_world_en=custom_world_en, mood=mood_clean,
        estimated_mood_ja=estimated_mood_ja,
    )  # fmt: skip
    scenes, scene_warnings = _template_scenes(
        preset, world, rng=rng, slots=slots, refs=refs, custom_world_en=custom_world_en, mood_en=mood_en,
        forbidden=forbidden,
    )  # fmt: skip
    warnings.extend(scene_warnings)
    return {
        "source": "template",
        # route/model/usage/cost are those of a failed Claude attempt (tokens are still billed), else null/0.
        "route": claude_route,
        "model": claude_model,
        "usage": claude_usage,
        "cost_usd": claude_cost,
        "world_bible": world,
        "scenes": scenes,
        "warnings": list(dict.fromkeys(warnings)),
        "preset": preset["id"],
        "seed": effective_seed,
        "world_estimate": estimate,
    }
