/**
 * API types mirroring ARCHITECTURE.md §5/§6 and `backend/app/schemas.py`.
 * Timestamps are ISO-8601 strings; media URLs are signed absolute URLs.
 */

export type ISODateString = string;

// --- enumerations ------------------------------------------------------------------

export type AnalysisStatus = "none" | "queued" | "running" | "done" | "failed";
export type LyricsFormat = "txt" | "lrc" | "srt";
export type LyricsFormatInput = "auto" | LyricsFormat;
export type LyricsExportFormat = "lrc" | "srt";
export type LyricSource = "manual" | "lrc" | "srt" | "txt" | "heuristic" | "whisper";
export type AssetKind = "audio" | "lyrics" | "material" | "clip" | "render" | "thumbnail" | "zip";
export type ClipSource = "upload" | "mock" | "kling" | "runway" | "veo" | "openrouter";
export type ClipStatus = "pending" | "generating" | "ready" | "failed" | "cancelled";
export type BatchStatus =
  | "queued"
  | "planning"
  | "generating_clips"
  | "rendering"
  | "completed"
  | "partial"
  | "failed"
  | "cancelled"
  | "budget_exceeded";
export type VideoStatus = "queued" | "waiting_clips" | "rendering" | "done" | "failed" | "cancelled";
export type PresetId =
  | "cinematic"
  | "emotional"
  | "urban_night"
  | "nostalgic"
  | "nature"
  | "dark"
  | "dreamy"
  | "documentary"
  | "custom";
export type ProviderId = "mock" | "library" | "kling" | "runway" | "veo" | "openrouter";
export type ProviderKind = "mock" | "library" | "api";
/** How Claude is reached for storyboards / translation: Anthropic directly or via OpenRouter. */
export type LlmRoute = "anthropic" | "openrouter";
/** Where the OpenRouter model list came from: fetched now, the cached listing, or the built-in candidates. */
export type ModelsSource = "live" | "cache" | "fallback";
export type LyricStyleId = "simple" | "cinematic" | "minimal" | "karaoke" | "fade";
/** Batch setting only: a style id, or `auto` = the planner rotates the preset's `lyric_styles` per video. */
export type LyricStyleChoice = LyricStyleId | "auto";
export type StartStrategy = "segment" | "chorus" | "varied" | "custom";
export type ReuseMode = "max_variety" | "balanced" | "economy";
export type Transition = "cut" | "crossfade";
export type Pan = "left" | "right" | "up" | "down" | "center";
export type FontWeight = "bold" | "regular" | "light";
export type TextPosition = "bottom" | "center" | "top";
export type AutoTimeMethod = "heuristic" | "whisper";
export type SectionLabel = "intro" | "verse" | "chorus" | "bridge" | "outro" | "break";
export type RightsKey =
  | "audio_rights"
  | "lyrics_rights"
  | "no_real_person_likeness"
  | "model_terms_checked"
  | "platform_terms";

// --- shared ------------------------------------------------------------------------

export interface Segment {
  start: number;
  end: number;
}

export interface AssetOut {
  id: string;
  kind: AssetKind;
  filename: string;
  content_type: string;
  size_bytes: number;
  duration: number | null;
  width: number | null;
  height: number | null;
  url: string;
  created_at: ISODateString;
}

// --- audio analysis (§6.1) -----------------------------------------------------------

export interface AnalysisSection {
  start: number;
  end: number;
  label: SectionLabel | string;
  energy: number;
  is_chorus_candidate: boolean;
}

export interface ScoredRange {
  start: number;
  end: number;
  score: number;
}

export interface EnergyRange {
  start: number;
  end: number;
  energy: number;
}

export interface SuggestedSegment {
  start: number;
  end: number;
  duration: number;
  score: number;
  reason: string;
}

export interface AudioMood {
  energy: number;
  tempo_class: "slow" | "mid" | "fast";
  brightness: number;
  mode_guess: "major" | "minor";
  tags: string[];
}

export interface AudioAnalysis {
  version: number;
  duration: number;
  sample_rate: number;
  channels: number;
  codec: string;
  bit_rate: number | null;
  bpm: number;
  bpm_confidence: number;
  beats: number[];
  downbeats: number[];
  loudness: { hop: number; rms_db: number[] };
  waveform: { points: number; peaks: number[] };
  sections: AnalysisSection[];
  chorus_candidates: ScoredRange[];
  highlights: EnergyRange[];
  quiet_parts: Segment[];
  suggested_segments: SuggestedSegment[];
  vocal_activity: { hop: number; values: number[] };
  mood: AudioMood;
}

export interface AnalysisOut {
  status: AnalysisStatus;
  error: string | null;
  analysis: AudioAnalysis | null;
  segment: Segment | null;
}

export interface AnalysisRerunOut {
  status: AnalysisStatus;
}

export interface SegmentUpdate {
  start: number;
  end: number;
}

export interface SegmentOut {
  segment: Segment;
}

// --- lyrics (§6.2) -----------------------------------------------------------------------

export interface WordTiming {
  text: string;
  start: number | null;
  end: number | null;
}

export interface LyricLine {
  id: string;
  text: string;
  /** Absolute seconds in the song. */
  start: number | null;
  end: number | null;
  words: WordTiming[] | null;
  source: LyricSource;
  confidence: number | null;
}

export interface LyricsUpdate {
  text: string;
  format?: LyricsFormatInput;
}

export interface LyricsParseOut {
  format: LyricsFormat;
  lines: LyricLine[];
  warnings: string[];
}

export interface LyricsOut {
  text: string;
  format: LyricsFormat;
  lines: LyricLine[];
}

export interface LyricLinesOut {
  lines: LyricLine[];
  /** e.g. the `lyrics_rights` reset after a change of the line texts (empty otherwise). */
  warnings: string[];
}

export interface AutoTimeRequest {
  method: AutoTimeMethod;
  start?: number | null;
  end?: number | null;
  overwrite_timed?: boolean;
}

export interface AutoTimeOut {
  lines: LyricLine[];
  method_used: AutoTimeMethod;
  warnings: string[];
}

// --- projects (§5.2) ---------------------------------------------------------------------

export interface ProjectCreate {
  title: string;
  artist_name: string;
  /** Other spellings of the artist name (romanised / English) that are kept out of prompts (likeness guard). */
  artist_aliases?: string[];
  mood?: string | null;
  purpose?: string | null;
  notes?: string | null;
  world_preset?: PresetId;
  custom_world?: string | null;
  budget_limit_usd?: number | null;
}

export type ProjectUpdate = Partial<ProjectCreate>;

export interface ProjectCounts {
  clips: number;
  videos: number;
  batches: number;
}

export interface ProjectOut {
  id: string;
  title: string;
  artist_name: string;
  artist_aliases: string[];
  mood: string | null;
  purpose: string | null;
  notes: string | null;
  audio_asset_id: string | null;
  audio: AssetOut | null;
  analysis: AudioAnalysis | null;
  analysis_status: AnalysisStatus;
  analysis_error: string | null;
  lyrics_text: string;
  lyrics_format: LyricsFormat;
  lyric_lines: LyricLine[];
  segment_start: number | null;
  segment_end: number | null;
  world_preset: PresetId | string;
  custom_world: string | null;
  rights: Partial<Record<RightsKey, boolean>>;
  rights_confirmed_at: ISODateString | null;
  budget_limit_usd: number | null;
  created_at: ISODateString;
  updated_at: ISODateString;
  counts: ProjectCounts;
  /** Notices about this request, e.g. `POST /audio` un-confirming `audio_rights` (empty otherwise). */
  warnings?: string[];
}

export interface ProjectSummary {
  id: string;
  title: string;
  artist_name: string;
  created_at: ISODateString;
  updated_at: ISODateString;
  has_audio: boolean;
  analysis_status: AnalysisStatus;
  lyric_line_count: number;
  video_count: number;
  cover_url: string | null;
}

export interface RightsUpdate {
  items: Partial<Record<RightsKey, boolean>>;
}

// --- clips (§5.5) ----------------------------------------------------------------------------

export interface ClipOut {
  id: string;
  source: ClipSource;
  provider: string;
  model: string | null;
  scene_id: string | null;
  prompt: string | null;
  status: ClipStatus;
  duration: number | null;
  width: number | null;
  height: number | null;
  cost_usd: number;
  error: string | null;
  url: string | null;
  thumbnail_url: string | null;
  created_at: ISODateString;
  used_in_videos: number;
}

// --- rendering / text style (§6.6) -----------------------------------------------------------

export interface TextStyle {
  font_family?: string | null;
  font_weight?: FontWeight | null;
  font_size?: number | null;
  primary_color?: string | null;
  highlight_color?: string | null;
  outline_color?: string | null;
  outline_width?: number | null;
  shadow?: number | null;
  position?: TextPosition | null;
  bg_opacity?: number | null;
  letter_spacing?: number | null;
  max_chars_per_line?: number | null;
}

// --- generation (§5.6) --------------------------------------------------------------------------

export interface GenerationSettings {
  preset: PresetId;
  custom_world: string | null;
  provider: ProviderId;
  /** Provider default when null. */
  model: string | null;
  /** 5..90 seconds. */
  duration_sec: number;
  /** 1..50 videos. */
  count: number;
  /** `auto` varies the style per video (each video's `plan.lyric_style` is a concrete id). */
  lyric_style: LyricStyleChoice;
  text_style: TextStyle | null;
  /** `auto` or a color grade id. */
  color_grade: string;
  start_strategy: StartStrategy;
  /** Used when `start_strategy` is `custom`. */
  start_sec: number | null;
  reuse: ReuseMode;
  max_new_clips: number | null;
  budget_limit_usd: number | null;
  seed: number | null;
  /** Ask Claude for a new storyboard instead of reusing the project's last one for the same world (one batch only). */
  new_storyboard?: boolean;
  /** Let the planner flip shots horizontally (mirrors any text/signage in the footage); default false. */
  allow_mirror?: boolean;
}

/** Request body for estimate / batch creation: omitted fields take server defaults. */
export type GenerationSettingsInput = Partial<GenerationSettings>;

export interface StoryboardRequest {
  preset: PresetId;
  custom_world?: string | null;
  scenes?: number | null;
}

export interface WorldBible {
  summary_ja: string;
  look_en: string;
  protagonist_en: string;
  location_en: string;
  palette: string[];
  consistency_tokens_en: string;
}

export interface StoryboardScene {
  id: string;
  section: string | null;
  lyric_ref: string | null;
  scene_type: string | null;
  camera_angle: string | null;
  camera_move: string | null;
  description_ja: string;
  prompt_en: string;
  negative_prompt_en: string;
  duration_hint: number | null;
  /** Additive (§6.3): song seconds of the scene's slot, null when unknown. */
  start?: number | null;
  end?: number | null;
}

export interface Storyboard {
  source: "claude" | "template";
  /**
   * Additive: the route of the Claude call — also set on a template fallback after a call
   * (then `model` / `cost_usd` are those of the failed attempt). Null when Claude was not called.
   */
  route?: LlmRoute | null;
  model: string | null;
  usage: { input_tokens: number; output_tokens: number } | null;
  cost_usd: number;
  world_bible: WorldBible;
  scenes: StoryboardScene[];
  warnings: string[];
}

export interface EstimateComparison {
  provider: string;
  model: string;
  total_usd: number;
  configured: boolean;
  /** Additive (§6.5 "As implemented"). */
  name?: string;
  selected?: boolean;
  clip_duration?: number;
  price_per_second_usd?: number;
  video_generation_usd?: number;
  verified?: boolean;
}

/**
 * Additive (§6.5): the plan re-priced with a lower `max_new_clips` that fits the budget (same storyboard,
 * refs and seed), so applying `max_new_clips` reproduces exactly this estimate.
 */
export interface EstimateBudgetFit {
  max_new_clips: number;
  new_clips: number;
  total_usd: number;
  /** Max shots per video of that plan (fitted). */
  shots_per_video: number;
  /** Max beat (pre-fit) shots per video. */
  beat_shots_per_video: number | null;
  max_shot_sec: number | null;
  min_speed: number | null;
  repeat_shots: number;
  /** Japanese one-liner, also appended to `Estimate.notes`. */
  note: string;
}

export interface Estimate {
  provider: string;
  model: string;
  clip_duration: number;
  videos: number;
  shots_total: number;
  unique_clips: number;
  new_clips: number;
  cached_clips: number;
  price_per_second_usd: number;
  video_generation_usd: number;
  llm_usd: number;
  total_usd: number;
  /** Every shot generated fresh. */
  naive_total_usd: number;
  savings_usd: number;
  budget_limit_usd: number | null;
  within_budget: boolean;
  variety_score: number | null;
  variety_warning: string | null;
  comparison: EstimateComparison[];
  notes: string[];
  /** Additive (§6.5): the selected model is usable now. */
  configured?: boolean;
  as_of?: string | null;
  pricing_verified?: boolean;
  pricing_source_url?: string | null;
  /** Additive (§6.4/§6.5), null or absent for older plans: shots per video of the (fitted) plan and the beat cut before fitting. */
  shots_per_video_min?: number | null;
  shots_per_video_max?: number | null;
  beat_shots_per_video_max?: number | null;
  /** Videos re-cut into fewer, longer shots so no clip repeats inside a video. */
  fitted_videos?: number | null;
  /** Longest shot (seconds) and slowest playback speed over all shots. */
  max_shot_sec?: number | null;
  min_speed?: number | null;
  /** Shots that reuse a clip already used earlier in the same video (library: an overlapping window). */
  repeat_shots?: number | null;
  overlap_repeat_shots?: number | null;
  scene_repeat_shots?: number | null;
  videos_with_repeats?: number | null;
  repeat_warning?: string | null;
  budget_fit?: EstimateBudgetFit | null;
}

// --- plans (§6.4) ----------------------------------------------------------------------------------

export interface ShotPlan {
  clip_key: string | null;
  clip_id: string | null;
  src_in: number;
  duration: number;
  hflip: boolean;
  zoom_from: number;
  zoom_to: number;
  pan: Pan;
  speed: number;
  /** Additive (§6.4). */
  scene_id?: string | null;
  section?: string | null;
  /** Set when the planned clip was unusable (failed/cancelled/deleted) and another clip of the batch was used. */
  substituted_from?: string | null;
}

export interface VideoPlan {
  index: number;
  title: string;
  seed: number | null;
  start: number;
  duration: number;
  lyric_style: LyricStyleId | string;
  text_style: TextStyle;
  color_grade: string;
  shots: ShotPlan[];
  transition: Transition;
  /** Pipeline additions (§5.9): number of shots filled with a substitute clip. */
  substitutions?: number;
  regenerated?: boolean;
  edited?: boolean;
  lyric_offsets?: Record<string, LyricOffset> | null;
  /** Planner addition (§6.4): how the video's cuts were fitted to its clips. */
  fit?: VideoFit;
}

/** `VideoPlan.fit`: beat cut vs planned shots, and the repeats left inside the video. */
export interface VideoFit {
  beat_shots: number;
  shots: number;
  /** Re-cut into fewer, longer shots (≤ 8 s, ≥ 0.75x) to avoid repeating a clip. */
  fitted: boolean;
  min_speed: number;
  max_shot_sec: number;
  repeat_shots: number;
  overlap_repeat_shots: number;
  scene_repeat_shots: number;
}

export interface ClipSpec {
  key: string;
  scene_id: string | null;
  prompt: string;
  negative_prompt: string;
  duration: number;
  seed: number | null;
  cache_key: string;
  existing_clip_id: string | null;
  clip_id?: string | null;
}

export interface BatchPlanStats {
  videos: number;
  shots_total: number;
  unique_clips: number;
  new_clips: number;
  reused_clips: number;
  variety_score: number;
  variety_warning: string | null;
  /** Additive (§6.4); absent in plans made before the no-repeat fit. */
  pool_target?: number;
  clip_duration?: number;
  /** Shots per video of the fitted plan, and the beat cut before fitting. */
  shots_per_video?: number[];
  beat_shots_per_video?: number[];
  fitted_videos?: number;
  repeat_shots?: number;
  overlap_repeat_shots?: number;
  scene_repeat_shots?: number;
  videos_with_repeats?: number;
  max_shot_sec?: number | null;
  min_speed?: number | null;
  repeat_warning?: string | null;
  /** Clips needed to fit the video without repeats (`fit_clip_floor`); 0 for library. */
  pool_fit_floor?: number;
}

export interface BatchPlan {
  clip_specs: ClipSpec[];
  videos: VideoPlan[];
  stats: BatchPlanStats;
}

// --- quality (§6.6) ---------------------------------------------------------------------------------

export interface QualityCheck {
  name: string;
  passed: boolean;
  value: unknown;
  expected: unknown;
}

export interface QualityReport {
  passed: boolean;
  checks: QualityCheck[];
  loudness_lufs: number | null;
  black_ratio: number | null;
}

// --- videos (§5.7) ----------------------------------------------------------------------------------

export interface VideoOut {
  id: string;
  project_id: string;
  batch_id: string;
  index: number;
  title: string;
  status: VideoStatus;
  /** 0..1 */
  progress: number;
  error: string | null;
  favorite: boolean;
  url: string | null;
  thumbnail_url: string | null;
  duration: number | null;
  quality: QualityReport | null;
  plan: VideoPlan;
  render_version: number;
  created_at: ISODateString;
  rendered_at: ISODateString | null;
}

export interface VideoDetailOut extends VideoOut {
  /** Lyric lines effective for this video (absolute song times). */
  lyric_lines: LyricLine[];
  /** Ready clips that can be swapped into a shot. */
  available_clips: ClipOut[];
}

export interface VideoUpdate {
  favorite?: boolean;
  title?: string;
}

export interface VideoRegenerateRequest {
  new_clips?: boolean;
}

export interface LyricOffset {
  start: number;
  end: number;
}

export interface ShotEdit {
  index: number;
  clip_id: string;
  src_in?: number | null;
}

export interface VideoEdit {
  start?: number | null;
  lyric_style?: LyricStyleId | null;
  text_style?: TextStyle | null;
  color_grade?: string | null;
  lyric_offsets?: Record<string, LyricOffset> | null;
  shots?: ShotEdit[] | null;
}

export interface VideoListFilters {
  favorite?: boolean;
  batch_id?: string;
  /** `false` hides cancelled videos (server default: shown). */
  include_cancelled?: boolean;
}

export interface ClipListFilters {
  /** `false` hides cancelled clips (server default: shown). */
  include_cancelled?: boolean;
}

/** Signed `GET /media-zip/…` link (`POST /api/videos/zip-link`, `POST /api/batches/{id}/zip-link`): no auth header needed. */
export interface ZipLinkOut {
  url: string;
  expires_at: ISODateString;
}

// --- batches (§5.6) ----------------------------------------------------------------------------------

export interface BatchCounts {
  total: number;
  done: number;
  rendering: number;
  queued: number;
  failed: number;
  cancelled: number;
}

export interface ClipCounts {
  total: number;
  ready: number;
  generating: number;
  failed: number;
  reused: number;
}

export interface BatchOut {
  id: string;
  project_id: string;
  status: BatchStatus;
  settings: GenerationSettings;
  estimate: Estimate | null;
  storyboard: Storyboard | null;
  error: string | null;
  cost_usd: number;
  created_at: ISODateString;
  started_at: ISODateString | null;
  finished_at: ISODateString | null;
  counts: BatchCounts;
  clip_counts: ClipCounts;
  /** Additive: while a finished batch is re-opened for a re-render, the status / reason it had before. */
  reopened_from?: BatchStatus | null;
  previous_error?: string | null;
}

/** `PATCH /api/batches/{id}`: a new budget cap (e.g. to resume a `budget_exceeded` batch). */
export interface BatchUpdate {
  budget_limit_usd: number;
}

/** `GET /api/batches/{id}` includes the batch's videos and clips. */
export interface BatchDetailOut extends BatchOut {
  videos: VideoOut[];
  clips: ClipOut[];
}

// --- system & dashboard (§5.1) ------------------------------------------------------------------------

export interface HealthOut {
  status: "ok" | "degraded";
  db: boolean;
  redis: boolean;
  ffmpeg: boolean;
  task_mode: string;
  storage_backend: string;
}

export interface ProviderModelInfo {
  id: string;
  name: string;
  clip_durations: number[];
  max_clip_duration: number;
  resolution: string;
  supports_vertical: boolean;
  /** Null when the price is unknown (`price_known: false`, e.g. an OpenRouter listing entry without a readable price). */
  price_per_second_usd: number | null;
  price_note: string;
  verified: boolean;
  /** Additive: `false` = no readable price; the model cannot be used (the budget cannot be reserved). */
  price_known?: boolean;
  /** Additive (§6.7 "As implemented"): clip length used for a 5 s request. */
  default_duration?: number;
  /** `false` marks image-only models (e.g. Runway gen4_turbo). */
  text_to_video?: boolean;
  image_to_video?: boolean;
  as_of?: string;
  source_url?: string;
}

export interface ProviderInfo {
  id: ProviderId | string;
  name: string;
  configured: boolean;
  available: boolean;
  reason: string | null;
  kind: ProviderKind;
  commercial_use_note: string;
  models: ProviderModelInfo[];
  /** Additive (§6.7): model used when `GenerationSettings.model` is null. */
  default_model?: string;
  /** Additive, providers with a fetched model list (OpenRouter): where `models` came from. */
  models_source?: ModelsSource;
  /** When the listing was fetched (ISO), null for the built-in candidates. */
  models_fetched_at?: ISODateString | null;
  /** Japanese note on the listing (source, staleness, models left out). */
  models_note?: string;
}

export interface WhisperInfo {
  available: boolean;
  reason: string | null;
}

export interface WorldPresetInfo {
  id: PresetId | string;
  name_ja: string;
  name_en: string;
  description_ja: string;
  palette: string[];
  /** Additive: recommended lyric styles, best first; `lyric_style: "auto"` rotates them per video. */
  lyric_styles?: string[];
  /** Colour grades `color_grade: "auto"` rotates through. */
  color_grades?: string[];
}

/** `GET /api/projects/{id}/world-estimate` (感情・世界観推定; deterministic, no API call). */
export interface WorldEstimate {
  /** Emotion ids: melancholic nostalgic hopeful energetic romantic calm dark dreamy. */
  emotions: string[];
  /** Theme ids: night nature. */
  themes: string[];
  mood_en: string;
  recommended_presets: (PresetId | string)[];
  sources: { audio: string[]; lyrics: string[]; user: string[] };
  rationale_ja: string;
  /** False until the song analysis is done (then only lyrics and the project's mood/purpose count). */
  analysis_used: boolean;
}

export interface LyricStyleInfo {
  id: LyricStyleId | string;
  name_ja: string;
  description: string;
}

export interface ColorGradeInfo {
  id: string;
  name: string;
}

export interface FontInfo {
  id: string;
  family: string;
  label: string;
}

export interface LimitsInfo {
  max_audio_mb: number;
  max_video_mb: number;
  max_count: number;
  min_duration: number;
  max_duration: number;
}

/** A boolean `GenerationSettings` option (id = field name), e.g. `allow_mirror`. */
export interface GenerationOptionInfo {
  id: string;
  name_ja: string;
  description_ja: string;
  default: boolean;
}

/** Additive (`CapabilitiesOut.llm`): how Claude is reached; never contains secrets. */
export interface LlmInfo {
  /** `anthropic` = ANTHROPIC_API_KEY (direct), `openrouter` = OPENROUTER_API_KEY only, null = template / glossary. */
  route: LlmRoute | null;
  /** e.g. `claude-opus-5-5` (direct) or `anthropic/claude-opus-5.5` (OpenRouter). */
  model: string | null;
  /** Japanese: what is sent where on this route. */
  data_policy_note: string;
  /** False until the route has been exercised with a real key. */
  verified: boolean;
}

export interface CapabilitiesOut {
  providers: ProviderInfo[];
  /** True for either Claude route (Anthropic direct or OpenRouter); `llm` says which. */
  claude_configured: boolean;
  llm?: LlmInfo;
  whisper: WhisperInfo;
  presets: WorldPresetInfo[];
  lyric_styles: LyricStyleInfo[];
  color_grades: ColorGradeInfo[];
  fonts: FontInfo[];
  durations: number[];
  counts: number[];
  limits: LimitsInfo;
  generation_options?: GenerationOptionInfo[];
}

export interface ActiveJob {
  batch_id: string;
  project_id: string;
  project_title: string;
  status: BatchStatus;
  done: number;
  total: number;
  failed: number;
}

export interface ProviderUsage {
  provider: string;
  amount_usd: number;
  clips: number;
}

export interface UsageOut {
  total_usd: number;
  month_usd: number;
  by_provider: ProviderUsage[];
  clips_generated: number;
  clips_reused: number;
  saved_usd: number;
}

export interface DashboardOut {
  projects: number;
  videos_done: number;
  videos_total: number;
  active_jobs: ActiveJob[];
  recent_videos: VideoOut[];
  usage: UsageOut;
  pending_estimate_usd: number;
}

export interface StatusOut {
  status: string;
}
