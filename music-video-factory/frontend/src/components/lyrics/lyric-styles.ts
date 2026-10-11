import type * as React from "react";

import type { LyricStyleId, LyricStyleInfo, TextPosition } from "@/lib/types";

/**
 * CSS approximations of the five burned-in lyric styles. Values mirror
 * `backend/app/services/subtitles.py::STYLE_DEFAULTS` (1080×1920 canvas
 * pixels); the preview scales them to its width. libass sizes Noto CJK so a
 * glyph is ≈ 0.69 × `font_size` wide, hence `GLYPH_EM_RATIO`.
 */

export const CANVAS_WIDTH = 1080;
export const CANVAS_HEIGHT = 1920;
/** Instagram Stories: top 14 % hidden behind the profile/progress bar. */
export const SAFE_TOP_PX = 270;
/** Bottom ~20 % hidden behind the reply bar. */
export const SAFE_BOTTOM_PX = 380;
const SIDE_MARGIN_RATIO = 0.08;
const GLYPH_EM_RATIO = 0.69;

export interface LyricStyleSpec {
  serif: boolean;
  fontWeight: number;
  fontSize: number;
  primaryColor: string;
  highlightColor: string;
  outlineColor: string;
  outlineWidth: number;
  shadow: number;
  shadowMode: "hard" | "soft";
  shadowOpacity: number;
  softShadowBlur: number;
  position: TextPosition;
  bgOpacity: number;
  letterSpacing: number;
  maxCharsPerLine: number;
  lineSpacing: number;
  inset: number;
  fadeIn: number;
  fadeOut: number;
  rise: number;
  boxPadding: number;
}

const BASE: LyricStyleSpec = {
  serif: false,
  fontWeight: 700,
  fontSize: 68,
  primaryColor: "#FFFFFF",
  highlightColor: "#4DA3FF",
  outlineColor: "#000000",
  outlineWidth: 3,
  shadow: 2,
  shadowMode: "hard",
  shadowOpacity: 0.45,
  softShadowBlur: 0,
  position: "bottom",
  bgOpacity: 0,
  letterSpacing: 1,
  maxCharsPerLine: 14,
  lineSpacing: 1.08,
  inset: 60,
  fadeIn: 0.12,
  fadeOut: 0.12,
  rise: 0,
  boxPadding: 0,
};

export const LYRIC_STYLE_SPECS: Record<LyricStyleId, LyricStyleSpec> = {
  simple: BASE,
  cinematic: {
    ...BASE,
    serif: true,
    fontWeight: 500,
    fontSize: 54,
    primaryColor: "#F4EFE6",
    highlightColor: "#E8C27A",
    outlineWidth: 0,
    shadow: 0,
    shadowOpacity: 0,
    bgOpacity: 0.55,
    letterSpacing: 3,
    maxCharsPerLine: 16,
    lineSpacing: 1.0,
    inset: 20,
    fadeIn: 0.25,
    fadeOut: 0.25,
    boxPadding: 16,
  },
  minimal: {
    ...BASE,
    fontWeight: 300,
    fontSize: 56,
    highlightColor: "#FFFFFF",
    outlineWidth: 0,
    shadow: 2,
    shadowMode: "soft",
    shadowOpacity: 0.85,
    softShadowBlur: 4.5,
    position: "center",
    letterSpacing: 8,
    maxCharsPerLine: 13,
    lineSpacing: 1.3,
    inset: 40,
    fadeIn: 0.18,
    fadeOut: 0.18,
  },
  karaoke: {
    ...BASE,
    fontSize: 70,
    outlineWidth: 4,
    shadowOpacity: 0.4,
    maxCharsPerLine: 13,
    lineSpacing: 1.1,
    fadeIn: 0.08,
    fadeOut: 0.12,
  },
  fade: {
    ...BASE,
    fontWeight: 500,
    fontSize: 64,
    highlightColor: "#FFFFFF",
    outlineWidth: 0,
    shadow: 3,
    shadowMode: "soft",
    shadowOpacity: 0.6,
    softShadowBlur: 7,
    position: "center",
    letterSpacing: 2,
    maxCharsPerLine: 13,
    lineSpacing: 1.22,
    inset: 40,
    fadeIn: 0.3,
    fadeOut: 0.35,
    rise: 14,
  },
};

/** Same names/descriptions as `presets.LYRIC_STYLES` (used until capabilities load). */
export const LYRIC_STYLES_FALLBACK: readonly LyricStyleInfo[] = [
  { id: "simple", name_ja: "シンプル", description: "白の太字ゴシックに細い縁取りと柔らかい影。どの映像にも合う定番スタイル" },
  { id: "cinematic", name_ja: "シネマティック", description: "明朝体・小さめの文字を半透明の帯に載せる映画字幕風" },
  { id: "minimal", name_ja: "ミニマル", description: "細字で字間を広げ、縁取りなし。余白を活かした上品な表示" },
  { id: "karaoke", name_ja: "カラオケ", description: "歌に合わせて文字の色が左から流れるように変わる" },
  { id: "fade", name_ja: "フェード", description: "ビートに合わせて歌詞がふわっと現れて消える" },
];

export function isLyricStyleId(value: string): value is LyricStyleId {
  return value in LYRIC_STYLE_SPECS;
}

const SANS = '"Noto Sans CJK JP", "Noto Sans JP", "Hiragino Sans", "Hiragino Kaku Gothic ProN", "Yu Gothic", sans-serif';
const SERIF = '"Noto Serif CJK JP", "Noto Serif JP", "Hiragino Mincho ProN", "Yu Mincho", serif';

function rgba(hex: string, alpha: number): string {
  const value = hex.replace("#", "");
  const full = value.length === 3 ? value.replace(/./g, (c) => c + c) : value.slice(0, 6);
  const r = Number.parseInt(full.slice(0, 2), 16);
  const g = Number.parseInt(full.slice(2, 4), 16);
  const b = Number.parseInt(full.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

/** Text styles for the preview at `scale` (= preview width / 1080). */
export function lyricTextStyle(spec: LyricStyleSpec, scale: number): React.CSSProperties {
  const fontPx = spec.fontSize * GLYPH_EM_RATIO * scale;
  const shadows: string[] = [];
  if (spec.shadow > 0 && spec.shadowOpacity > 0) {
    const offset = spec.shadow * scale;
    shadows.push(
      spec.shadowMode === "soft"
        ? `0 ${offset}px ${Math.max(1, spec.softShadowBlur * 2 * scale)}px ${rgba("#000000", spec.shadowOpacity)}`
        : `${offset}px ${offset}px 0 ${rgba("#000000", spec.shadowOpacity)}`,
    );
  }
  return {
    fontFamily: spec.serif ? SERIF : SANS,
    fontWeight: spec.fontWeight,
    fontSize: `${fontPx}px`,
    lineHeight: spec.lineSpacing * 1.25,
    letterSpacing: `${spec.letterSpacing * scale}px`,
    color: spec.primaryColor,
    textShadow: shadows.length > 0 ? shadows.join(", ") : undefined,
    WebkitTextStroke: spec.outlineWidth > 0 ? `${spec.outlineWidth * 2 * scale}px ${spec.outlineColor}` : undefined,
    paintOrder: spec.outlineWidth > 0 ? "stroke fill" : undefined,
    wordBreak: "keep-all",
    overflowWrap: "anywhere",
    lineBreak: "strict",
    textAlign: "center",
  };
}

/** Background box for the cinematic style (per visual line). */
export function lyricBoxStyle(spec: LyricStyleSpec, scale: number): React.CSSProperties | undefined {
  if (spec.bgOpacity <= 0) return undefined;
  const padding = spec.boxPadding * scale;
  return {
    backgroundColor: rgba("#000000", spec.bgOpacity),
    padding: `${padding * 0.4}px ${padding}px`,
    boxDecorationBreak: "clone",
    WebkitBoxDecorationBreak: "clone",
  };
}

/** Absolute placement of the text block inside the 9:16 frame (respecting the safe area). */
export function lyricBlockStyle(spec: LyricStyleSpec, position: TextPosition, scale: number): React.CSSProperties {
  const safeWidth = CANVAS_WIDTH * (1 - 2 * SIDE_MARGIN_RATIO);
  const glyph = spec.fontSize * GLYPH_EM_RATIO + spec.letterSpacing;
  const maxWidth = Math.min(safeWidth, glyph * spec.maxCharsPerLine + 2 * spec.boxPadding) * scale;
  const base: React.CSSProperties = {
    position: "absolute",
    left: "50%",
    width: `${maxWidth}px`,
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
  };
  if (position === "top") return { ...base, top: `${(SAFE_TOP_PX + spec.inset) * scale}px`, transform: "translateX(-50%)" };
  if (position === "center") {
    const center = SAFE_TOP_PX + (CANVAS_HEIGHT - SAFE_TOP_PX - SAFE_BOTTOM_PX) / 2;
    return { ...base, top: `${center * scale}px`, transform: "translate(-50%, -50%)" };
  }
  return { ...base, bottom: `${(SAFE_BOTTOM_PX + spec.inset) * scale}px`, transform: "translateX(-50%)" };
}

/** Opacity and vertical rise for fade in/out at `time` within `[start, end)`. */
export function lyricFade(spec: LyricStyleSpec, time: number, start: number, end: number): { opacity: number; rise: number } {
  let opacity = 1;
  if (spec.fadeIn > 0 && time < start + spec.fadeIn) opacity = Math.min(opacity, (time - start) / spec.fadeIn);
  if (spec.fadeOut > 0 && time > end - spec.fadeOut) opacity = Math.min(opacity, (end - time) / spec.fadeOut);
  opacity = Math.min(1, Math.max(0, opacity));
  const appear = spec.fadeIn > 0 ? Math.min(1, Math.max(0, (time - start) / spec.fadeIn)) : 1;
  return { opacity, rise: spec.rise * (1 - appear) };
}
