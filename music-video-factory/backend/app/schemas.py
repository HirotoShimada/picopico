"""Pydantic v2 request/response models for the whole HTTP API (ARCHITECTURE.md §5).

Request models ignore unknown fields; enum-like fields use ``Literal`` so
invalid values produce 422. Nested JSON documents produced by services
(AudioAnalysis, Storyboard, Estimate, VideoPlan, QualityReport) are typed
loosely (``dict``) in responses so service-side additions never break
serialisation; typed models for them are provided for clients/tests.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --- enumerations -------------------------------------------------------------------

AnalysisStatusT = Literal["none", "queued", "running", "done", "failed"]
LyricsFormatT = Literal["txt", "lrc", "srt"]
LyricsFormatInT = Literal["auto", "txt", "lrc", "srt"]
LyricSourceT = Literal["manual", "lrc", "srt", "txt", "heuristic", "whisper"]
AssetKindT = Literal["audio", "lyrics", "material", "clip", "render", "thumbnail", "zip"]
ClipSourceT = Literal["upload", "mock", "kling", "runway", "veo", "openrouter"]
ClipStatusT = Literal["pending", "generating", "ready", "failed", "cancelled"]
BatchStatusT = Literal[
    "queued", "planning", "generating_clips", "rendering",
    "completed", "partial", "failed", "cancelled", "budget_exceeded",
]  # fmt: skip
VideoStatusT = Literal["queued", "waiting_clips", "rendering", "done", "failed", "cancelled"]
PresetIdT = Literal[
    "cinematic", "emotional", "urban_night", "nostalgic", "nature", "dark", "dreamy", "documentary", "custom"
]
ProviderIdT = Literal["mock", "library", "kling", "runway", "veo", "openrouter"]
LyricStyleT = Literal["simple", "cinematic", "minimal", "karaoke", "fade"]
StartStrategyT = Literal["segment", "chorus", "varied", "custom"]
ReuseModeT = Literal["max_variety", "balanced", "economy"]
TransitionT = Literal["cut", "crossfade"]
PanT = Literal["left", "right", "up", "down", "center"]
FontWeightT = Literal["bold", "regular", "light"]
TextPositionT = Literal["bottom", "center", "top"]
AutoTimeMethodT = Literal["heuristic", "whisper"]
ProviderKindT = Literal["mock", "library", "api"]

# §9 rights checklist: key -> Japanese label. All are required before batch creation.
RIGHTS_ITEMS: dict[str, str] = {
    "audio_rights": "音源の権利を保有、または利用許諾を得ている",
    "lyrics_rights": "歌詞の権利を保有、または利用許諾を得ている",
    "no_real_person_likeness": "実在の人物（アーティスト本人を含む）の肖像を生成しない",
    "model_terms_checked": "使用する動画生成モデルの利用規約・商用利用条件を確認した",
    "platform_terms": "投稿先（Instagram 等）の規約を確認した",
}
REQUIRED_RIGHTS_KEYS: tuple[str, ...] = tuple(RIGHTS_ITEMS)

DURATION_CHOICES: tuple[int, ...] = (15, 30, 60)
COUNT_CHOICES: tuple[int, ...] = (1, 3, 5, 10, 30)
MIN_DURATION_SEC = 5
MAX_DURATION_SEC = 90
MAX_COUNT = 50
MIN_SEGMENT_SEC = 3.0
MAX_LYRIC_LINE_CHARS = 500

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_ID_RE = r"^[a-z0-9_]{1,40}$"


def normalize_hex_color(value: str) -> str:
    """``#abc`` / ``#aabbcc`` → ``#AABBCC``; raises ``ValueError`` otherwise."""
    value = value.strip()
    if not _HEX_RE.match(value):
        raise ValueError("色は #RRGGBB 形式で指定してください")
    digits = value[1:]
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return f"#{digits.upper()}"


def _strip_or_none(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


MAX_ARTIST_ALIASES = 10


def _clean_aliases(value: Any) -> Any:
    """Artist aliases: stripped, blanks dropped, de-duplicated (case-insensitive), order kept."""
    if value is None or not isinstance(value, list):
        return value
    seen: set[str] = set()
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return value  # let the type check report it
        text = " ".join(item.split())
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            out.append(text)
    return out


class APIModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class LooseModel(BaseModel):
    """For service-produced documents: keeps unknown keys."""

    model_config = ConfigDict(extra="allow", from_attributes=True)


# --- shared ---------------------------------------------------------------------------


class Segment(APIModel):
    start: float
    end: float


class AssetOut(APIModel):
    id: str
    kind: AssetKindT
    filename: str
    content_type: str
    size_bytes: int
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    url: str
    created_at: datetime


# --- lyrics -------------------------------------------------------------------------------


class WordTiming(APIModel):
    text: str
    start: float | None = None
    end: float | None = None


class LyricLine(APIModel):
    """§6.2. Times are absolute seconds in the song."""

    id: str = Field(min_length=1, max_length=64)
    text: str
    start: float | None = Field(default=None, ge=0)
    end: float | None = Field(default=None, ge=0)
    words: list[WordTiming] | None = None
    source: LyricSourceT = "manual"
    confidence: float | None = Field(default=None, ge=0, le=1)


class LyricsUpdate(BaseModel):
    text: str = Field(max_length=100_000)
    format: LyricsFormatInT = "auto"


class LyricsParseOut(APIModel):
    format: LyricsFormatT
    lines: list[LyricLine]
    warnings: list[str] = Field(default_factory=list)


class LyricsOut(APIModel):
    text: str
    format: LyricsFormatT
    lines: list[LyricLine]


class LyricLinesUpdate(BaseModel):
    lines: list[LyricLine] = Field(max_length=2000)

    @model_validator(mode="after")
    def _check_lines(self) -> LyricLinesUpdate:
        seen: set[str] = set()
        for line in self.lines:
            if line.id in seen:
                raise ValueError(f"歌詞行 ID が重複しています: {line.id}")
            seen.add(line.id)
            if len(line.text) > MAX_LYRIC_LINE_CHARS:
                raise ValueError(f"歌詞行 {line.id} が長すぎます（{MAX_LYRIC_LINE_CHARS}文字以内）")
            if line.start is not None and line.end is not None and line.end < line.start:
                raise ValueError(f"歌詞行 {line.id} の終了時刻が開始時刻より前です")
        return self


class LyricLinesOut(APIModel):
    lines: list[LyricLine]
    warnings: list[str] = Field(default_factory=list)  # e.g. lyrics_rights reset after a text change


class AutoTimeRequest(BaseModel):
    method: AutoTimeMethodT = "heuristic"
    start: float | None = Field(default=None, ge=0)
    end: float | None = Field(default=None, gt=0)
    overwrite_timed: bool = False

    @model_validator(mode="after")
    def _check_range(self) -> AutoTimeRequest:
        if self.start is not None and self.end is not None and self.end <= self.start:
            raise ValueError("終了時刻は開始時刻より後にしてください")
        return self


class AutoTimeOut(APIModel):
    lines: list[LyricLine]
    method_used: AutoTimeMethodT
    warnings: list[str] = Field(default_factory=list)


# --- projects ---------------------------------------------------------------------------------


class ProjectCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    artist_name: str = Field(min_length=1, max_length=200)
    artist_aliases: list[Annotated[str, Field(max_length=100)]] = Field(default_factory=list, max_length=MAX_ARTIST_ALIASES)
    mood: str | None = Field(default=None, max_length=2000)
    purpose: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=10_000)
    world_preset: PresetIdT = "cinematic"
    custom_world: str | None = Field(default=None, max_length=4000)
    budget_limit_usd: float | None = Field(default=None, ge=0, le=100_000)

    @field_validator("title", "artist_name", mode="before")
    @classmethod
    def _strip_required(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("mood", "purpose", "notes", "custom_world", mode="before")
    @classmethod
    def _strip_optional(cls, value: Any) -> Any:
        return _strip_or_none(value)

    @field_validator("artist_aliases", mode="before")
    @classmethod
    def _aliases(cls, value: Any) -> Any:
        return _clean_aliases(value)


class ProjectUpdate(BaseModel):
    """Partial update: only fields present in the request body are applied."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    artist_name: str | None = Field(default=None, min_length=1, max_length=200)
    artist_aliases: list[Annotated[str, Field(max_length=100)]] | None = Field(default=None, max_length=MAX_ARTIST_ALIASES)
    mood: str | None = Field(default=None, max_length=2000)
    purpose: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=10_000)
    world_preset: PresetIdT | None = None
    custom_world: str | None = Field(default=None, max_length=4000)
    budget_limit_usd: float | None = Field(default=None, ge=0, le=100_000)

    @field_validator("title", "artist_name", mode="before")
    @classmethod
    def _strip_required(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("mood", "purpose", "notes", "custom_world", mode="before")
    @classmethod
    def _strip_optional(cls, value: Any) -> Any:
        return _strip_or_none(value)

    @field_validator("artist_aliases", mode="before")
    @classmethod
    def _aliases(cls, value: Any) -> Any:
        return _clean_aliases(value)

    @model_validator(mode="after")
    def _non_nullable(self) -> ProjectUpdate:
        for name in ("title", "artist_name", "world_preset", "artist_aliases"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} は null にできません")
        return self


class ProjectCounts(APIModel):
    clips: int = 0
    videos: int = 0
    batches: int = 0


class ProjectOut(APIModel):
    id: str
    title: str
    artist_name: str
    artist_aliases: list[str] = Field(default_factory=list)
    mood: str | None = None
    purpose: str | None = None
    notes: str | None = None
    audio_asset_id: str | None = None
    audio: AssetOut | None = None
    analysis: dict[str, Any] | None = None
    analysis_status: AnalysisStatusT
    analysis_error: str | None = None
    lyrics_text: str = ""
    lyrics_format: LyricsFormatT = "txt"
    lyric_lines: list[LyricLine] = Field(default_factory=list)
    segment_start: float | None = None
    segment_end: float | None = None
    world_preset: str
    custom_world: str | None = None
    rights: dict[str, bool] = Field(default_factory=dict)
    rights_confirmed_at: datetime | None = None
    budget_limit_usd: float | None = None
    created_at: datetime
    updated_at: datetime
    counts: ProjectCounts
    # Notices about this request, e.g. the audio upload un-confirming ``audio_rights`` (empty otherwise).
    warnings: list[str] = Field(default_factory=list)


class ProjectSummary(APIModel):
    id: str
    title: str
    artist_name: str
    created_at: datetime
    updated_at: datetime
    has_audio: bool
    analysis_status: AnalysisStatusT
    lyric_line_count: int
    video_count: int
    cover_url: str | None = None


class RightsUpdate(BaseModel):
    items: dict[str, bool]

    @field_validator("items")
    @classmethod
    def _known_keys(cls, items: dict[str, bool]) -> dict[str, bool]:
        unknown = sorted(set(items) - set(RIGHTS_ITEMS))
        if unknown:
            raise ValueError(f"不明な権利確認項目です: {', '.join(unknown)}")
        return items


# --- audio analysis -----------------------------------------------------------------------------


class AnalysisOut(APIModel):
    status: AnalysisStatusT
    error: str | None = None
    analysis: dict[str, Any] | None = None
    segment: Segment | None = None


class WorldEstimateSources(APIModel):
    audio: list[str] = Field(default_factory=list)
    lyrics: list[str] = Field(default_factory=list)
    user: list[str] = Field(default_factory=list)


class WorldEstimateOut(APIModel):
    """感情・世界観推定 (``storyboard.estimate_world``): deterministic, no API call."""

    emotions: list[str] = Field(default_factory=list)
    themes: list[str] = Field(default_factory=list)
    mood_en: str = ""
    recommended_presets: list[str] = Field(default_factory=list)
    sources: WorldEstimateSources = Field(default_factory=WorldEstimateSources)
    rationale_ja: str = ""
    analysis_used: bool = False  # false while the song analysis is not done (lyrics/user input only)


class AnalysisRerunOut(APIModel):
    status: AnalysisStatusT


class SegmentUpdate(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)

    @model_validator(mode="after")
    def _min_length(self) -> SegmentUpdate:
        if self.end - self.start < MIN_SEGMENT_SEC:
            raise ValueError(f"区間は{MIN_SEGMENT_SEC:g}秒以上にしてください")
        return self


class SegmentOut(APIModel):
    segment: Segment


# --- clips -----------------------------------------------------------------------------------------


class ClipOut(APIModel):
    id: str
    source: ClipSourceT
    provider: str
    model: str | None = None
    scene_id: str | None = None
    prompt: str | None = None
    status: ClipStatusT
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    cost_usd: float = 0.0
    error: str | None = None
    url: str | None = None
    thumbnail_url: str | None = None
    created_at: datetime
    used_in_videos: int = 0


# --- rendering / text style ------------------------------------------------------------------------------


class TextStyle(BaseModel):
    """§6.6. All fields optional overrides of the lyric style's defaults."""

    font_family: str | None = Field(default=None, min_length=1, max_length=64)
    font_weight: FontWeightT | None = None
    font_size: int | None = Field(default=None, ge=16, le=200)
    primary_color: str | None = None
    highlight_color: str | None = None
    outline_color: str | None = None
    outline_width: float | None = Field(default=None, ge=0, le=20)
    shadow: float | None = Field(default=None, ge=0, le=10)
    position: TextPositionT | None = None
    bg_opacity: float | None = Field(default=None, ge=0, le=1)
    letter_spacing: float | None = Field(default=None, ge=-10, le=50)
    max_chars_per_line: int | None = Field(default=None, ge=4, le=40)

    @field_validator("primary_color", "highlight_color", "outline_color")
    @classmethod
    def _hex(cls, value: str | None) -> str | None:
        return normalize_hex_color(value) if value is not None else None

    @field_validator("font_family")
    @classmethod
    def _font_family(cls, value: str | None) -> str | None:
        # The family name is written into an ASS style line; reject override/separator chars.
        if value is not None and re.search(r"[,{}\\\r\n\x00-\x1f]", value):
            raise ValueError("フォント名に使用できない文字が含まれています")
        return value.strip() if value is not None else None


class GenerationSettings(BaseModel):
    """§5.6 batch settings (also the estimate request body)."""

    preset: PresetIdT = "cinematic"
    custom_world: str | None = Field(default=None, max_length=4000)
    provider: ProviderIdT = "mock"
    model: str | None = Field(default=None, max_length=100)
    duration_sec: float = Field(default=15, ge=MIN_DURATION_SEC, le=MAX_DURATION_SEC)
    count: int = Field(default=3, ge=1, le=MAX_COUNT)
    # "auto" (extension): rotate the preset's recommended lyric styles across the videos.
    lyric_style: LyricStyleT | Literal["auto"] = "simple"
    text_style: TextStyle | None = None
    color_grade: str = Field(default="auto", pattern=_ID_RE)
    start_strategy: StartStrategyT = "segment"
    start_sec: float | None = Field(default=None, ge=0)
    reuse: ReuseModeT = "balanced"
    max_new_clips: int | None = Field(default=None, ge=0, le=500)
    budget_limit_usd: float | None = Field(default=None, ge=0, le=100_000)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    # Extension: with Claude, later batches reuse the project's storyboard for the same world
    # (consistent protagonist, clip cache hits); true asks Claude for a new one.
    new_storyboard: bool = False
    # Horizontal flips mirror any text/signage in the footage, so the planner flips shots only when allowed.
    allow_mirror: bool = Field(
        default=False,
        description="左右反転を許可する（素材内の文字や看板が鏡文字になるため既定ではオフ）",
    )

    @field_validator("custom_world", "model", mode="before")
    @classmethod
    def _strip_optional(cls, value: Any) -> Any:
        return _strip_or_none(value)

    @model_validator(mode="after")
    def _check(self) -> GenerationSettings:
        if self.start_strategy == "custom" and self.start_sec is None:
            raise ValueError("開始位置を「カスタム」にする場合は start_sec を指定してください")
        return self


class StoryboardRequest(BaseModel):
    preset: PresetIdT = "cinematic"
    custom_world: str | None = Field(default=None, max_length=4000)
    scenes: int | None = Field(default=None, ge=1, le=30)

    @field_validator("custom_world", mode="before")
    @classmethod
    def _strip_optional(cls, value: Any) -> Any:
        return _strip_or_none(value)


class WorldBible(LooseModel):
    summary_ja: str = ""
    look_en: str = ""
    protagonist_en: str = ""
    location_en: str = ""
    palette: list[str] = Field(default_factory=list)
    consistency_tokens_en: str = ""


class StoryboardScene(LooseModel):
    id: str
    section: str | None = None
    lyric_ref: str | None = None
    scene_type: str | None = None
    camera_angle: str | None = None
    camera_move: str | None = None
    description_ja: str = ""
    prompt_en: str = ""
    negative_prompt_en: str = ""
    duration_hint: float | None = None


class Storyboard(LooseModel):
    """§6.3 Storyboard document."""

    source: Literal["claude", "template"]
    # Extension: how Claude was reached when it was called (also for a template fallback after a call).
    route: Literal["anthropic", "openrouter"] | None = None
    model: str | None = None
    usage: dict[str, int] | None = None
    cost_usd: float = 0.0
    world_bible: WorldBible
    scenes: list[StoryboardScene]
    warnings: list[str] = Field(default_factory=list)


class EstimateComparison(LooseModel):
    provider: str
    model: str
    total_usd: float
    configured: bool


class EstimateBudgetFit(LooseModel):
    """Extension: the ``max_new_clips`` cap that brings an over-budget batch within the budget,
    priced from a real re-plan with the same seed (applying it reproduces this estimate)."""

    max_new_clips: int
    new_clips: int
    total_usd: float
    shots_per_video: int
    beat_shots_per_video: int | None = None
    max_shot_sec: float | None = None
    min_speed: float | None = None
    repeat_shots: int = 0
    note: str


class Estimate(LooseModel):
    """§6.5 Estimate document."""

    provider: str
    model: str
    clip_duration: float
    videos: int
    shots_total: int
    unique_clips: int
    new_clips: int
    cached_clips: int
    price_per_second_usd: float
    video_generation_usd: float
    llm_usd: float
    total_usd: float
    naive_total_usd: float
    savings_usd: float
    budget_limit_usd: float | None = None
    within_budget: bool
    variety_score: float | None = None
    variety_warning: str | None = None
    comparison: list[EstimateComparison] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    # Extension (§6.4 plan stats): shots per video after fitting cuts to the clip pool, and in-video repeats.
    shots_per_video_min: int | None = None
    shots_per_video_max: int | None = None
    beat_shots_per_video_max: int | None = None
    fitted_videos: int | None = None
    max_shot_sec: float | None = None
    min_speed: float | None = None
    repeat_shots: int | None = None
    overlap_repeat_shots: int | None = None
    adjacent_repeat_shots: int | None = None
    scene_repeat_shots: int | None = None
    videos_with_repeats: int | None = None
    repeat_warning: str | None = None
    budget_fit: EstimateBudgetFit | None = None


# --- batches & videos ------------------------------------------------------------------------------------


class ShotPlan(LooseModel):
    clip_key: str | None = None
    clip_id: str | None = None
    src_in: float = 0.0
    duration: float
    hflip: bool = False
    zoom_from: float = 1.0
    zoom_to: float = 1.06
    pan: PanT = "center"
    speed: float = 1.0


class VideoPlan(LooseModel):
    """§6.4 VideoPlan (stored per Video)."""

    index: int
    title: str = ""
    seed: int | None = None
    start: float
    duration: float
    lyric_style: str
    text_style: dict[str, Any] = Field(default_factory=dict)
    color_grade: str
    shots: list[ShotPlan]
    transition: TransitionT = "cut"


class QualityCheck(LooseModel):
    name: str
    passed: bool
    value: Any = None
    expected: Any = None


class QualityReport(LooseModel):
    passed: bool
    checks: list[QualityCheck] = Field(default_factory=list)
    loudness_lufs: float | None = None
    black_ratio: float | None = None


class VideoOut(APIModel):
    id: str
    project_id: str
    batch_id: str
    index: int
    title: str
    status: VideoStatusT
    progress: float = 0.0
    error: str | None = None
    favorite: bool = False
    url: str | None = None
    thumbnail_url: str | None = None
    duration: float | None = None
    quality: dict[str, Any] | None = None
    plan: dict[str, Any] = Field(default_factory=dict)
    render_version: int = 1
    created_at: datetime
    rendered_at: datetime | None = None


class VideoDetailOut(VideoOut):
    lyric_lines: list[LyricLine] = Field(default_factory=list)
    available_clips: list[ClipOut] = Field(default_factory=list)


class VideoUpdate(BaseModel):
    favorite: bool | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)


class VideoRegenerateRequest(BaseModel):
    new_clips: bool = False


class BatchUpdate(BaseModel):
    """``PATCH /api/batches/{id}`` (extension): a new budget cap, e.g. to retry after ``budget_exceeded``."""

    budget_limit_usd: float = Field(ge=0, le=100_000)


class LyricOffset(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)

    @model_validator(mode="after")
    def _order(self) -> LyricOffset:
        if self.end <= self.start:
            raise ValueError("終了時刻は開始時刻より後にしてください")
        return self


class ShotEdit(BaseModel):
    index: int = Field(ge=0)
    clip_id: str = Field(min_length=1, max_length=32)
    src_in: float | None = Field(default=None, ge=0)


class VideoEdit(BaseModel):
    """§5.7 manual edit overrides; omitted fields keep the current plan."""

    start: float | None = Field(default=None, ge=0)
    lyric_style: LyricStyleT | None = None
    text_style: TextStyle | None = None
    color_grade: str | None = Field(default=None, pattern=_ID_RE)
    lyric_offsets: dict[str, LyricOffset] | None = None
    shots: list[ShotEdit] | None = Field(default=None, max_length=200)

    @field_validator("shots")
    @classmethod
    def _unique_shots(cls, shots: list[ShotEdit] | None) -> list[ShotEdit] | None:
        if shots is not None and len({s.index for s in shots}) != len(shots):
            raise ValueError("同じショット番号が複数指定されています")
        return shots


class VideoZipRequest(BaseModel):
    video_ids: list[str] = Field(min_length=1, max_length=500)


class ZipLinkOut(APIModel):
    """Signed ``GET /media-zip/…`` URL (no auth header needed) that streams the ZIP; valid until ``expires_at``."""

    url: str
    expires_at: datetime


class BatchCounts(APIModel):
    total: int = 0
    done: int = 0
    rendering: int = 0
    queued: int = 0
    failed: int = 0
    cancelled: int = 0


class ClipCounts(APIModel):
    total: int = 0
    ready: int = 0
    generating: int = 0
    failed: int = 0
    reused: int = 0


class BatchOut(APIModel):
    id: str
    project_id: str
    status: BatchStatusT
    settings: dict[str, Any]
    estimate: dict[str, Any] | None = None
    storyboard: dict[str, Any] | None = None
    error: str | None = None
    cost_usd: float = 0.0
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    counts: BatchCounts
    clip_counts: ClipCounts
    # While a finished batch is re-opened for a re-render: the status and reason it had before.
    reopened_from: BatchStatusT | None = None
    previous_error: str | None = None
    videos: list[VideoOut] | None = None
    clips: list[ClipOut] | None = None


# --- system & dashboard ------------------------------------------------------------------------------------


class HealthOut(APIModel):
    status: Literal["ok", "degraded"]
    db: bool
    redis: bool
    ffmpeg: bool
    task_mode: str
    storage_backend: str


class ProviderModelInfo(LooseModel):
    id: str
    name: str
    clip_durations: list[float]
    max_clip_duration: float
    resolution: str
    supports_vertical: bool
    # null when the price is unknown (OpenRouter listing entry without a readable price; see price_known)
    price_per_second_usd: float | None
    price_note: str = ""
    verified: bool = False
    price_known: bool = True  # extension: False = not usable (the budget cannot be reserved)


class ProviderInfo(LooseModel):
    id: str
    name: str
    configured: bool
    available: bool
    reason: str | None = None
    kind: ProviderKindT
    commercial_use_note: str = ""
    models: list[ProviderModelInfo] = Field(default_factory=list)


class WhisperInfo(APIModel):
    available: bool
    reason: str | None = None


class LlmInfo(APIModel):
    """Extension: how Claude is reached for storyboards/translation (no secrets)."""

    route: Literal["anthropic", "openrouter"] | None = None  # None = template only
    model: str | None = None
    data_policy_note: str = ""
    verified: bool = False


class WorldPresetInfo(LooseModel):
    id: str
    name_ja: str
    name_en: str = ""
    description_ja: str = ""
    palette: list[str] = Field(default_factory=list)
    color_grades: list[str] = Field(default_factory=list)  # grades ``color_grade: "auto"`` rotates
    lyric_styles: list[str] = Field(default_factory=list)  # recommended, best first (``lyric_style: "auto"``)


class LyricStyleInfo(APIModel):
    id: str
    name_ja: str
    description: str = ""


class ColorGradeInfo(APIModel):
    id: str
    name: str


class FontInfo(APIModel):
    id: str
    family: str
    label: str


class LimitsInfo(APIModel):
    max_audio_mb: int
    max_video_mb: int
    max_count: int = MAX_COUNT
    min_duration: int = MIN_DURATION_SEC
    max_duration: int = MAX_DURATION_SEC


class GenerationOptionInfo(APIModel):
    """A boolean ``GenerationSettings`` option the generate form can offer."""

    id: str
    name_ja: str
    description_ja: str = ""
    default: bool = False


# Boolean GenerationSettings options listed by GET /api/system/capabilities (ids = field names).
GENERATION_OPTIONS: tuple[GenerationOptionInfo, ...] = (
    GenerationOptionInfo(
        id="allow_mirror",
        name_ja="左右反転を許可",
        description_ja="ショットを左右反転して動画ごとの変化を増やします。素材に映る文字や看板も鏡文字になるため、"
        "既定ではオフです。",
        default=False,
    ),
)


class CapabilitiesOut(APIModel):
    providers: list[ProviderInfo]
    claude_configured: bool  # true for either Claude route (Anthropic direct or OpenRouter)
    llm: LlmInfo = Field(default_factory=LlmInfo)
    whisper: WhisperInfo
    presets: list[WorldPresetInfo]
    lyric_styles: list[LyricStyleInfo]
    color_grades: list[ColorGradeInfo]
    fonts: list[FontInfo]
    durations: list[int] = Field(default_factory=lambda: list(DURATION_CHOICES))
    counts: list[int] = Field(default_factory=lambda: list(COUNT_CHOICES))
    limits: LimitsInfo
    generation_options: list[GenerationOptionInfo] = Field(default_factory=lambda: list(GENERATION_OPTIONS))


class ActiveJob(APIModel):
    batch_id: str
    project_id: str
    project_title: str
    status: BatchStatusT
    done: int
    total: int
    failed: int


class ProviderUsage(APIModel):
    provider: str
    amount_usd: float
    clips: int


class UsageOut(APIModel):
    total_usd: float
    month_usd: float
    by_provider: list[ProviderUsage]
    clips_generated: int
    clips_reused: int
    saved_usd: float


class DashboardOut(APIModel):
    projects: int
    videos_done: int
    videos_total: int
    active_jobs: list[ActiveJob]
    recent_videos: list[VideoOut]
    usage: UsageOut
    pending_estimate_usd: float


class StatusOut(APIModel):
    """Generic ``{status}`` response (e.g. analysis rerun 202)."""

    status: str
