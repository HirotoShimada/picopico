"""World presets and creative vocabulary (ARCHITECTURE.md §6.3).

Everything here is static data shared by the storyboard generator
(:mod:`app.services.storyboard`), the batch planner
(:mod:`app.services.variation`), the renderer (``COLOR_GRADES``) and the
capabilities endpoint (``preset_infos`` / ``color_grade_infos`` /
``lyric_style_infos``).

Prompt vocabulary is English (video models are trained on English captions);
names and descriptions shown in the UI are Japanese.

Scene ``prompt_en`` strings are templates with two placeholders:
``{subject}`` (the world-bible protagonist description) and ``{place}`` (the
world-bible location). Scenes with ``has_subject=False`` are atmosphere
B-roll without people, which keeps a batch visually varied and is the safest
material for likeness rules.

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

from typing import Literal, NotRequired, TypedDict

__all__ = [
    "CAMERA_ANGLES",
    "CAMERA_MOVES",
    "COLOR_GRADES",
    "DEFAULT_PRESET_ID",
    "LYRIC_STYLES",
    "PRESET_IDS",
    "SCENE_TYPES",
    "SECTION_LABELS_JA",
    "WORLD_PRESETS",
    "CameraEntry",
    "ColorGrade",
    "LocalizedText",
    "LyricStyle",
    "SceneType",
    "WorldPreset",
    "color_grade_infos",
    "get_preset",
    "lyric_style_infos",
    "preset_infos",
]

Energy = Literal["low", "mid", "high"]


class LocalizedText(TypedDict):
    en: str
    ja: str


class SceneType(TypedDict):
    name_ja: str
    prompt_en: str  # template with {subject} / {place}
    desc_ja: str  # template with {subject} / {place} (Japanese)
    has_subject: bool
    energy: Energy


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
    look_en: str  # lighting + lens + film stock + atmosphere, comma separated
    lighting_en: str
    lens_en: str
    film_en: str
    atmosphere_en: str
    palette: list[str]  # 3 hex colours
    palette_en: str
    palette_ja: str
    scene_types: list[str]
    camera_angles: list[str]
    camera_moves: list[str]
    color_grades: list[str]
    lyric_styles: list[str]  # recommended lyric styles, best first
    negative_en: str
    protagonists: list[LocalizedText]
    locations: list[LocalizedText]


# --- scene vocabulary ----------------------------------------------------------------

SCENE_TYPES: dict[str, SceneType] = {
    "establishing_wide": {
        "name_ja": "情景ワイド",
        "prompt_en": (
            "wide establishing shot of {place}, {subject}, small in the frame, the environment telling the story"
        ),
        "desc_ja": "{place}を広く見せる情景カット。{subject}は小さく画面の中に",
        "has_subject": True,
        "energy": "low",
    },
    "walking": {
        "name_ja": "歩く",
        "prompt_en": "{subject}, walking slowly through {place}, clothes moving gently with each step",
        "desc_ja": "{subject}が{place}をゆっくり歩く",
        "has_subject": True,
        "energy": "mid",
    },
    "back_view": {
        "name_ja": "後ろ姿",
        "prompt_en": "{subject}, seen from behind, standing still and looking out over {place}",
        "desc_ja": "{place}を見つめる{subject}の後ろ姿",
        "has_subject": True,
        "energy": "low",
    },
    "close_portrait": {
        "name_ja": "表情のアップ",
        "prompt_en": (
            "intimate close-up of {subject}, eyes glancing away from the camera, a slow calm breath, "
            "{place} softly out of focus behind"
        ),
        "desc_ja": "{subject}の表情に寄る。背景の{place}はぼける",
        "has_subject": True,
        "energy": "low",
    },
    "profile_window": {
        "name_ja": "窓辺の横顔",
        "prompt_en": "{subject}, in profile beside a window, the light of {place} reflected faintly in the glass",
        "desc_ja": "窓辺に立つ{subject}の横顔。ガラスに{place}の光が映る",
        "has_subject": True,
        "energy": "low",
    },
    "turn_to_light": {
        "name_ja": "振り返り",
        "prompt_en": (
            "{subject}, slowly turning their head over the shoulder toward the light, a quiet expression, {place} "
            "around them"
        ),
        "desc_ja": "{place}で{subject}が光のほうへゆっくり振り返る",
        "has_subject": True,
        "energy": "mid",
    },
    "running": {
        "name_ja": "走る",
        "prompt_en": "{subject}, running forward through {place}, hair and clothes moving naturally in the wind",
        "desc_ja": "{subject}が{place}を駆け抜ける",
        "has_subject": True,
        "energy": "high",
    },
    "wind_hair": {
        "name_ja": "風を受ける",
        "prompt_en": "{subject}, standing in strong wind at {place}, hair and clothes streaming, eyes half closed",
        "desc_ja": "{place}で強い風を受けて立つ{subject}",
        "has_subject": True,
        "energy": "high",
    },
    "silhouette": {
        "name_ja": "シルエット",
        "prompt_en": (
            "backlit silhouette of {subject}, against the bright sky above {place}, rim light outlining the figure"
        ),
        "desc_ja": "{place}の空を背にした{subject}のシルエット",
        "has_subject": True,
        "energy": "mid",
    },
    "spin_sway": {
        "name_ja": "揺れる・回る",
        "prompt_en": "{subject}, swaying and slowly spinning with arms loose, lost in the music, in {place}",
        "desc_ja": "音楽に身を任せて揺れる{subject}（{place}）",
        "has_subject": True,
        "energy": "high",
    },
    "crowd_still": {
        "name_ja": "雑踏の中で",
        "prompt_en": (
            "{subject}, standing still while motion-blurred passers-by stream around them in {place}, "
            "the passers-by faceless and out of focus"
        ),
        "desc_ja": "{place}の雑踏の中で立ち止まる{subject}",
        "has_subject": True,
        "energy": "mid",
    },
    "train_window": {
        "name_ja": "車窓",
        "prompt_en": (
            "{subject}, sitting by the window of a moving train, passing lights sliding across the glass, {place} "
            "streaking past outside"
        ),
        "desc_ja": "走る電車の窓辺に座る{subject}。外を{place}が流れていく",
        "has_subject": True,
        "energy": "mid",
    },
    "rooftop": {
        "name_ja": "屋上",
        "prompt_en": "{subject}, standing at the edge of a rooftop above {place}, the horizon stretching far behind",
        "desc_ja": "{place}を見下ろす屋上に立つ{subject}",
        "has_subject": True,
        "energy": "mid",
    },
    "lying_down": {
        "name_ja": "寝そべる",
        "prompt_en": "{subject}, lying on their back looking up, {place} around them, eyes slowly closing",
        "desc_ja": "{place}で仰向けに寝そべり空を見上げる{subject}",
        "has_subject": True,
        "energy": "low",
    },
    "water_reflection": {
        "name_ja": "水面の反射",
        "prompt_en": "reflections rippling on water in {place}, the blurred reflection of {subject}, drifting through",
        "desc_ja": "{place}の水面に揺れる反射。{subject}の姿がにじむ",
        "has_subject": True,
        "energy": "low",
    },
    "ambient_detail": {
        "name_ja": "質感ディテール",
        "prompt_en": (
            "slow macro detail of {place}: light moving across textured surfaces, dust and particles drifting in the "
            "air, no people"
        ),
        "desc_ja": "{place}の質感に寄るマクロカット（人物なし）",
        "has_subject": False,
        "energy": "low",
    },
    "sky_drift": {
        "name_ja": "空",
        "prompt_en": "the sky above {place} slowly shifting, clouds drifting and the light changing, no people",
        "desc_ja": "{place}の空。雲が流れ光が移ろう（人物なし）",
        "has_subject": False,
        "energy": "low",
    },
    "light_abstract": {
        "name_ja": "光の抽象",
        "prompt_en": (
            "out-of-focus lights of {place} drifting across the frame, soft bokeh and gentle lens flares, no people"
        ),
        "desc_ja": "{place}の光がぼけて漂う抽象カット（人物なし）",
        "has_subject": False,
        "energy": "mid",
    },
    "nature_macro": {
        "name_ja": "自然のマクロ",
        "prompt_en": (
            "macro shot of leaves, grass and dew drops in {place} trembling in a light breeze, shallow depth of field, "
            "no people"
        ),
        "desc_ja": "{place}の葉や水滴に寄るマクロカット（人物なし）",
        "has_subject": False,
        "energy": "low",
    },
    "street_flow": {
        "name_ja": "街の流れ",
        "prompt_en": (
            "traffic light trails and reflections flowing through {place}, wet asphalt glistening, no pedestrians"
        ),
        "desc_ja": "{place}を流れる車のライトと反射（人物の特定なし）",
        "has_subject": False,
        "energy": "high",
    },
}

CAMERA_ANGLES: dict[str, CameraEntry] = {
    "eye_level": {"name_ja": "目線の高さ", "prompt_en": "eye-level camera"},
    "low_angle": {"name_ja": "ローアングル", "prompt_en": "low-angle camera looking up"},
    "high_angle": {"name_ja": "ハイアングル", "prompt_en": "high-angle camera looking down"},
    "overhead": {"name_ja": "真俯瞰", "prompt_en": "top-down overhead camera"},
    "over_shoulder": {"name_ja": "肩越し", "prompt_en": "over-the-shoulder framing"},
    "ground_level": {"name_ja": "地面すれすれ", "prompt_en": "camera placed just above the ground"},
    "through_foreground": {
        "name_ja": "前景越し",
        "prompt_en": "shot through soft out-of-focus foreground elements",
    },
    "dutch_tilt": {"name_ja": "ダッチアングル", "prompt_en": "slight dutch-angle tilt"},
}

CAMERA_MOVES: dict[str, CameraEntry] = {
    "static": {"name_ja": "フィックス", "prompt_en": "locked-off tripod shot with no camera movement", "energy": "low"},
    "slow_push_in": {"name_ja": "ゆっくり寄る", "prompt_en": "slow dolly push-in", "energy": "low"},
    "slow_pull_out": {
        "name_ja": "ゆっくり引く",
        "prompt_en": "slow dolly pull-out revealing the space",
        "energy": "low",
    },
    "pan_slow": {"name_ja": "ゆっくりパン", "prompt_en": "slow smooth horizontal pan", "energy": "low"},
    "tilt_up": {"name_ja": "ティルトアップ", "prompt_en": "slow tilt up", "energy": "low"},
    "rack_focus": {"name_ja": "ピント送り", "prompt_en": "static camera with a gentle rack focus", "energy": "low"},
    "handheld": {
        "name_ja": "手持ち",
        "prompt_en": "subtle handheld camera with natural micro-movements",
        "energy": "mid",
    },
    "tracking_side": {"name_ja": "横移動", "prompt_en": "smooth lateral tracking shot", "energy": "mid"},
    "orbit": {"name_ja": "回り込み", "prompt_en": "slow arc orbiting around the subject", "energy": "mid"},
    "crane_up": {"name_ja": "クレーンアップ", "prompt_en": "rising crane shot", "energy": "mid"},
    "follow_behind": {"name_ja": "追従", "prompt_en": "gimbal shot following closely behind", "energy": "high"},
    "drone_rise": {
        "name_ja": "ドローン上昇",
        "prompt_en": "aerial drone shot rising and moving forward",
        "energy": "high",
    },
    "fast_push": {"name_ja": "勢いよく寄る", "prompt_en": "energetic fast push-in", "energy": "high"},
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

# --- world presets ----------------------------------------------------------------------------

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
    protagonists: list[LocalizedText],
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
        description_ja="映画のような光と構図。浅い被写界深度と陰影で、1カットごとにドラマを感じさせる王道のルック。",
        lighting_en="motivated soft key light with deep contrasty shadows and warm practical lights",
        lens_en="35mm anamorphic lens, shallow depth of field, gentle oval bokeh",
        film_en="Kodak Vision3 500T film look with fine grain",
        atmosphere_en="light haze in the air catching the light",
        palette=["#0F2A3A", "#E39B55", "#1B1B1F"],
        palette_en="deep teal, warm amber and charcoal",
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
            "orbit",
            "crane_up",
            "follow_behind",
            "drone_rise",
            "rack_focus",
        ],
        color_grades=["teal_orange", "warm_film", "cool_night", "natural"],
        lyric_styles=["cinematic", "simple", "fade"],
        negative_en="flat lighting, amateur footage, harsh on-camera flash",
        protagonists=[
            {
                "en": (
                    "a fictional woman in her late twenties with shoulder-length dark hair, wearing a long charcoal "
                    "wool coat over a cream knit sweater"
                ),
                "ja": "肩までの黒髪にチャコールのロングコートを着た20代後半の女性（架空の人物）",
            },
            {
                "en": (
                    "a fictional man in his late twenties with short black hair and light stubble, wearing a dark navy "
                    "overcoat and a grey scarf"
                ),
                "ja": "短い黒髪に紺のオーバーコートとグレーのマフラーの20代後半の男性（架空の人物）",
            },
        ],
        locations=[
            {"en": "a rain-slicked city street at blue hour", "ja": "雨に濡れたブルーアワーの街路"},
            {"en": "an empty coastal highway at dusk", "ja": "夕暮れの誰もいない海沿いの道"},
            {"en": "a quiet train platform at night", "ja": "夜の静かな駅のホーム"},
        ],
    ),
    "emotional": _preset(
        id="emotional",
        name_ja="エモーショナル",
        name_en="Emotional",
        description_ja="柔らかな逆光と余韻のある間。感情に寄り添う親密なカットで、切なさや温かさを描く。",
        lighting_en="soft golden backlight with gentle lens flare and warm bounce light on the face",
        lens_en="50mm lens at f/1.4, very shallow depth of field, creamy bokeh",
        film_en="Kodak Portra 400 film look with soft fine grain",
        atmosphere_en="dust particles floating in the sunbeams, tender intimate mood",
        palette=["#F2C6A0", "#7A8FA6", "#FFF4E6"],
        palette_en="peach, dusty blue and soft cream",
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
            "crane_up",
        ],
        color_grades=["golden_hour", "warm_film", "faded_pastel", "natural"],
        lyric_styles=["fade", "simple", "minimal"],
        negative_en="harsh contrast, cold clinical light",
        protagonists=[
            {
                "en": (
                    "a fictional young woman in her early twenties with long wavy chestnut hair, wearing an oversized "
                    "white shirt and a thin silver necklace"
                ),
                "ja": "長い栗色のウェーブヘアに白いオーバーサイズシャツの20代前半の女性（架空の人物）",
            },
            {
                "en": (
                    "a fictional young man in his early twenties with tousled brown hair, wearing a faded denim jacket "
                    "over a white t-shirt"
                ),
                "ja": "くしゃっとした茶髪にデニムジャケットの20代前半の男性（架空の人物）",
            },
        ],
        locations=[
            {
                "en": "a sunlit bedroom with sheer curtains moving in the breeze",
                "ja": "薄いカーテンが揺れる陽だまりの部屋",
            },
            {"en": "a riverside path in late afternoon light", "ja": "午後遅くの光が差す川沿いの小道"},
            {"en": "a seaside town at golden hour", "ja": "ゴールデンアワーの海辺の町"},
        ],
    ),
    "urban_night": _preset(
        id="urban_night",
        name_ja="都会の夜",
        name_en="Urban Night",
        description_ja="ネオンと雨の反射、行き交う光。夜の街を歩く孤独とスピード感をスタイリッシュに。",
        lighting_en="mixed neon practical lighting in magenta and cyan, wet reflections, deep blacks",
        lens_en="35mm anamorphic lens with horizontal blue lens flares, shallow depth of field",
        film_en="CineStill 800T film look with halation around highlights",
        atmosphere_en="light rain and steam rising from the street, glistening asphalt",
        palette=["#0B0F2B", "#FF2E88", "#2EE6FF"],
        palette_en="midnight navy, neon magenta and electric cyan",
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
            "drone_rise",
            "fast_push",
            "pan_slow",
        ],
        color_grades=["neon_night", "cool_night", "teal_orange", "vivid"],
        lyric_styles=["simple", "karaoke", "cinematic"],
        negative_en="daylight, readable signage, legible neon words, brand logos",
        protagonists=[
            {
                "en": (
                    "a fictional woman in her mid-twenties with a sleek black bob haircut, wearing a black leather "
                    "jacket and silver hoop earrings"
                ),
                "ja": "黒髪のボブに黒のレザージャケットとシルバーのフープピアスの20代半ばの女性（架空の人物）",
            },
            {
                "en": (
                    "a fictional man in his mid-twenties with short bleached hair, wearing a black hooded jacket and a "
                    "silver chain"
                ),
                "ja": "短いブリーチヘアに黒のフードジャケットとシルバーチェーンの20代半ばの男性（架空の人物）",
            },
        ],
        locations=[
            {"en": "a neon-lit downtown street in the rain at night", "ja": "雨の夜のネオン街"},
            {"en": "a narrow alley glowing with unreadable neon signs", "ja": "読めないネオンが光る細い路地"},
            {"en": "an elevated train line crossing a glittering city at night", "ja": "夜景を横切る高架線"},
        ],
    ),
    "nostalgic": _preset(
        id="nostalgic",
        name_ja="ノスタルジック",
        name_en="Nostalgic",
        description_ja="16mmフィルムの粒子と褪せた色。夏休みの記憶のような、懐かしく少し切ない情景。",
        lighting_en="warm hazy late-afternoon sunlight with soft halation",
        lens_en="vintage 16mm lens with soft edges and gentle vignetting",
        film_en="16mm Kodak Ektachrome film look with visible grain and slightly faded colors",
        atmosphere_en="lazy summer air, cicada-season warmth, a memory-like quality",
        palette=["#E8C07D", "#6E9C9F", "#C96F53"],
        palette_en="faded mustard, sea-glass green and terracotta",
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
            {
                "en": (
                    "a fictional young woman in her early twenties with a low ponytail, wearing a pale yellow sundress "
                    "and white canvas sneakers"
                ),
                "ja": "低いポニーテールに淡い黄色のワンピースと白いスニーカーの20代前半の女性（架空の人物）",
            },
            {
                "en": (
                    "a fictional young man in his early twenties with short messy hair, wearing a loose white short-"
                    "sleeved shirt and khaki shorts"
                ),
                "ja": "短いくせ毛に白い半袖シャツとカーキの短パンの20代前半の男性（架空の人物）",
            },
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
        lighting_en="natural sunlight, soft morning light and golden hour glow",
        lens_en="24mm wide-angle lens with natural perspective and deep focus",
        film_en="clean high-dynamic-range digital cinema look with natural colors",
        atmosphere_en="mist drifting over the landscape, wind moving through grass and trees",
        palette=["#2F5D3A", "#A8C5DA", "#E9DFC4"],
        palette_en="forest green, sky blue and sand beige",
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
            "slow_push_in",
            "slow_pull_out",
            "drone_rise",
            "crane_up",
            "tracking_side",
            "orbit",
            "tilt_up",
        ],
        color_grades=["natural", "forest_moody", "golden_hour", "vivid"],
        lyric_styles=["minimal", "simple", "fade"],
        negative_en="buildings, cars, litter, power lines",
        protagonists=[
            {
                "en": (
                    "a fictional woman in her mid-twenties with long dark braided hair, wearing a loose olive linen "
                    "dress"
                ),
                "ja": "長い黒髪の三つ編みにオリーブ色のリネンワンピースの20代半ばの女性（架空の人物）",
            },
            {
                "en": "a fictional man in his mid-twenties with shoulder-length dark hair, wearing a beige linen shirt",
                "ja": "肩までの黒髪にベージュのリネンシャツの20代半ばの男性（架空の人物）",
            },
        ],
        locations=[
            {"en": "a misty mountain meadow at sunrise", "ja": "朝霧の高原"},
            {"en": "a windswept coastal cliff above the ocean", "ja": "海を見下ろす風の強い崖"},
            {"en": "a sunlit old-growth forest", "ja": "木漏れ日の原生林"},
        ],
    ),
    "dark": _preset(
        id="dark",
        name_ja="ダーク",
        name_en="Dark",
        description_ja="低いキー、硬い影、霧。緊張感とミステリアスな空気をまとう、陰影の強い世界。",
        lighting_en="low-key lighting with a single hard side light and deep crushed shadows",
        lens_en="40mm anamorphic lens, shallow depth of field",
        film_en="desaturated film look with heavy grain",
        atmosphere_en="thick fog and smoke drifting through shafts of cold light",
        palette=["#0A0A0C", "#4A5560", "#8C1C13"],
        palette_en="near-black, cold slate grey and deep crimson",
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
            {
                "en": (
                    "a fictional woman in her late twenties with slicked-back dark hair, wearing a black high-neck coat"
                ),
                "ja": "オールバックの黒髪に黒のハイネックコートの20代後半の女性（架空の人物）",
            },
            {
                "en": "a fictional man in his late twenties with dark wet-look hair, wearing a black trench coat",
                "ja": "濡れたような黒髪に黒のトレンチコートの20代後半の男性（架空の人物）",
            },
        ],
        locations=[
            {"en": "an abandoned concrete warehouse filled with fog", "ja": "霧が立ち込める廃倉庫"},
            {"en": "a dark forest road at night lit by a single streetlight", "ja": "街灯ひとつの夜の森の道"},
            {"en": "a deserted underground parking garage", "ja": "人のいない地下駐車場"},
        ],
    ),
    "dreamy": _preset(
        id="dreamy",
        name_ja="ドリーミー",
        name_en="Dreamy",
        description_ja="霞がかった光、パステル、スローモーション。現実と夢のあいだのような幻想的な世界。",
        lighting_en="soft diffused glowing light with blooming highlights",
        lens_en="85mm lens with a soft-focus diffusion filter, dreamy bokeh",
        film_en="pastel film look with lifted blacks",
        atmosphere_en="floating petals and light particles, gentle slow-motion feeling",
        palette=["#F6C1D9", "#B9C7F2", "#FFFFFF"],
        palette_en="blush pink, periwinkle and pearl white",
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
        camera_moves=["static", "slow_push_in", "slow_pull_out", "orbit", "crane_up", "rack_focus", "tracking_side"],
        color_grades=["lavender_haze", "faded_pastel", "golden_hour", "natural"],
        lyric_styles=["fade", "minimal", "simple"],
        negative_en="harsh shadows, gritty textures, dark horror mood",
        protagonists=[
            {
                "en": (
                    "a fictional young woman in her early twenties with long platinum-blonde hair, wearing a flowing "
                    "pale lilac dress"
                ),
                "ja": "長いプラチナブロンドに淡いライラックのドレスの20代前半の女性（架空の人物）",
            },
            {
                "en": (
                    "a fictional young man in his early twenties with soft silver-grey hair, wearing a loose white "
                    "linen shirt"
                ),
                "ja": "柔らかなシルバーグレーの髪に白いリネンシャツの20代前半の男性（架空の人物）",
            },
        ],
        locations=[
            {"en": "a field of white flowers under a pastel sky", "ja": "パステルの空の下の白い花畑"},
            {"en": "a shallow mirror-like lake at dawn", "ja": "夜明けの鏡のような浅い湖"},
            {"en": "a sunlit greenhouse full of hanging plants", "ja": "植物が吊るされた光あふれる温室"},
        ],
    ),
    "documentary": _preset(
        id="documentary",
        name_ja="ドキュメンタリー",
        name_en="Documentary",
        description_ja="手持ちカメラと自然光。飾らない日常の断片を切り取る、リアルで温度のある映像。",
        lighting_en="available natural light and real practical lights, unpolished but beautiful",
        lens_en="28mm prime lens, handheld, moderate depth of field",
        film_en="Fujifilm Eterna-like muted film look with natural grain",
        atmosphere_en="candid everyday moments, authentic textures of real life",
        palette=["#5B6770", "#C9B79C", "#2E3B2F"],
        palette_en="muted slate, warm beige and dark olive",
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
            {
                "en": (
                    "a fictional woman in her early thirties with short tousled hair, wearing a grey hoodie and a "
                    "canvas tote bag"
                ),
                "ja": "短いくしゃっとした髪にグレーのパーカーとキャンバストートの30代前半の女性（架空の人物）",
            },
            {
                "en": (
                    "a fictional man in his early thirties with a short beard, wearing a worn olive work jacket and a "
                    "knit beanie"
                ),
                "ja": "短いひげにオリーブのワークジャケットとニット帽の30代前半の男性（架空の人物）",
            },
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
        lighting_en="natural cinematic lighting with soft contrast",
        lens_en="35mm lens, shallow depth of field",
        film_en="subtle film grain, true-to-life colors",
        atmosphere_en="atmospheric depth",
        palette=["#2B2D42", "#D9A066", "#EDF2F4"],
        palette_en="ink blue, warm ochre and off-white",
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
            {
                "en": (
                    "a fictional woman in her mid-twenties with shoulder-length dark hair, wearing a simple black "
                    "jacket"
                ),
                "ja": "肩までの黒髪に黒いジャケットの20代半ばの女性（架空の人物）",
            },
            {
                "en": "a fictional man in his mid-twenties with short dark hair, wearing a simple black jacket",
                "ja": "短い黒髪に黒いジャケットの20代半ばの男性（架空の人物）",
            },
        ],
        locations=[{"en": "an atmospheric open space", "ja": "空気感のある開けた場所"}],
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
