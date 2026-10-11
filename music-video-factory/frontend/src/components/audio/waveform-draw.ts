import { formatTime } from "@/lib/format";
import type { AnalysisSection, EnergyRange, ScoredRange } from "@/lib/types";

import { CHORUS_MARKER_COLOR, HIGHLIGHT_MARKER_COLOR, sectionColor, sectionLabel } from "./sections";
import { chooseTickStep, type TimeRange } from "./timeline";

/** Vertical lanes of the waveform (CSS px). */
export interface WaveformLayout {
  sectionsTop: number;
  sectionsHeight: number;
  waveTop: number;
  waveHeight: number;
  beatsTop: number;
  beatsHeight: number;
  markersTop: number;
  markersHeight: number;
  rulerTop: number;
  rulerHeight: number;
}

const SECTIONS_HEIGHT = 22;
const LANE_GAP = 4;
const BEATS_HEIGHT = 12;
const MARKERS_HEIGHT = 10;
const RULER_HEIGHT = 18;

export function waveformLayout(height: number): WaveformLayout {
  const fixed = SECTIONS_HEIGHT + LANE_GAP + BEATS_HEIGHT + MARKERS_HEIGHT + LANE_GAP + RULER_HEIGHT;
  const waveHeight = Math.max(40, height - fixed);
  const waveTop = SECTIONS_HEIGHT + LANE_GAP;
  const beatsTop = waveTop + waveHeight;
  const markersTop = beatsTop + BEATS_HEIGHT;
  const rulerTop = markersTop + MARKERS_HEIGHT + LANE_GAP;
  return {
    sectionsTop: 0,
    sectionsHeight: SECTIONS_HEIGHT,
    waveTop,
    waveHeight,
    beatsTop,
    beatsHeight: BEATS_HEIGHT,
    markersTop,
    markersHeight: MARKERS_HEIGHT,
    rulerTop,
    rulerHeight: RULER_HEIGHT,
  };
}

export interface WaveformDrawInput {
  /** CSS pixels. */
  width: number;
  height: number;
  devicePixelRatio: number;
  duration: number;
  peaks: readonly number[];
  sections: readonly AnalysisSection[];
  beats: readonly number[];
  downbeats: readonly number[];
  chorusCandidates: readonly ScoredRange[];
  highlights: readonly EnergyRange[];
  selection: TimeRange | null;
  /** Server-side segment, drawn dashed when it differs from the selection. */
  savedSelection: TimeRange | null;
}

const COLORS = {
  waveSelected: "#e4e4e7",
  waveOutside: "#52525b",
  waveDefault: "#a1a1aa",
  midline: "#27272a",
  selectionFill: "rgba(255, 255, 255, 0.07)",
  selectionEdge: "#f4f4f5",
  savedEdge: "#a1a1aa",
  beat: "#3f3f46",
  downbeat: "#a1a1aa",
  rulerTick: "#3f3f46",
  rulerText: "#71717a",
  sectionText: "#e4e4e7",
} as const;

const FONT_STACK = '"Hiragino Sans", "Noto Sans JP", "Noto Sans CJK JP", "Yu Gothic UI", system-ui, sans-serif';

/** Alpha-blend a `#rrggbb` colour. */
function withAlpha(hex: string, alpha: number): string {
  const value = hex.replace("#", "");
  const r = Number.parseInt(value.slice(0, 2), 16);
  const g = Number.parseInt(value.slice(2, 4), 16);
  const b = Number.parseInt(value.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

/** Max peak per output bar (peaks are a fixed 1200-point envelope of the whole track). */
export function aggregatePeaks(peaks: readonly number[], bars: number): number[] {
  const result = new Array<number>(Math.max(0, bars)).fill(0);
  if (peaks.length === 0 || bars <= 0) return result;
  for (let i = 0; i < bars; i += 1) {
    const from = Math.floor((i * peaks.length) / bars);
    const to = Math.max(from + 1, Math.floor(((i + 1) * peaks.length) / bars));
    let max = 0;
    for (let j = from; j < to && j < peaks.length; j += 1) {
      const value = peaks[j];
      if (value > max) max = value;
    }
    result[i] = Math.min(1, Math.max(0, max));
  }
  return result;
}

export function drawWaveform(ctx: CanvasRenderingContext2D, input: WaveformDrawInput): void {
  const { width, height, devicePixelRatio: dpr, duration } = input;
  const layout = waveformLayout(height);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  if (width <= 0 || duration <= 0) return;

  const xAt = (seconds: number) => (seconds / duration) * width;
  const selection = input.selection;

  // --- sections lane ------------------------------------------------------------------------
  ctx.font = `500 11px ${FONT_STACK}`;
  ctx.textBaseline = "middle";
  for (const section of input.sections) {
    const x0 = Math.round(xAt(section.start));
    const x1 = Math.round(xAt(section.end));
    const w = Math.max(1, x1 - x0 - 1);
    const color = sectionColor(section.label);
    ctx.fillStyle = withAlpha(color, 0.2);
    ctx.fillRect(x0, layout.sectionsTop, w, layout.sectionsHeight);
    ctx.fillStyle = withAlpha(color, 0.9);
    ctx.fillRect(x0, layout.sectionsTop, w, 2);
    // Faint band behind the waveform so sections read across the whole height.
    ctx.fillStyle = withAlpha(color, 0.045);
    ctx.fillRect(x0, layout.waveTop, w, layout.waveHeight);

    const label = sectionLabel(section.label);
    const textWidth = ctx.measureText(label).width;
    if (textWidth + 10 <= w) {
      ctx.fillStyle = COLORS.sectionText;
      ctx.fillText(label, x0 + 6, layout.sectionsTop + layout.sectionsHeight / 2 + 1);
    }
  }

  // --- selection background -------------------------------------------------------------------
  if (selection) {
    const x0 = xAt(selection.start);
    const x1 = xAt(selection.end);
    ctx.fillStyle = COLORS.selectionFill;
    ctx.fillRect(x0, layout.waveTop, x1 - x0, layout.waveHeight + layout.beatsHeight);
  }

  // --- waveform (mirrored bars) ---------------------------------------------------------------------
  const barWidth = 2;
  const barStep = 3;
  const bars = Math.max(1, Math.floor(width / barStep));
  const values = aggregatePeaks(input.peaks, bars);
  const mid = layout.waveTop + layout.waveHeight / 2;
  const maxHalf = layout.waveHeight / 2 - 2;
  ctx.fillStyle = COLORS.midline;
  ctx.fillRect(0, Math.round(mid), width, 1);
  for (let i = 0; i < bars; i += 1) {
    const x = i * barStep;
    const centerTime = ((x + barWidth / 2) / width) * duration;
    const inside = selection ? centerTime >= selection.start && centerTime <= selection.end : null;
    ctx.fillStyle = inside === null ? COLORS.waveDefault : inside ? COLORS.waveSelected : COLORS.waveOutside;
    const half = Math.max(0.75, values[i] * maxHalf);
    ctx.fillRect(x, mid - half, barWidth, half * 2);
  }

  // --- beats lane ------------------------------------------------------------------------------------
  const pxPerSecond = width / duration;
  const beatSpacing = input.beats.length > 1 ? (input.beats[input.beats.length - 1] - input.beats[0]) / (input.beats.length - 1) : 0;
  if (beatSpacing * pxPerSecond >= 4) {
    ctx.fillStyle = COLORS.beat;
    for (const beat of input.beats) {
      ctx.fillRect(Math.round(xAt(beat)), layout.beatsTop + layout.beatsHeight - 4, 1, 4);
    }
  }
  const downbeatSpacing =
    input.downbeats.length > 1
      ? (input.downbeats[input.downbeats.length - 1] - input.downbeats[0]) / (input.downbeats.length - 1)
      : Number.POSITIVE_INFINITY;
  if (downbeatSpacing * pxPerSecond >= 4) {
    ctx.fillStyle = COLORS.downbeat;
    for (const downbeat of input.downbeats) {
      ctx.fillRect(Math.round(xAt(downbeat)), layout.beatsTop + 2, 1, layout.beatsHeight - 2);
    }
  }

  // --- markers lane: chorus candidates (amber) and highlights (fuchsia) -----------------------------------
  const markerHalf = layout.markersHeight / 2;
  for (const candidate of input.chorusCandidates) {
    const x0 = xAt(candidate.start);
    const x1 = xAt(candidate.end);
    ctx.fillStyle = withAlpha(CHORUS_MARKER_COLOR, 0.35 + 0.55 * Math.min(1, Math.max(0, candidate.score)));
    ctx.fillRect(x0, layout.markersTop + 1, Math.max(2, x1 - x0), markerHalf - 1);
  }
  for (const highlight of input.highlights) {
    const x0 = xAt(highlight.start);
    const x1 = xAt(highlight.end);
    ctx.fillStyle = withAlpha(HIGHLIGHT_MARKER_COLOR, 0.8);
    ctx.fillRect(x0, layout.markersTop + markerHalf + 1, Math.max(2, x1 - x0), markerHalf - 2);
  }

  // --- selection edges + saved segment --------------------------------------------------------------------
  const edgeTop = layout.sectionsTop;
  const edgeHeight = layout.markersTop - edgeTop;
  const saved = input.savedSelection;
  const savedDiffers =
    saved && (!selection || Math.abs(saved.start - selection.start) > 0.005 || Math.abs(saved.end - selection.end) > 0.005);
  if (saved && savedDiffers) {
    ctx.strokeStyle = COLORS.savedEdge;
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    for (const seconds of [saved.start, saved.end]) {
      const x = Math.round(xAt(seconds)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(x, layout.waveTop);
      ctx.lineTo(x, layout.beatsTop + layout.beatsHeight);
      ctx.stroke();
    }
    ctx.setLineDash([]);
  }
  if (selection) {
    ctx.fillStyle = COLORS.selectionEdge;
    for (const seconds of [selection.start, selection.end]) {
      ctx.fillRect(Math.round(xAt(seconds)) - 1, edgeTop, 2, edgeHeight);
    }
  }

  // --- ruler ------------------------------------------------------------------------------------------------
  const step = chooseTickStep(pxPerSecond);
  ctx.font = `10px ${FONT_STACK}`;
  ctx.textBaseline = "top";
  ctx.textAlign = "left";
  for (let seconds = 0; seconds <= duration + 1e-6; seconds += step) {
    const x = Math.round(xAt(seconds));
    ctx.fillStyle = COLORS.rulerTick;
    ctx.fillRect(Math.min(x, width - 1), layout.rulerTop, 1, 5);
    const label = formatTime(seconds);
    const textWidth = ctx.measureText(label).width;
    if (x + 3 + textWidth <= width) {
      ctx.fillStyle = COLORS.rulerText;
      ctx.fillText(label, x + 3, layout.rulerTop + 4);
    }
  }
}
