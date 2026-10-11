/** Japanese labels and display colours for the audio analysis (§6.1). */

/** Same labels as `presets.SECTION_LABELS_JA` on the backend. */
export const SECTION_LABELS_JA: Record<string, string> = {
  intro: "イントロ",
  verse: "Aメロ",
  chorus: "サビ",
  bridge: "ブリッジ",
  outro: "アウトロ",
  break: "間奏",
};

/**
 * Muted, non-blue hues (blue is reserved for primary actions and progress).
 * Each section is also labelled in text, so colour is never the only cue.
 */
export const SECTION_COLORS: Record<string, string> = {
  intro: "#71717a",
  verse: "#2dd4bf",
  chorus: "#f59e0b",
  bridge: "#a78bfa",
  outro: "#71717a",
  break: "#94a3b8",
};

export const CHORUS_MARKER_COLOR = "#f59e0b";
export const HIGHLIGHT_MARKER_COLOR = "#e879f9";

export function sectionLabel(label: string): string {
  return SECTION_LABELS_JA[label] ?? label;
}

export function sectionColor(label: string): string {
  return SECTION_COLORS[label] ?? "#71717a";
}

/** Mood tags produced by `audio_analysis._mood`. Unknown tags are shown as-is. */
export const MOOD_TAG_LABELS: Record<string, string> = {
  silent: "無音",
  energetic: "エネルギッシュ",
  calm: "穏やか",
  upbeat: "アップビート",
  slow: "スロー",
  bright: "明るい",
  dark: "ダーク",
  melancholic: "切ない",
  uplifting: "高揚感",
  dynamic: "ダイナミック",
  danceable: "ノリが良い",
};

export function moodTagLabel(tag: string): string {
  return MOOD_TAG_LABELS[tag] ?? tag;
}

export const TEMPO_CLASS_LABELS: Record<string, string> = {
  slow: "ゆったり",
  mid: "ミドルテンポ",
  fast: "アップテンポ",
};

export const MODE_LABELS: Record<string, string> = {
  major: "メジャー（明るめ）",
  minor: "マイナー（暗め）",
};
