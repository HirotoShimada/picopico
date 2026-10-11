import type {
  GenerationSettings,
  LimitsInfo,
  PresetId,
  ProviderId,
  ProviderKind,
  RightsKey,
  WorldPresetInfo,
} from "./types";

/** §9 rights checklist (all required before batch creation). */
export const RIGHTS_ITEMS: readonly { key: RightsKey; label: string }[] = [
  { key: "audio_rights", label: "音源の権利を保有、または利用許諾を得ている" },
  { key: "lyrics_rights", label: "歌詞の権利を保有、または利用許諾を得ている" },
  { key: "no_real_person_likeness", label: "実在の人物（アーティスト本人を含む）の肖像を生成しない" },
  { key: "model_terms_checked", label: "使用する動画生成モデルの利用規約・商用利用条件を確認した" },
  { key: "platform_terms", label: "投稿先（Instagram 等）の規約を確認した" },
];

export const PRESET_IDS: readonly PresetId[] = [
  "cinematic",
  "emotional",
  "urban_night",
  "nostalgic",
  "nature",
  "dark",
  "dreamy",
  "documentary",
  "custom",
];

/**
 * World presets shown before `/api/system/capabilities` answers (or when it is
 * unavailable). The server list wins when present.
 */
export const WORLD_PRESETS_FALLBACK: readonly WorldPresetInfo[] = [
  {
    id: "cinematic",
    name_ja: "シネマティック",
    name_en: "Cinematic",
    description_ja: "映画のようなライティングと浅い被写界深度。ドラマチックで洗練された映像。",
    palette: ["#1f3b4d", "#d98c4a", "#0d0d0f"],
  },
  {
    id: "emotional",
    name_ja: "エモーショナル",
    name_en: "Emotional",
    description_ja: "やわらかな逆光と感情に寄り添うクローズアップ。切なさや温かさを丁寧に描く。",
    palette: ["#f2c6a0", "#8c5e58", "#2b2633"],
  },
  {
    id: "urban_night",
    name_ja: "都会の夜",
    name_en: "Urban night",
    description_ja: "ネオンと街灯が映える夜の街。濡れた路面の反射、ビルの谷間、走る車の光跡。",
    palette: ["#0b1d3a", "#e23e8a", "#29c5d6"],
  },
  {
    id: "nostalgic",
    name_ja: "ノスタルジック",
    name_en: "Nostalgic",
    description_ja: "色褪せたフィルム調と粒子感。夕暮れの商店街や海辺など、懐かしい情景。",
    palette: ["#c9a46c", "#7a6a53", "#e8dcc4"],
  },
  {
    id: "nature",
    name_ja: "自然",
    name_en: "Nature",
    description_ja: "森・海・空など雄大な自然。朝の光や風の動きを活かした清らかな映像。",
    palette: ["#2f5d3a", "#9cc5a1", "#e3d9b5"],
  },
  {
    id: "dark",
    name_ja: "ダーク",
    name_en: "Dark",
    description_ja: "低照度で陰影の強いコントラスト。ミステリアスで緊張感のある世界観。",
    palette: ["#0a0a0c", "#3d1f2b", "#8a8f99"],
  },
  {
    id: "dreamy",
    name_ja: "ドリーミー",
    name_en: "Dreamy",
    description_ja: "パステル調の色彩と柔らかなボケ。幻想的で浮遊感のある映像。",
    palette: ["#f5c6de", "#b8c7f2", "#fff1d6"],
  },
  {
    id: "documentary",
    name_ja: "ドキュメンタリー",
    name_en: "Documentary",
    description_ja: "手持ちカメラ風の自然な質感。日常のリアルな瞬間を切り取る。",
    palette: ["#6b6f73", "#b59a7a", "#2e3236"],
  },
  {
    id: "custom",
    name_ja: "カスタム",
    name_en: "Custom",
    description_ja: "世界観を自由に記述します（日本語可）。場所・時間帯・色・質感などを具体的に。",
    palette: ["#3f3f46", "#71717a", "#a1a1aa"],
  },
];

export const PROVIDER_LABELS: Record<ProviderId, string> = {
  mock: "モック（ローカル合成）",
  library: "素材ライブラリ",
  kling: "Kling",
  runway: "Runway",
  veo: "Veo（Gemini API）",
};

export const PROVIDER_KIND_LABELS: Record<ProviderKind, string> = {
  mock: "モック",
  library: "ライブラリ",
  api: "外部 API",
};

export function providerLabel(id: string): string {
  return (PROVIDER_LABELS as Record<string, string>)[id] ?? id;
}

export const AUDIO_EXTENSIONS: readonly string[] = ["mp3", "wav", "m4a", "aac", "flac", "ogg"];
export const VIDEO_EXTENSIONS: readonly string[] = ["mp4", "mov", "webm", "m4v"];
export const LYRICS_EXTENSIONS: readonly string[] = ["txt", "lrc", "srt"];

export const AUDIO_MIN_SECONDS = 5;
export const AUDIO_MAX_SECONDS = 15 * 60;
export const MAX_CLIP_FILES_PER_UPLOAD = 20;
export const MAX_LYRICS_FILE_BYTES = 1024 * 1024;

/** Server defaults (§3); `/api/system/capabilities` is authoritative when loaded. */
export const DEFAULT_LIMITS: LimitsInfo = {
  max_audio_mb: 100,
  max_video_mb: 500,
  max_count: 50,
  min_duration: 5,
  max_duration: 90,
};

export const DEFAULT_DURATIONS: readonly number[] = [15, 30, 60];
export const DEFAULT_COUNTS: readonly number[] = [1, 3, 5, 10, 30];

/** Mirrors the backend `GenerationSettings` defaults. */
export const DEFAULT_GENERATION_SETTINGS: GenerationSettings = {
  preset: "cinematic",
  custom_world: null,
  provider: "mock",
  model: null,
  duration_sec: 15,
  count: 3,
  lyric_style: "simple",
  text_style: null,
  color_grade: "auto",
  start_strategy: "segment",
  start_sec: null,
  reuse: "balanced",
  max_new_clips: null,
  budget_limit_usd: null,
  seed: null,
};
