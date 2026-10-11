import { isLyricStyleId, LYRIC_STYLE_SPECS, LYRIC_STYLES_FALLBACK, type LyricStyleSpec } from "@/components/lyrics/lyric-styles";
import type {
  ColorGradeInfo,
  FontWeight,
  LyricStyleChoice,
  LyricStyleId,
  LyricStyleInfo,
  TextPosition,
  TextStyle,
  WorldPresetInfo,
} from "@/lib/types";

/** Text-style fields the generate page and the editor let the user override. */
export type EditableTextStyleKey =
  | "font_size"
  | "primary_color"
  | "highlight_color"
  | "outline_color"
  | "outline_width"
  | "position"
  | "bg_opacity";

/** Server limits from `schemas.TextStyle`. */
export const FONT_SIZE_RANGE = { min: 32, max: 120 } as const;
export const OUTLINE_WIDTH_RANGE = { min: 0, max: 12 } as const;

const FONT_WEIGHT_CSS: Record<FontWeight, number> = { bold: 700, regular: 400, light: 300 };

export function toLyricStyleId(value: string | null | undefined): LyricStyleId {
  return value && isLyricStyleId(value) ? value : "simple";
}

/** Batch setting value: keeps `auto` (per-video rotation), otherwise a known style id. */
export function toLyricStyleChoice(value: string | null | undefined): LyricStyleChoice {
  return value === "auto" ? "auto" : toLyricStyleId(value);
}

export const AUTO_LYRIC_STYLE_LABEL = "自動（動画ごとに変える）";

/**
 * Styles `lyric_style: "auto"` rotates through, video by video: the preset's
 * recommended styles (same filter and fallback as `variation.plan_batch`).
 */
export function autoLyricStyles(preset: Pick<WorldPresetInfo, "lyric_styles"> | null | undefined): LyricStyleId[] {
  const styles = (preset?.lyric_styles ?? []).filter(isLyricStyleId);
  return styles.length > 0 ? styles : ["simple"];
}

/** Concrete style to preview a choice with: `auto` shows the first video's style. */
export function previewStyleId(choice: LyricStyleChoice, autoStyles: readonly LyricStyleId[]): LyricStyleId {
  return choice === "auto" ? (autoStyles[0] ?? "simple") : choice;
}

/** Display name of a lyric style setting (`auto` included; unknown ids are shown as is). */
export function lyricStyleChoiceName(value: string, styles: readonly LyricStyleInfo[]): string {
  if (value === "auto") return AUTO_LYRIC_STYLE_LABEL;
  return styles.find((style) => style.id === value)?.name_ja ?? LYRIC_STYLES_FALLBACK.find((style) => style.id === value)?.name_ja ?? value;
}

/** The style defaults merged with the user's overrides (approximation of `subtitles.resolve_text_style`). */
export function effectiveLyricSpec(styleId: LyricStyleId, overrides: TextStyle | null | undefined): LyricStyleSpec {
  const base = LYRIC_STYLE_SPECS[styleId] ?? LYRIC_STYLE_SPECS.simple;
  if (!overrides) return base;
  return {
    ...base,
    serif: overrides.font_family ? /serif/i.test(overrides.font_family) : base.serif,
    fontWeight: overrides.font_weight ? FONT_WEIGHT_CSS[overrides.font_weight] : base.fontWeight,
    fontSize: overrides.font_size ?? base.fontSize,
    primaryColor: overrides.primary_color ?? base.primaryColor,
    highlightColor: overrides.highlight_color ?? base.highlightColor,
    outlineColor: overrides.outline_color ?? base.outlineColor,
    outlineWidth: overrides.outline_width ?? base.outlineWidth,
    shadow: overrides.shadow ?? base.shadow,
    position: overrides.position ?? base.position,
    bgOpacity: overrides.bg_opacity ?? base.bgOpacity,
    letterSpacing: overrides.letter_spacing ?? base.letterSpacing,
    maxCharsPerLine: overrides.max_chars_per_line ?? base.maxCharsPerLine,
  };
}

/** Default value of an editable field for a lyric style (shown as placeholder). */
export function textStyleDefault(styleId: LyricStyleId, key: EditableTextStyleKey): string | number {
  const spec = LYRIC_STYLE_SPECS[styleId] ?? LYRIC_STYLE_SPECS.simple;
  switch (key) {
    case "font_size":
      return spec.fontSize;
    case "primary_color":
      return spec.primaryColor;
    case "highlight_color":
      return spec.highlightColor;
    case "outline_color":
      return spec.outlineColor;
    case "outline_width":
      return spec.outlineWidth;
    case "position":
      return spec.position;
    case "bg_opacity":
      return spec.bgOpacity;
  }
}

/** Drop unset (`null` / `undefined`) fields; `null` when nothing is overridden. */
export function compactTextStyle(style: TextStyle | null | undefined): TextStyle | null {
  if (!style) return null;
  const entries = Object.entries(style).filter(([, value]) => value !== null && value !== undefined);
  return entries.length > 0 ? (Object.fromEntries(entries) as TextStyle) : null;
}

export function textStylesEqual(a: TextStyle | null | undefined, b: TextStyle | null | undefined): boolean {
  const left = compactTextStyle(a) ?? {};
  const right = compactTextStyle(b) ?? {};
  const keys = new Set([...Object.keys(left), ...Object.keys(right)]) as Set<keyof TextStyle>;
  for (const key of keys) {
    const x = left[key];
    const y = right[key];
    if (typeof x === "string" && typeof y === "string" ? x.toUpperCase() !== y.toUpperCase() : x !== y) return false;
  }
  return true;
}

export const POSITION_LABELS: Record<TextPosition, string> = {
  top: "上",
  center: "中央",
  bottom: "下",
};

/** Same ids/names as `presets.color_grade_infos()`; the server list wins when loaded. */
export const COLOR_GRADES_FALLBACK: readonly ColorGradeInfo[] = [
  { id: "natural", name: "ナチュラル" },
  { id: "teal_orange", name: "ティール＆オレンジ" },
  { id: "warm_film", name: "ウォームフィルム" },
  { id: "cool_night", name: "クールナイト" },
  { id: "bleach_bypass", name: "ブリーチバイパス" },
  { id: "faded_pastel", name: "フェードパステル" },
  { id: "mono_noir", name: "モノクロノワール" },
  { id: "vivid", name: "ビビッド" },
  { id: "golden_hour", name: "ゴールデンアワー" },
  { id: "neon_night", name: "ネオンナイト" },
  { id: "forest_moody", name: "フォレストムーディ" },
  { id: "lavender_haze", name: "ラベンダーヘイズ" },
];

/**
 * Representative swatches per grade (shadow / mid / highlight tint) so the
 * picker is not text-only. Approximations of the FFmpeg looks.
 */
export const COLOR_GRADE_SWATCHES: Record<string, readonly [string, string, string]> = {
  natural: ["#2b2b2b", "#8a8478", "#e6e1d8"],
  teal_orange: ["#12343b", "#2f6f73", "#e39b5b"],
  warm_film: ["#3a2a1e", "#a8784f", "#efd6b0"],
  cool_night: ["#0d1626", "#2c4a6e", "#9fb8d6"],
  bleach_bypass: ["#1e1f1f", "#6f7372", "#d3d6d2"],
  faded_pastel: ["#5b5560", "#c4a9b8", "#efe3dc"],
  mono_noir: ["#0b0b0b", "#5c5c5c", "#e2e2e2"],
  vivid: ["#1b1033", "#d2335c", "#ffd23f"],
  golden_hour: ["#3b220f", "#c8782f", "#ffd9a0"],
  neon_night: ["#0b0820", "#c03fd0", "#2fd6e6"],
  forest_moody: ["#0f1a12", "#3e5f45", "#b7c6a4"],
  lavender_haze: ["#2a2338", "#9a86c2", "#ece2f5"],
};

export function colorGradeName(id: string, grades: readonly ColorGradeInfo[]): string {
  if (id === "auto") return "自動";
  return grades.find((grade) => grade.id === id)?.name ?? COLOR_GRADES_FALLBACK.find((grade) => grade.id === id)?.name ?? id;
}
