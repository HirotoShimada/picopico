"""World presets and creative vocabulary (ARCHITECTURE.md §6.3).

Everything here is static data shared by the storyboard generator
(:mod:`app.services.storyboard`), the batch planner
(:mod:`app.services.variation`), the renderer (``COLOR_GRADES``) and the
capabilities endpoint (``preset_infos`` / ``color_grade_infos`` /
``lyric_style_infos``).

Prompt vocabulary is English (video models are trained on English captions);
names and descriptions shown in the UI are Japanese.

Scene ``prompt_en`` strings are templates with two placeholders:
``{subject}`` (the protagonist's identity plus the garments visible in the
scene's ``framing``) and ``{place}`` (the world-bible location). Scenes with
``has_subject=False`` are atmosphere B-roll without people (``framing`` is
``no_people``), which keeps a batch visually varied and is the safest material
for likeness rules.

Prompt wording is deliberately plain and candid (realism direction, 2026-10-13
"映像がAI感強くない？"): ordinary people, everyday places, the light a place
really has, plain physical actions and plain camera language. No lens specs,
film-look/grain/halation words or palette names reach a prompt (grain and
grading are added in post by the renderer); ``palette`` hex values are for the
app's interface only.

Wardrobe is structured (:class:`Garment`: ``region`` + ``item_en``) so a
prompt names only what the framing shows (:data:`FRAMING_REGIONS`): close-ups
get the outerwear/top (and head accessories), medium shots add the bottoms and
carried items, only full-body/wide shots mention shoes.

Colour grades are *colour-only* point filters that work in RGB
(``colorchannelmixer`` for saturation, ``colorbalance`` for mid-tone and
highlight tints, ``colortemperature``, ``vibrance`` and monotone ``curves``):
vignette and film grain are added per grade by the renderer, and runs of point
filters are baked into a single 3D LUT there, so keeping grades
position-independent keeps rendering fast. Grades keep shadow detail: tone
curves use ``interp=pchip`` (no spline undershoot below black) with a toe
slope of at least ~0.6, shadows are tinted by lifting channels instead of
pulling them under black, and ``eq``/``hue`` (an 8-bit YUV round trip, pivoting
contrast around mid-grey) are not used — on a grey ramp, adjacent input codes
0–40 stay distinguishable (regression-tested).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Literal, NotRequired, TypedDict

__all__ = [
    "CAMERA_ANGLES",
    "CAMERA_MOVES",
    "COLOR_GRADES",
    "DEFAULT_PRESET_ID",
    "FRAMINGS",
    "FRAMINGS_WITH_FACE",
    "FRAMING_REGIONS",
    "GARMENT_REGIONS",
    "LYRIC_STYLES",
    "PRESET_IDS",
    "SCENE_TYPES",
    "SECTION_LABELS_JA",
    "WORLD_PRESETS",
    "CameraEntry",
    "ColorGrade",
    "Framing",
    "Garment",
    "GarmentRegion",
    "LocalizedText",
    "LyricStyle",
    "Protagonist",
    "SceneType",
    "WorldPreset",
    "color_grade_infos",
    "garment_region",
    "get_preset",
    "lyric_style_infos",
    "preset_infos",
    "visible_garments",
    "wardrobe_phrase",
]

Energy = Literal["low", "mid", "high"]
GarmentRegion = Literal["outer", "top", "bottom", "feet", "head", "carry"]
Framing = Literal["no_people", "detail", "close_up", "medium", "full_body"]

# outer: coats, jackets · top: shirts, sweaters, dresses · bottom: trousers, skirts, shorts · feet: shoes, socks ·
# head: hats, glasses, earrings, necklaces, scarves (anything at head/neck height) · carry: bags, watches, belts.
GARMENT_REGIONS: tuple[GarmentRegion, ...] = ("outer", "top", "bottom", "feet", "head", "carry")
# no_people: nobody in frame · detail: part of the protagonist without the face (hands, back, feet) ·
# close_up: face and shoulders · medium: waist up or seated · full_body: the whole figure, wide shots.
FRAMINGS: tuple[Framing, ...] = ("no_people", "detail", "close_up", "medium", "full_body")
# Garments a framing shows. no_people/detail get none from the app (a detail prompt names its one visible garment).
FRAMING_REGIONS: dict[str, frozenset[str]] = {
    "no_people": frozenset(),
    "detail": frozenset(),
    "close_up": frozenset({"outer", "top", "head"}),
    "medium": frozenset({"outer", "top", "head", "bottom", "carry"}),
    "full_body": frozenset(GARMENT_REGIONS),
}
# Framings whose prompt gets the protagonist's identity (face/hair) from the world bible.
FRAMINGS_WITH_FACE: frozenset[str] = frozenset({"close_up", "medium", "full_body"})


class LocalizedText(TypedDict):
    en: str
    ja: str


class Garment(TypedDict):
    region: GarmentRegion
    item_en: str  # colour, material and garment in plain words ("faded navy cotton work jacket")


class Protagonist(TypedDict):
    en: str  # identity + every garment (display; legacy ``protagonist_en``)
    ja: str
    identity_en: str  # age, build, face and hair — no clothing
    wardrobe: list[Garment]


class SceneType(TypedDict):
    name_ja: str
    prompt_en: str  # template with {subject} / {place}
    desc_ja: str  # template with {subject} / {place} (Japanese)
    has_subject: bool
    energy: Energy
    framing: Framing  # default framing (Claude may choose another; no-subject scenes are always no_people)


class CameraEntry(TypedDict):
    name_ja: str
    prompt_en: str
    energy: NotRequired[Energy]


class ColorGrade(TypedDict):
    name: str  # Japanese display name
    name_en: str
    description_ja: str
    ffmpeg_filter: str


class LyricStyle(TypedDict):
    name_ja: str
    description: str
    default_position: Literal["bottom", "center", "top"]


class WorldPreset(TypedDict):
    id: str
    name_ja: str
    name_en: str
    description_ja: str
    look_en: str  # lighting + camera + colour + atmosphere in plain words, comma separated (world bible)
    lighting_en: str  # the light the places really have
    lens_en: str  # plain camera feel (no focal lengths / lens names; kept under its historical key)
    film_en: str  # plain colour response (no film-look / grain words; grain is added in post)
    atmosphere_en: str
    palette: list[str]  # 3 hex colours, for the app's interface only (never in prompts)
    palette_en: str  # the same colours as they occur in the places (reference only, never in prompts)
    palette_ja: str
    scene_types: list[str]
    camera_angles: list[str]
    camera_moves: list[str]
    color_grades: list[str]
    lyric_styles: list[str]  # recommended lyric styles, best first
    negative_en: str
    protagonists: list[Protagonist]
    locations: list[LocalizedText]


# --- scene vocabulary ----------------------------------------------------------------

SCENE_TYPES: dict[str, SceneType] = {
    "establishing_wide": {
        "name_ja": "情景ワイド",
        "prompt_en": (
            "wide shot of {place} as it really looks, everyday details in view, {subject}, small in the frame "
            "and going about their day"
        ),
        "desc_ja": "{place}をありのままに広く見せる情景カット。{subject}は画面の中で小さく、普段どおりに過ごしている",
        "has_subject": True,
        "energy": "low",
        "framing": "full_body",
    },
    "walking": {
        "name_ja": "歩く",
        "prompt_en": (
            "{subject}, walking through {place} at an everyday pace, glancing around, not posing for the camera"
        ),
        "desc_ja": "{subject}が{place}をいつもの足取りで歩く",
        "has_subject": True,
        "energy": "mid",
        "framing": "full_body",
    },
    "back_view": {
        "name_ja": "後ろ姿",
        "prompt_en": (
            "{subject}, seen from behind in {place}, waiting with hands in pockets and shifting weight, then taking "
            "a few steps away"
        ),
        "desc_ja": "{place}で何かを待つ{subject}の後ろ姿。少しして歩き出す",
        "has_subject": True,
        "energy": "low",
        "framing": "full_body",
    },
    "close_portrait": {
        "name_ja": "表情のアップ",
        "prompt_en": (
            "close-up of {subject}, in an unguarded moment in {place}, blinking, eyes on something off-screen, a "
            "small natural change of expression"
        ),
        "desc_ja": "{place}で、ふとした瞬間の{subject}の表情に寄る",
        "has_subject": True,
        "energy": "low",
        "framing": "close_up",
    },
    "profile_window": {
        "name_ja": "窓辺の横顔",
        "prompt_en": (
            "{subject}, in profile by a window with smudges on the glass, chin resting on one hand, the view of "
            "{place} faint in the reflection"
        ),
        "desc_ja": "窓辺で頬杖をつく{subject}の横顔。ガラスに{place}がうっすら映る",
        "has_subject": True,
        "energy": "low",
        "framing": "close_up",
    },
    "turn_to_light": {
        "name_ja": "振り返り",
        "prompt_en": (
            "{subject}, in {place}, turning around as if someone off-screen just called out, a brief real half-smile"
        ),
        "desc_ja": "{place}で、呼ばれたように{subject}が振り返る",
        "has_subject": True,
        "energy": "mid",
        "framing": "medium",
    },
    "running": {
        "name_ja": "走る",
        "prompt_en": "{subject}, running to catch up with something in {place}, slightly out of breath, uneven steps",
        "desc_ja": "{subject}が{place}を息を切らして走る",
        "has_subject": True,
        "energy": "high",
        "framing": "full_body",
    },
    "wind_hair": {
        "name_ja": "風を受ける",
        "prompt_en": "{subject}, caught in gusty wind at {place}, squinting and pushing hair out of their face",
        "desc_ja": "{place}で強い風に目を細め、髪を押さえる{subject}",
        "has_subject": True,
        "energy": "high",
        "framing": "medium",
    },
    "silhouette": {
        "name_ja": "シルエット",
        "prompt_en": (
            "{subject}, against the bright sky at {place}, backlit so the figure is mostly a dark shape, walking "
            "across the frame"
        ),
        "desc_ja": "{place}の明るい空を背に、影になった{subject}が横切る",
        "has_subject": True,
        "energy": "mid",
        "framing": "full_body",
    },
    "spin_sway": {
        "name_ja": "揺れる・回る",
        "prompt_en": (
            "{subject}, dancing a little in {place} to music only they can hear, unselfconscious and slightly "
            "awkward, laughing at themselves"
        ),
        "desc_ja": "{place}で{subject}が音楽に合わせて少しだけ踊る。照れ笑いまじりに",
        "has_subject": True,
        "energy": "high",
        "framing": "full_body",
    },
    "crowd_still": {
        "name_ja": "雑踏の中で",
        "prompt_en": (
            "{subject}, standing still in {place} while ordinary passers-by walk past, the passers-by out of focus "
            "with no clear faces"
        ),
        "desc_ja": "{place}の人の流れの中で立ち止まる{subject}",
        "has_subject": True,
        "energy": "mid",
        "framing": "medium",
    },
    "train_window": {
        "name_ja": "車窓",
        "prompt_en": (
            "{subject}, sitting by the window of a moving local train, head leaning on the glass, {place} passing "
            "outside"
        ),
        "desc_ja": "走る電車の窓にもたれる{subject}。外を{place}が流れていく",
        "has_subject": True,
        "energy": "mid",
        "framing": "medium",
    },
    "rooftop": {
        "name_ja": "屋上",
        "prompt_en": (
            "{subject}, leaning on the railing of an ordinary rooftop above {place}, water tanks and pipes around, "
            "looking down at the street"
        ),
        "desc_ja": "{place}を見下ろす屋上の手すりにもたれる{subject}",
        "has_subject": True,
        "energy": "mid",
        "framing": "medium",
    },
    "lying_down": {
        "name_ja": "寝そべる",
        "prompt_en": "{subject}, lying on their back in {place}, one arm over their eyes, chest rising and falling",
        "desc_ja": "{place}で仰向けに寝転び、腕で目を覆う{subject}",
        "has_subject": True,
        "energy": "low",
        "framing": "full_body",
    },
    "water_reflection": {
        "name_ja": "水面の反射",
        "prompt_en": (
            "the reflection of {subject}, in a puddle or still water in {place}, breaking up when a drop or the "
            "wind disturbs the surface"
        ),
        "desc_ja": "{place}の水面や水たまりに映る{subject}。波紋で揺れる",
        "has_subject": True,
        "energy": "low",
        "framing": "medium",
    },
    "ambient_detail": {
        "name_ja": "質感ディテール",
        "prompt_en": (
            "close detail of ordinary textures in {place}: worn surfaces and small everyday objects, the light "
            "shifting a little as a cloud passes, no people"
        ),
        "desc_ja": "{place}の使い込まれた質感や小物に寄るカット（人物なし）",
        "has_subject": False,
        "energy": "low",
        "framing": "no_people",
    },
    "sky_drift": {
        "name_ja": "空",
        "prompt_en": (
            "the sky above {place} with treetops, wires or rooftops along the lower edge of the frame, clouds "
            "moving at their real speed, no people"
        ),
        "desc_ja": "{place}の空。下端に木々や電線、屋根が入り、雲が流れる（人物なし）",
        "has_subject": False,
        "energy": "low",
        "framing": "no_people",
    },
    "light_abstract": {
        "name_ja": "光の抽象",
        "prompt_en": (
            "the lights of {place} seen through a window pane with smudges and raindrops, slightly out of focus, "
            "no people"
        ),
        "desc_ja": "窓ガラス越しに少しぼけて見える{place}の明かり（人物なし）",
        "has_subject": False,
        "energy": "mid",
        "framing": "no_people",
    },
    "nature_macro": {
        "name_ja": "自然のマクロ",
        "prompt_en": (
            "close-up of grass, weeds and leaves at {place} moving in the breeze, a small insect crawling past, "
            "no people"
        ),
        "desc_ja": "{place}の草や葉に寄るカット。風に揺れる（人物なし）",
        "has_subject": False,
        "energy": "low",
        "framing": "no_people",
    },
    "street_flow": {
        "name_ja": "街の流れ",
        "prompt_en": (
            "ordinary traffic passing through {place}, headlights and reflections on the road surface, no "
            "pedestrians in focus"
        ),
        "desc_ja": "{place}を行き交う車と路面の反射（人物の特定なし）",
        "has_subject": False,
        "energy": "high",
        "framing": "no_people",
    },
}

# Plain camera language: what the camera does, not cinematography jargon.
CAMERA_ANGLES: dict[str, CameraEntry] = {
    "eye_level": {"name_ja": "目線の高さ", "prompt_en": "camera at eye level"},
    "low_angle": {"name_ja": "ローアングル", "prompt_en": "camera low, looking slightly up"},
    "high_angle": {"name_ja": "ハイアングル", "prompt_en": "camera a little above, looking down"},
    "overhead": {"name_ja": "真俯瞰", "prompt_en": "camera looking straight down from above"},
    "over_shoulder": {"name_ja": "肩越し", "prompt_en": "framed over someone's shoulder"},
    "ground_level": {"name_ja": "地面すれすれ", "prompt_en": "camera just above the ground"},
    "through_foreground": {
        "name_ja": "前景越し",
        "prompt_en": "shot past something in the foreground that is slightly out of focus",
    },
    "dutch_tilt": {"name_ja": "ダッチアングル", "prompt_en": "camera slightly tilted"},
}

CAMERA_MOVES: dict[str, CameraEntry] = {
    "static": {"name_ja": "フィックス", "prompt_en": "camera on a tripod, not moving", "energy": "low"},
    "slow_push_in": {"name_ja": "ゆっくり寄る", "prompt_en": "camera moving a little closer", "energy": "low"},
    "slow_pull_out": {
        "name_ja": "ゆっくり引く",
        "prompt_en": "camera slowly moving back to show more of the place",
        "energy": "low",
    },
    "pan_slow": {"name_ja": "ゆっくりパン", "prompt_en": "camera slowly panning to follow", "energy": "low"},
    "tilt_up": {"name_ja": "ティルトアップ", "prompt_en": "camera slowly tilting up", "energy": "low"},
    "rack_focus": {
        "name_ja": "ピント送り",
        "prompt_en": "camera still, focus shifting from the foreground to the background",
        "energy": "low",
    },
    "handheld": {"name_ja": "手持ち", "prompt_en": "handheld camera with slight natural shake", "energy": "mid"},
    "tracking_side": {"name_ja": "横移動", "prompt_en": "camera moving alongside at walking pace", "energy": "mid"},
    "orbit": {"name_ja": "回り込み", "prompt_en": "handheld camera moving partway around", "energy": "mid"},
    "crane_up": {"name_ja": "クレーンアップ", "prompt_en": "camera rising slowly to a higher view", "energy": "mid"},
    "follow_behind": {
        "name_ja": "追従",
        "prompt_en": "handheld camera following a few steps behind",
        "energy": "high",
    },
    # Kept for stored storyboards and the enum; presets no longer pick it (drone reveals read as stock AI footage).
    "drone_rise": {
        "name_ja": "ドローン上昇",
        "prompt_en": "high view from above the area, camera drifting slowly forward",
        "energy": "high",
    },
    "fast_push": {"name_ja": "勢いよく寄る", "prompt_en": "camera moving quickly closer", "energy": "high"},
}

SECTION_LABELS_JA: dict[str, str] = {
    "intro": "イントロ",
    "verse": "Aメロ",
    "chorus": "サビ",
    "bridge": "ブリッジ",
    "outro": "アウトロ",
    "break": "間奏",
}

# --- colour grades ---------------------------------------------------------------------


def _saturation(amount: float) -> str:
    """``colorchannelmixer`` that scales chroma around Rec.709 luma (exact, in RGB; 1 = unchanged)."""
    kr, kg, kb = 0.2126, 0.7152, 0.0722
    inv = 1.0 - amount

    def c(value: float) -> str:
        return f"{value:.4f}".rstrip("0").rstrip(".")

    return (
        f"colorchannelmixer=rr={c(kr + (1 - kr) * amount)}:rg={c(kg * inv)}:rb={c(kb * inv)}"
        f":gr={c(kr * inv)}:gg={c(kg + (1 - kg) * amount)}:gb={c(kb * inv)}"
        f":br={c(kr * inv)}:bg={c(kg * inv)}:bb={c(kb + (1 - kb) * amount)}"
    )


COLOR_GRADES: dict[str, ColorGrade] = {
    "natural": {
        "name": "ナチュラル",
        "name_en": "Natural",
        "description_ja": "色を整える程度の自然な補正",
        "ffmpeg_filter": f"{_saturation(1.05)},curves=interp=pchip:m='0/0 0.25/0.235 0.75/0.775 1/1'",
    },
    "teal_orange": {
        "name": "ティール＆オレンジ",
        "name_en": "Teal & Orange",
        "description_ja": "影は青緑、肌とハイライトは暖色の映画的な配色",
        "ffmpeg_filter": (
            "colorbalance=rm=0.04:bm=-0.06:rh=0.07:gh=0.02:bh=-0.07,"
            f"{_saturation(1.14)},"
            "curves=interp=pchip:r='0/0 0.2/0.15 0.5/0.5 1/1':g='0/0.01 0.5/0.5 1/1'"
            ":b='0/0.04 0.2/0.25 0.5/0.48 1/0.96':m='0/0 0.12/0.09 0.25/0.21 0.75/0.8 1/1'"
        ),
    },
    "warm_film": {
        "name": "ウォームフィルム",
        "name_en": "Warm Film",
        "description_ja": "黒が少し浮いた暖かいフィルム調",
        "ffmpeg_filter": (
            "colortemperature=temperature=5200:mix=0.55:pl=0.6,"
            "curves=interp=pchip:r='0/0.05 0.5/0.53 1/0.97':g='0/0.04 0.5/0.5 1/0.95':b='0/0.06 0.5/0.46 1/0.88',"
            f"{_saturation(0.93)}"
        ),
    },
    "cool_night": {
        "name": "クールナイト",
        "name_en": "Cool Night",
        "description_ja": "青みの強い夜の空気感。影を締めて静けさを出す",
        "ffmpeg_filter": (
            "colortemperature=temperature=9000:mix=0.45:pl=0.5,"
            "curves=interp=pchip:r='0/0 0.5/0.47 1/0.96':g='0/0.01 0.5/0.49 1/0.98':b='0/0.04 0.5/0.54 1/1'"
            ":m='0/0 0.12/0.085 0.5/0.49 1/1',"
            f"{_saturation(0.86)}"
        ),
    },
    "bleach_bypass": {
        "name": "ブリーチバイパス",
        "name_en": "Bleach Bypass",
        "description_ja": "低彩度・高コントラストの硬質なルック",
        "ffmpeg_filter": (
            f"{_saturation(0.5)},curves=interp=pchip:m='0/0 0.157/0.094 0.333/0.24 0.5/0.5 0.667/0.76 0.843/0.906 1/1'"
        ),
    },
    "faded_pastel": {
        "name": "フェードパステル",
        "name_en": "Faded Pastel",
        "description_ja": "淡く褪せたパステル調。柔らかく軽い印象",
        "ffmpeg_filter": (
            f"{_saturation(0.78)},colorbalance=rh=0.04:bh=0.03,"
            "curves=interp=pchip:g='0/0 0.1/0.085 0.3/0.3 1/1':m='0/0.1 0.5/0.56 1/0.94'"
        ),
    },
    "mono_noir": {
        "name": "モノクロノワール",
        "name_en": "Mono Noir",
        "description_ja": "コントラストの強いモノクロ",
        "ffmpeg_filter": (
            f"{_saturation(0.0)},curves=interp=pchip:m='0/0 0.12/0.075 0.25/0.165 0.5/0.47 0.75/0.83 1/1'"
        ),
    },
    "vivid": {
        "name": "ビビッド",
        "name_en": "Vivid",
        "description_ja": "鮮やかで抜けの良い発色",
        "ffmpeg_filter": (
            f"vibrance=intensity=0.25,{_saturation(1.12)},"
            "curves=interp=pchip:m='0/0 0.1/0.085 0.25/0.225 0.75/0.79 1/1'"
        ),
    },
    "golden_hour": {
        "name": "ゴールデンアワー",
        "name_en": "Golden Hour",
        "description_ja": "夕方の金色の光。肌が温かく見える",
        "ffmpeg_filter": (
            "colortemperature=temperature=4300:mix=0.45:pl=0.7,"
            "colorbalance=rh=0.05:gh=0.03:bh=-0.05:bs=0.03,"
            f"{_saturation(1.06)},curves=interp=pchip:m='0/0.02 0.5/0.52 1/0.98'"
        ),
    },
    "neon_night": {
        "name": "ネオンナイト",
        "name_en": "Neon Night",
        "description_ja": "マゼンタとシアンが映える夜の街のルック",
        "ffmpeg_filter": (
            "colorbalance=rm=0.05:gm=-0.05:rh=0.03:bh=0.05,"
            f"{_saturation(1.3)},"
            "curves=interp=pchip:r='0/0.025 0.15/0.16 0.5/0.5 1/1':g='0/0 0.15/0.135 0.5/0.5 1/1'"
            ":b='0/0.05 0.15/0.19 0.5/0.49 1/1':m='0/0 0.12/0.09 0.3/0.245 0.7/0.765 1/1'"
        ),
    },
    "forest_moody": {
        "name": "フォレストムーディ",
        "name_en": "Moody Forest",
        "description_ja": "深い緑と落ち着いた陰影",
        "ffmpeg_filter": (
            "colorbalance=gm=0.04:rh=0.03:bh=-0.02,"
            f"{_saturation(0.86)},"
            "curves=interp=pchip:r='0/0 0.15/0.125 0.5/0.49 1/1':g='0/0.03 0.15/0.17 0.5/0.5 1/1'"
            ":b='0/0.02 0.15/0.16 0.5/0.49 1/1':m='0/0.02 0.12/0.095 0.25/0.195 0.75/0.775 1/0.97'"
        ),
    },
    "lavender_haze": {
        "name": "ラベンダーヘイズ",
        "name_en": "Lavender Haze",
        "description_ja": "淡い紫がかった霞のような夢見心地の色",
        "ffmpeg_filter": (
            "colorbalance=rs=0.04:bs=0.08:gm=-0.03:rh=0.03:bh=0.05,"
            f"{_saturation(0.85)},curves=interp=pchip:m='0/0.08 0.5/0.55 1/0.96'"
        ),
    },
}

# --- lyric styles --------------------------------------------------------------------------

LYRIC_STYLES: dict[str, LyricStyle] = {
    "simple": {
        "name_ja": "シンプル",
        "description": "白の太字ゴシックに細い縁取りと柔らかい影。どの映像にも合う定番スタイル",
        "default_position": "bottom",
    },
    "cinematic": {
        "name_ja": "シネマティック",
        "description": "明朝体・小さめの文字を半透明の帯に載せる映画字幕風",
        "default_position": "bottom",
    },
    "minimal": {
        "name_ja": "ミニマル",
        "description": "細字で字間を広げ、縁取りなし。余白を活かした上品な表示",
        "default_position": "center",
    },
    "karaoke": {
        "name_ja": "カラオケ",
        "description": "歌に合わせて文字の色が左から流れるように変わる",
        "default_position": "bottom",
    },
    "fade": {
        "name_ja": "フェード",
        "description": "ビートに合わせて歌詞がふわっと現れて消える",
        "default_position": "center",
    },
}

# --- wardrobe ----------------------------------------------------------------------------------


def _words(text: str) -> list[str]:
    return text.split()


# Garment nouns → body region (the last garment noun of a phrase decides: "tote bag" → carry, "sweater dress" → top).
_GARMENT_NOUNS: dict[str, GarmentRegion] = {
    **dict.fromkeys(
        _words(
            "sneakers shoes shoe boots boot sandals heels loafers trainers slippers flats pumps socks clogs oxfords "
            "mules geta footwear"
        ),
        "feet",
    ),
    **dict.fromkeys(
        _words(
            "skirt trousers pants jeans shorts slacks leggings chinos culottes tights stockings joggers sweatpants "
            "corduroys hakama"
        ),
        "bottom",
    ),
    **dict.fromkeys(
        _words(
            "coat jacket trench parka cardigan blazer raincoat overcoat windbreaker poncho cape anorak gilet vest "
            "bomber shawl"
        ),
        "outer",
    ),
    **dict.fromkeys(
        _words(
            "shirt t-shirt tee blouse sweater jumper hoodie sweatshirt top camisole tank turtleneck polo dress "
            "sundress gown kimono yukata uniform jumpsuit overalls robe tunic pullover"
        ),
        "top",
    ),
    **dict.fromkeys(
        _words(
            "hat cap beanie beret glasses sunglasses spectacles earrings earring necklace chain choker scarf "
            "headband hairpin hairclip bandana pendant piercings"
        ),
        "head",
    ),
    **dict.fromkeys(_words("bag tote backpack rucksack purse satchel handbag watch belt bracelet"), "carry"),
}
_ARTICLE_WORDS = frozenset({"a", "an", "the", "some", "her", "his", "their", "two", "pair"})


def garment_region(text: str) -> GarmentRegion | None:
    """Body region of a garment phrase (``"white canvas sneakers"`` → ``feet``), ``None`` when it names no garment."""
    region: GarmentRegion | None = None
    for word in re.findall(r"[a-z]+(?:-[a-z]+)*", (text or "").lower()):
        region = _GARMENT_NOUNS.get(word, region)
    return region


def _with_article(item: str) -> str:
    text = " ".join((item or "").split()).strip(" ,.;")
    words = text.split()
    if not words or words[0].lower() in _ARTICLE_WORDS:
        return text
    last = re.sub(r"[^a-z]", "", words[-1].lower())
    if last.endswith("s") and not last.endswith(("ss", "us", "is")):  # plural: jeans, sneakers, earrings
        return text
    return f"{'an' if words[0][0].lower() in 'aeiou' else 'a'} {text}"


def _join(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}" if items else ""


def wardrobe_phrase(garments: Sequence[Mapping[str, str]]) -> str:
    """Garments as an English phrase for "wearing …": outerwear over the top, then bottoms, shoes, accessories.

    ``[{outer: charcoal wool coat}, {top: cream knit sweater}, {feet: black boots}]`` →
    ``"a charcoal wool coat over a cream knit sweater and black boots"``.
    """
    by_region: dict[str, list[str]] = {region: [] for region in GARMENT_REGIONS}
    for garment in garments:
        item = _with_article(str(garment.get("item_en") or ""))
        if item and garment.get("region") in by_region and item not in by_region[str(garment.get("region"))]:
            by_region[str(garment.get("region"))].append(item)
    outer, top = by_region["outer"], by_region["top"]
    main = [f"{outer[0]} over {top[0]}", *outer[1:], *top[1:]] if outer and top else [*outer, *top]
    main += by_region["bottom"] + by_region["feet"]
    extras = by_region["head"] + by_region["carry"]
    if not main:
        return _join(extras)
    return f"{_join(main)}, with {_join(extras)}" if extras else _join(main)


def visible_garments(garments: Sequence[Mapping[str, str]], framing: str) -> list[Mapping[str, str]]:
    """The garments a shot of this ``framing`` shows (``FRAMING_REGIONS``); unknown framings show none."""
    regions = FRAMING_REGIONS.get(framing, frozenset())
    return [g for g in garments if g.get("region") in regions and str(g.get("item_en") or "").strip()]


def _person(identity_en: str, wardrobe: Sequence[tuple[GarmentRegion, str]], ja: str) -> Protagonist:
    garments: list[Garment] = [{"region": region, "item_en": item} for region, item in wardrobe]
    return {
        "en": f"{identity_en}, wearing {wardrobe_phrase(garments)}",
        "ja": ja,
        "identity_en": identity_en,
        "wardrobe": garments,
    }


# --- world presets ----------------------------------------------------------------------------
#
# Plain, naturalistic wording: real light sources, true-to-life colour, ordinary people and places.
# No focal lengths, lens names, film looks, grain, halation or palette names (they read as stock AI imagery).

_BASE_ANGLES = ["eye_level", "low_angle", "high_angle", "over_shoulder", "through_foreground"]


def _preset(
    *,
    id: str,
    name_ja: str,
    name_en: str,
    description_ja: str,
    lighting_en: str,
    lens_en: str,
    film_en: str,
    atmosphere_en: str,
    palette: list[str],
    palette_en: str,
    palette_ja: str,
    scene_types: list[str],
    camera_angles: list[str],
    camera_moves: list[str],
    color_grades: list[str],
    lyric_styles: list[str],
    negative_en: str,
    protagonists: list[Protagonist],
    locations: list[LocalizedText],
) -> WorldPreset:
    return {
        "id": id,
        "name_ja": name_ja,
        "name_en": name_en,
        "description_ja": description_ja,
        "look_en": f"{lighting_en}, {lens_en}, {film_en}, {atmosphere_en}",
        "lighting_en": lighting_en,
        "lens_en": lens_en,
        "film_en": film_en,
        "atmosphere_en": atmosphere_en,
        "palette": palette,
        "palette_en": palette_en,
        "palette_ja": palette_ja,
        "scene_types": scene_types,
        "camera_angles": camera_angles,
        "camera_moves": camera_moves,
        "color_grades": color_grades,
        "lyric_styles": lyric_styles,
        "negative_en": negative_en,
        "protagonists": protagonists,
        "locations": locations,
    }


WORLD_PRESETS: dict[str, WorldPreset] = {
    "cinematic": _preset(
        id="cinematic",
        name_ja="シネマティック",
        name_en="Cinematic",
        description_ja="映画のような光と構図。実際にある光と陰影で、1カットごとにドラマを感じさせる王道のルック。",
        lighting_en=(
            "only the light the place really has: street lamps, lit windows and the last daylight, deep shadows"
        ),
        lens_en="steady camera at human height with a natural field of view",
        film_en="true-to-life colour with slightly muted contrast",
        atmosphere_en="damp air after rain, puddles and wet pavement",
        palette=["#0F2A3A", "#E39B55", "#1B1B1F"],
        palette_en="dark blue-green shadows, warm street-lamp light and charcoal asphalt",
        palette_ja="深いティール・アンバー・チャコール",
        scene_types=[
            "establishing_wide",
            "walking",
            "back_view",
            "close_portrait",
            "profile_window",
            "turn_to_light",
            "running",
            "silhouette",
            "rooftop",
            "water_reflection",
            "light_abstract",
            "sky_drift",
        ],
        camera_angles=[*_BASE_ANGLES, "ground_level"],
        camera_moves=[
            "static",
            "slow_push_in",
            "slow_pull_out",
            "tracking_side",
            "handheld",
            "orbit",
            "crane_up",
            "follow_behind",
            "rack_focus",
        ],
        color_grades=["teal_orange", "warm_film", "cool_night", "natural"],
        lyric_styles=["cinematic", "simple", "fade"],
        negative_en="flat lighting, harsh on-camera flash, glossy commercial look",
        protagonists=[
            _person(
                "a fictional woman in her late twenties with shoulder-length dark hair and an ordinary, "
                "unretouched face",
                [
                    ("outer", "long charcoal wool coat"),
                    ("top", "cream knit sweater"),
                    ("bottom", "dark grey straight-leg trousers"),
                    ("feet", "black leather ankle boots"),
                ],
                "肩までの黒髪にチャコールのロングコートを着た20代後半の女性（架空の人物）",
            ),
            _person(
                "a fictional man in his late twenties with short black hair, light stubble and an ordinary face",
                [
                    ("outer", "dark navy overcoat"),
                    ("top", "plain white shirt"),
                    ("bottom", "charcoal trousers"),
                    ("feet", "black leather shoes"),
                    ("head", "grey wool scarf"),
                ],
                "短い黒髪に紺のオーバーコートとグレーのマフラーの20代後半の男性（架空の人物）",
            ),
        ],
        locations=[
            {"en": "a quiet residential street after rain at dusk", "ja": "雨上がりの夕暮れの住宅街"},
            {
                "en": "an empty coastal road at dusk with guard rails and wind-bent grass",
                "ja": "ガードレールと風になびく草のある夕暮れの海沿いの道",
            },
            {
                "en": "a small train platform at night under fluorescent lights",
                "ja": "蛍光灯の灯る夜の小さな駅のホーム",
            },
        ],
    ),
    "emotional": _preset(
        id="emotional",
        name_ja="エモーショナル",
        name_en="Emotional",
        description_ja="柔らかな自然光と余韻のある間。感情に寄り添う親密なカットで、切なさや温かさを描く。",
        lighting_en="soft daylight from a window or late-afternoon sun, light bouncing off pale walls",
        lens_en="handheld camera close to the person, natural field of view",
        film_en="warm true-to-life colour with gentle contrast",
        atmosphere_en="quiet lived-in rooms and streets, small everyday details",
        palette=["#F2C6A0", "#7A8FA6", "#FFF4E6"],
        palette_en="warm skin and wood tones, faded blue denim and cream walls",
        palette_ja="ピーチ・くすんだブルー・クリーム",
        scene_types=[
            "close_portrait",
            "profile_window",
            "turn_to_light",
            "back_view",
            "walking",
            "lying_down",
            "spin_sway",
            "silhouette",
            "water_reflection",
            "sky_drift",
            "ambient_detail",
            "light_abstract",
            "train_window",
        ],
        camera_angles=["eye_level", "high_angle", "over_shoulder", "through_foreground", "ground_level"],
        camera_moves=[
            "static",
            "slow_push_in",
            "slow_pull_out",
            "handheld",
            "orbit",
            "rack_focus",
            "tracking_side",
            "pan_slow",
        ],
        color_grades=["golden_hour", "warm_film", "faded_pastel", "natural"],
        lyric_styles=["fade", "simple", "minimal"],
        negative_en="harsh contrast, cold clinical light",
        protagonists=[
            _person(
                "a fictional young woman in her early twenties with long wavy chestnut hair, freckles and an "
                "ordinary, unretouched face",
                [
                    ("top", "oversized white cotton shirt"),
                    ("bottom", "light blue jeans"),
                    ("feet", "white canvas sneakers"),
                    ("head", "thin silver necklace"),
                ],
                "長い栗色のウェーブヘアに白いオーバーサイズシャツの20代前半の女性（架空の人物）",
            ),
            _person(
                "a fictional young man in his early twenties with tousled brown hair and an ordinary face",
                [
                    ("outer", "faded denim jacket"),
                    ("top", "white t-shirt"),
                    ("bottom", "black chinos"),
                    ("feet", "worn brown leather boots"),
                ],
                "くしゃっとした茶髪にデニムジャケットの20代前半の男性（架空の人物）",
            ),
        ],
        locations=[
            {
                "en": "a small sunlit bedroom with thin curtains and a cluttered desk",
                "ja": "薄いカーテンと散らかった机のある、陽の入る小さな部屋",
            },
            {"en": "a riverside path with benches in late-afternoon light", "ja": "ベンチのある午後遅くの川沿いの小道"},
            {
                "en": "a seaside town with narrow streets and laundry drying on balconies",
                "ja": "ベランダに洗濯物が干された、細い路地の海辺の町",
            },
        ],
    ),
    "urban_night": _preset(
        id="urban_night",
        name_ja="都会の夜",
        name_en="Urban Night",
        description_ja="ネオンと雨の反射、行き交う光。夜の街を歩く孤独とスピード感をスタイリッシュに。",
        lighting_en=(
            "mixed street light from lit shop fronts, vending machines, traffic lights and car headlights, wet "
            "reflections, deep blacks"
        ),
        lens_en="handheld camera at street level, natural field of view",
        film_en="true-to-life night colour with real dark shadows",
        atmosphere_en="light rain, wet asphalt, steam rising from a drain",
        palette=["#0B0F2B", "#FF2E88", "#2EE6FF"],
        palette_en="dark night sky, pink and cyan shop lights reflected on wet asphalt",
        palette_ja="ミッドナイトネイビー・ネオンマゼンタ・シアン",
        scene_types=[
            "walking",
            "crowd_still",
            "train_window",
            "rooftop",
            "back_view",
            "close_portrait",
            "running",
            "street_flow",
            "light_abstract",
            "water_reflection",
            "turn_to_light",
            "establishing_wide",
        ],
        camera_angles=[*_BASE_ANGLES, "dutch_tilt"],
        camera_moves=[
            "static",
            "slow_push_in",
            "tracking_side",
            "follow_behind",
            "handheld",
            "orbit",
            "fast_push",
            "pan_slow",
        ],
        color_grades=["neon_night", "cool_night", "teal_orange", "vivid"],
        lyric_styles=["simple", "karaoke", "cinematic"],
        negative_en="daylight, readable signage, legible neon words, brand logos",
        protagonists=[
            _person(
                "a fictional woman in her mid-twenties with a black bob haircut and an ordinary, unretouched face",
                [
                    ("outer", "black leather jacket"),
                    ("top", "grey cotton t-shirt"),
                    ("bottom", "black jeans"),
                    ("feet", "black lace-up boots"),
                    ("head", "small silver hoop earrings"),
                ],
                "黒髪のボブに黒のレザージャケットとシルバーのフープピアスの20代半ばの女性（架空の人物）",
            ),
            _person(
                "a fictional man in his mid-twenties with short bleached hair and an ordinary face",
                [
                    ("outer", "black hooded jacket"),
                    ("top", "white t-shirt"),
                    ("bottom", "dark cargo trousers"),
                    ("feet", "black sneakers"),
                    ("head", "thin silver chain necklace"),
                ],
                "短いブリーチヘアに黒のフードジャケットとシルバーチェーンの20代半ばの男性（架空の人物）",
            ),
        ],
        locations=[
            {"en": "a downtown side street in the rain at night", "ja": "雨の夜の繁華街の裏通り"},
            {
                "en": "a narrow alley lit by small shop fronts with no readable signs",
                "ja": "読める看板のない小さな店の明かりが灯る細い路地",
            },
            {"en": "the platform of an elevated train line at night", "ja": "夜の高架駅のホーム"},
        ],
    ),
    "nostalgic": _preset(
        id="nostalgic",
        name_ja="ノスタルジック",
        name_en="Nostalgic",
        description_ja="フィルムのような粒子と褪せた色。夏休みの記憶のような、懐かしく少し切ない情景。",
        lighting_en="warm, slightly hazy late-afternoon sunlight",
        lens_en="handheld camera with slightly imperfect framing, natural field of view",
        film_en="warm, slightly faded true-to-life colour",
        atmosphere_en="summer heat, sun-bleached walls, weeds growing by the road",
        palette=["#E8C07D", "#6E9C9F", "#C96F53"],
        palette_en="sun-faded yellow walls, green sea and rusty terracotta roofs",
        palette_ja="褪せたマスタード・シーグラスグリーン・テラコッタ",
        scene_types=[
            "walking",
            "back_view",
            "train_window",
            "profile_window",
            "turn_to_light",
            "lying_down",
            "running",
            "sky_drift",
            "ambient_detail",
            "nature_macro",
            "establishing_wide",
            "water_reflection",
            "wind_hair",
        ],
        camera_angles=["eye_level", "low_angle", "high_angle", "over_shoulder", "through_foreground"],
        camera_moves=["static", "handheld", "pan_slow", "slow_push_in", "tracking_side", "tilt_up", "rack_focus"],
        color_grades=["warm_film", "faded_pastel", "golden_hour", "natural"],
        lyric_styles=["minimal", "fade", "cinematic"],
        negative_en="modern smartphones, crisp digital look, high-tech gadgets",
        protagonists=[
            _person(
                "a fictional young woman in her early twenties with a low ponytail, sun-tanned skin and an "
                "ordinary face",
                [("top", "pale yellow cotton sundress"), ("feet", "white canvas sneakers")],
                "低いポニーテールに淡い黄色のワンピースと白いスニーカーの20代前半の女性（架空の人物）",
            ),
            _person(
                "a fictional young man in his early twenties with short messy hair and an ordinary face",
                [
                    ("top", "loose white short-sleeved shirt"),
                    ("bottom", "khaki shorts"),
                    ("feet", "worn rubber sandals"),
                ],
                "短いくせ毛に白い半袖シャツとカーキの短パンの20代前半の男性（架空の人物）",
            ),
        ],
        locations=[
            {
                "en": "a small seaside town street lined with old wooden houses in summer",
                "ja": "古い木造家屋が並ぶ夏の海辺の町",
            },
            {"en": "a countryside railway crossing surrounded by rice fields", "ja": "田んぼに囲まれた田舎の踏切"},
            {"en": "a quiet local train station in midsummer", "ja": "真夏の小さな無人駅"},
        ],
    ),
    "nature": _preset(
        id="nature",
        name_ja="ネイチャー",
        name_en="Nature",
        description_ja="森・海・空・風。自然光とスケールの大きな風景で、壮大さと解放感を描く。",
        lighting_en="natural daylight, soft morning light or a low evening sun",
        lens_en="camera at human height with a wide but natural field of view",
        film_en="true-to-life colour with natural contrast",
        atmosphere_en="wind moving grass and branches, morning mist lying in low places",
        palette=["#2F5D3A", "#A8C5DA", "#E9DFC4"],
        palette_en="green grass and trees, pale sky and sandy paths",
        palette_ja="フォレストグリーン・スカイブルー・サンドベージュ",
        scene_types=[
            "establishing_wide",
            "back_view",
            "walking",
            "wind_hair",
            "lying_down",
            "running",
            "silhouette",
            "nature_macro",
            "sky_drift",
            "water_reflection",
            "close_portrait",
            "spin_sway",
        ],
        camera_angles=["eye_level", "low_angle", "high_angle", "overhead", "ground_level", "through_foreground"],
        camera_moves=[
            "static",
            "handheld",
            "slow_push_in",
            "slow_pull_out",
            "pan_slow",
            "crane_up",
            "tracking_side",
            "orbit",
            "tilt_up",
        ],
        color_grades=["natural", "forest_moody", "golden_hour", "vivid"],
        lyric_styles=["minimal", "simple", "fade"],
        negative_en="buildings, cars, litter, power lines",
        protagonists=[
            _person(
                "a fictional woman in her mid-twenties with long dark braided hair, sun-tanned skin and an ordinary "
                "face",
                [
                    ("outer", "light grey knit cardigan"),
                    ("top", "loose olive linen dress"),
                    ("feet", "brown leather walking boots"),
                ],
                "長い黒髪の三つ編みにオリーブ色のリネンワンピースの20代半ばの女性（架空の人物）",
            ),
            _person(
                "a fictional man in his mid-twenties with shoulder-length dark hair and an ordinary face",
                [
                    ("top", "beige linen shirt"),
                    ("bottom", "faded olive trousers"),
                    ("feet", "worn hiking boots"),
                ],
                "肩までの黒髪にベージュのリネンシャツの20代半ばの男性（架空の人物）",
            ),
        ],
        locations=[
            {"en": "a mountain meadow at sunrise with low mist", "ja": "朝霧の残る日の出の高原"},
            {"en": "a grassy cliff path above the ocean on a windy day", "ja": "風の強い日の、海を見下ろす崖の小道"},
            {
                "en": "an old forest with moss-covered ground and patches of sunlight",
                "ja": "苔むした地面に木漏れ日が落ちる古い森",
            },
        ],
    ),
    "dark": _preset(
        id="dark",
        name_ja="ダーク",
        name_en="Dark",
        description_ja="低いキー、硬い影、霧。緊張感とミステリアスな空気をまとう、陰影の強い世界。",
        lighting_en="low light from a single real source such as a bare bulb or a street lamp, deep shadows",
        lens_en="steady camera with a natural field of view",
        film_en="muted, cool true-to-life colour",
        atmosphere_en="cold damp air, light fog, concrete and wet surfaces",
        palette=["#0A0A0C", "#4A5560", "#8C1C13"],
        palette_en="near-black shadows, cold grey concrete and a dark red accent",
        palette_ja="漆黒・冷たいスレートグレー・深紅",
        scene_types=[
            "silhouette",
            "close_portrait",
            "back_view",
            "walking",
            "turn_to_light",
            "crowd_still",
            "profile_window",
            "ambient_detail",
            "light_abstract",
            "establishing_wide",
            "water_reflection",
            "wind_hair",
        ],
        camera_angles=["low_angle", "high_angle", "overhead", "over_shoulder", "dutch_tilt", "through_foreground"],
        camera_moves=[
            "static",
            "slow_push_in",
            "slow_pull_out",
            "handheld",
            "orbit",
            "rack_focus",
            "fast_push",
            "follow_behind",
        ],
        color_grades=["bleach_bypass", "mono_noir", "cool_night", "teal_orange"],
        lyric_styles=["cinematic", "minimal", "fade"],
        negative_en="gore, blood, weapons, bright cheerful colors",
        protagonists=[
            _person(
                "a fictional woman in her late twenties with slicked-back dark hair and an ordinary, unretouched face",
                [
                    ("outer", "black high-neck coat"),
                    ("top", "black turtleneck"),
                    ("bottom", "black wool trousers"),
                    ("feet", "black leather boots"),
                ],
                "オールバックの黒髪に黒のハイネックコートの20代後半の女性（架空の人物）",
            ),
            _person(
                "a fictional man in his late twenties with damp dark hair and an ordinary face",
                [
                    ("outer", "black trench coat"),
                    ("top", "dark grey shirt"),
                    ("bottom", "black trousers"),
                    ("feet", "black leather shoes"),
                ],
                "濡れたような黒髪に黒のトレンチコートの20代後半の男性（架空の人物）",
            ),
        ],
        locations=[
            {"en": "an empty concrete warehouse with puddles on the floor", "ja": "床に水たまりのある、がらんとした倉庫"},
            {"en": "a dark forest road at night lit by a single streetlight", "ja": "街灯ひとつの夜の森の道"},
            {"en": "a deserted underground parking garage", "ja": "人のいない地下駐車場"},
        ],
    ),
    "dreamy": _preset(
        id="dreamy",
        name_ja="ドリーミー",
        name_en="Dreamy",
        description_ja="やわらかな光と淡い色。現実の中にふと現れる夢のような瞬間を、静かに切り取る世界。",
        lighting_en="soft overcast or early-morning light, pale and even",
        lens_en="camera close to the subject with a natural field of view",
        film_en="soft pastel colours as they really are in the place, gentle contrast",
        atmosphere_en="quiet, slightly hazy air, curtains or tall grass moving a little",
        palette=["#F6C1D9", "#B9C7F2", "#FFFFFF"],
        palette_en="pale pink flowers, light blue sky and white fabric",
        palette_ja="ブラッシュピンク・ペリウィンクル・パールホワイト",
        scene_types=[
            "spin_sway",
            "lying_down",
            "close_portrait",
            "turn_to_light",
            "back_view",
            "walking",
            "silhouette",
            "light_abstract",
            "sky_drift",
            "water_reflection",
            "nature_macro",
            "wind_hair",
        ],
        camera_angles=["eye_level", "high_angle", "overhead", "through_foreground", "ground_level"],
        camera_moves=["static", "slow_push_in", "slow_pull_out", "handheld", "orbit", "rack_focus", "tracking_side"],
        color_grades=["lavender_haze", "faded_pastel", "golden_hour", "natural"],
        lyric_styles=["fade", "minimal", "simple"],
        negative_en="harsh shadows, gritty textures, dark horror mood",
        protagonists=[
            _person(
                "a fictional young woman in her early twenties with long light-brown hair and an ordinary, "
                "unretouched face",
                [("top", "loose pale lilac cotton dress"), ("feet", "white ballet flats")],
                "長い明るい茶髪に淡いライラックのワンピースの20代前半の女性（架空の人物）",
            ),
            _person(
                "a fictional young man in his early twenties with soft light-brown hair and an ordinary face",
                [
                    ("top", "loose white linen shirt"),
                    ("bottom", "light grey linen trousers"),
                    ("feet", "white canvas sneakers"),
                ],
                "柔らかな明るい茶髪に白いリネンシャツの20代前半の男性（架空の人物）",
            ),
        ],
        locations=[
            {"en": "a field of white wildflowers under a pale sky", "ja": "淡い空の下の白い野花の原っぱ"},
            {"en": "a shallow, still lake at dawn", "ja": "夜明けの静かな浅い湖"},
            {"en": "a small greenhouse full of hanging plants", "ja": "植物が吊るされた小さな温室"},
        ],
    ),
    "documentary": _preset(
        id="documentary",
        name_ja="ドキュメンタリー",
        name_en="Documentary",
        description_ja="手持ちカメラと自然光。飾らない日常の断片を切り取る、リアルで温度のある映像。",
        lighting_en="available natural light and real practical lights, unpolished",
        lens_en="handheld camera with a natural field of view and moderate depth of field",
        film_en="muted, low-contrast true-to-life colour",
        atmosphere_en="candid everyday moments, authentic textures of real life",
        palette=["#5B6770", "#C9B79C", "#2E3B2F"],
        palette_en="grey concrete, warm beige walls and dark green plants",
        palette_ja="スレート・ウォームベージュ・ダークオリーブ",
        scene_types=[
            "walking",
            "crowd_still",
            "train_window",
            "profile_window",
            "back_view",
            "close_portrait",
            "rooftop",
            "ambient_detail",
            "street_flow",
            "establishing_wide",
            "sky_drift",
            "turn_to_light",
        ],
        camera_angles=["eye_level", "over_shoulder", "high_angle", "through_foreground", "low_angle"],
        camera_moves=["handheld", "static", "pan_slow", "follow_behind", "tracking_side", "rack_focus", "slow_push_in"],
        color_grades=["natural", "bleach_bypass", "warm_film", "forest_moody"],
        lyric_styles=["simple", "minimal", "cinematic"],
        negative_en="glossy commercial look, staged posing, heavy color grading",
        protagonists=[
            _person(
                "a fictional woman in her early thirties with short tousled hair and an ordinary, unretouched face",
                [
                    ("top", "grey hoodie"),
                    ("bottom", "dark blue jeans"),
                    ("feet", "scuffed white sneakers"),
                    ("carry", "canvas tote bag"),
                ],
                "短いくしゃっとした髪にグレーのパーカーとキャンバストートの30代前半の女性（架空の人物）",
            ),
            _person(
                "a fictional man in his early thirties with a short beard and an ordinary face",
                [
                    ("outer", "worn olive work jacket"),
                    ("top", "plain navy t-shirt"),
                    ("bottom", "grey work trousers"),
                    ("feet", "brown work boots"),
                    ("head", "knit beanie"),
                ],
                "短いひげにオリーブのワークジャケットとニット帽の30代前半の男性（架空の人物）",
            ),
        ],
        locations=[
            {"en": "a busy early-morning neighborhood with small shops opening", "ja": "店が開き始める朝の商店街"},
            {"en": "a commuter train and station during the morning rush", "ja": "朝の通勤電車と駅"},
            {"en": "a small apartment kitchen in morning light", "ja": "朝の光が入る小さなアパートの台所"},
        ],
    ),
    "custom": _preset(
        id="custom",
        name_ja="カスタム",
        name_en="Custom",
        description_ja="入力した世界観テキストをもとに絵コンテを作ります（日本語で自由に記述できます）。",
        lighting_en="natural light that fits the place, soft contrast",
        lens_en="camera at human height with a natural field of view",
        film_en="true-to-life colour",
        atmosphere_en="real, lived-in surroundings",
        palette=["#2B2D42", "#D9A066", "#EDF2F4"],
        palette_en="dark blue shadows, warm ochre light and off-white walls",
        palette_ja="インクブルー・オーカー・オフホワイト",
        scene_types=[
            "establishing_wide",
            "walking",
            "back_view",
            "close_portrait",
            "profile_window",
            "turn_to_light",
            "running",
            "silhouette",
            "wind_hair",
            "ambient_detail",
            "sky_drift",
            "light_abstract",
        ],
        camera_angles=[*_BASE_ANGLES, "ground_level"],
        camera_moves=[
            "static",
            "slow_push_in",
            "slow_pull_out",
            "tracking_side",
            "handheld",
            "orbit",
            "crane_up",
            "follow_behind",
        ],
        color_grades=["natural", "teal_orange", "warm_film", "faded_pastel"],
        lyric_styles=["simple", "cinematic", "fade"],
        negative_en="",
        protagonists=[
            _person(
                "a fictional woman in her mid-twenties with shoulder-length dark hair and an ordinary face",
                [
                    ("outer", "simple black jacket"),
                    ("top", "white t-shirt"),
                    ("bottom", "dark jeans"),
                    ("feet", "black sneakers"),
                ],
                "肩までの黒髪に黒いジャケットの20代半ばの女性（架空の人物）",
            ),
            _person(
                "a fictional man in his mid-twenties with short dark hair and an ordinary face",
                [
                    ("outer", "simple black jacket"),
                    ("top", "grey t-shirt"),
                    ("bottom", "dark jeans"),
                    ("feet", "black sneakers"),
                ],
                "短い黒髪に黒いジャケットの20代半ばの男性（架空の人物）",
            ),
        ],
        locations=[{"en": "an open space that fits the setting", "ja": "設定に合った開けた場所"}],
    ),
}

PRESET_IDS: tuple[str, ...] = tuple(WORLD_PRESETS)
DEFAULT_PRESET_ID = "cinematic"


def get_preset(preset_id: str | None) -> WorldPreset:
    """The preset for ``preset_id``; unknown or empty ids fall back to ``cinematic``."""
    return WORLD_PRESETS.get(preset_id or "", WORLD_PRESETS[DEFAULT_PRESET_ID])


def preset_infos() -> list[dict[str, object]]:
    """``WorldPresetInfo`` list for ``GET /api/system/capabilities``."""
    return [
        {
            "id": p["id"],
            "name_ja": p["name_ja"],
            "name_en": p["name_en"],
            "description_ja": p["description_ja"],
            "palette": list(p["palette"]),
            "color_grades": list(p["color_grades"]),
            "lyric_styles": list(p["lyric_styles"]),
        }
        for p in WORLD_PRESETS.values()
    ]


def color_grade_infos() -> list[dict[str, str]]:
    """``[{id, name}]`` for the capabilities endpoint (name is Japanese)."""
    return [{"id": grade_id, "name": grade["name"]} for grade_id, grade in COLOR_GRADES.items()]


def lyric_style_infos() -> list[dict[str, str]]:
    """``[{id, name_ja, description}]`` for the capabilities endpoint."""
    return [
        {"id": style_id, "name_ja": style["name_ja"], "description": style["description"]}
        for style_id, style in LYRIC_STYLES.items()
    ]
