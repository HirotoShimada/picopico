import { compactTextStyle, textStylesEqual, toLyricStyleId } from "@/components/generate/text-style";
import type { LyricLine, LyricOffset, LyricStyleId, ShotEdit, ShotPlan, TextStyle, VideoDetailOut, VideoEdit } from "@/lib/types";

/** Seconds; edits smaller than this are treated as "unchanged". */
const EPSILON = 0.005;
/** Shortest lyric line the editor allows. */
export const MIN_LINE_SECONDS = 0.2;

/**
 * Unsaved edits relative to the current render. Only differences are stored,
 * so a refreshed `VideoDetailOut` (new render) automatically becomes the base.
 */
export interface EditDraft {
  /** Absolute song seconds of the video start. */
  start?: number;
  lyricStyle?: LyricStyleId;
  /** `null` = clear all text overrides. */
  textStyle?: TextStyle | null;
  colorGrade?: string;
  /** Line id → absolute song seconds. */
  lyricTimes: Record<string, LyricOffset>;
  /** Shot index → clip id. */
  shots: Record<number, string>;
}

export const EMPTY_DRAFT: EditDraft = { lyricTimes: {}, shots: {} };

export interface EffectiveEdit {
  start: number;
  duration: number;
  lyricStyle: LyricStyleId;
  textStyle: TextStyle | null;
  colorGrade: string;
  lines: LyricLine[];
  shots: ShotPlan[];
}

function round2(value: number): number {
  return Math.round(value * 100) / 100;
}

export function baseOf(video: VideoDetailOut): EffectiveEdit {
  const plan = video.plan;
  return {
    start: plan?.start ?? 0,
    duration: plan?.duration ?? video.duration ?? 0,
    lyricStyle: toLyricStyleId(plan?.lyric_style),
    textStyle: compactTextStyle(plan?.text_style),
    colorGrade: plan?.color_grade ?? "natural",
    lines: video.lyric_lines ?? [],
    shots: plan?.shots ?? [],
  };
}

/** Current render merged with the draft. */
export function applyDraft(video: VideoDetailOut, draft: EditDraft): EffectiveEdit {
  const base = baseOf(video);
  return {
    ...base,
    start: draft.start ?? base.start,
    lyricStyle: draft.lyricStyle ?? base.lyricStyle,
    textStyle: draft.textStyle !== undefined ? draft.textStyle : base.textStyle,
    colorGrade: draft.colorGrade ?? base.colorGrade,
    lines: base.lines.map((line) => {
      const time = draft.lyricTimes[line.id];
      return time ? { ...line, start: time.start, end: time.end } : line;
    }),
    shots: base.shots.map((shot, index) => {
      const clipId = draft.shots[index];
      return clipId ? { ...shot, clip_id: clipId } : shot;
    }),
  };
}

/** Remove entries equal to the base so the change count stays honest. */
export function normalizeDraft(video: VideoDetailOut, draft: EditDraft): EditDraft {
  const base = baseOf(video);
  const next: EditDraft = { lyricTimes: {}, shots: {} };
  if (draft.start !== undefined && Math.abs(draft.start - base.start) > EPSILON) next.start = round2(draft.start);
  if (draft.lyricStyle !== undefined && draft.lyricStyle !== base.lyricStyle) next.lyricStyle = draft.lyricStyle;
  if (draft.textStyle !== undefined && !textStylesEqual(draft.textStyle, base.textStyle)) next.textStyle = compactTextStyle(draft.textStyle);
  if (draft.colorGrade !== undefined && draft.colorGrade !== base.colorGrade) next.colorGrade = draft.colorGrade;
  for (const line of base.lines) {
    const time = draft.lyricTimes[line.id];
    if (!time) continue;
    const startChanged = line.start === null || Math.abs(time.start - line.start) > EPSILON;
    const endChanged = line.end === null || Math.abs(time.end - line.end) > EPSILON;
    if (startChanged || endChanged) next.lyricTimes[line.id] = { start: round2(time.start), end: round2(time.end) };
  }
  base.shots.forEach((shot, index) => {
    const clipId = draft.shots[index];
    if (clipId && clipId !== shot.clip_id) next.shots[index] = clipId;
  });
  return next;
}

export interface ChangeSummary {
  count: number;
  labels: string[];
}

export function summarizeDraft(draft: EditDraft): ChangeSummary {
  const labels: string[] = [];
  if (draft.start !== undefined) labels.push("開始位置");
  if (draft.lyricStyle !== undefined) labels.push("歌詞スタイル");
  if (draft.textStyle !== undefined) labels.push("文字の設定");
  if (draft.colorGrade !== undefined) labels.push("カラー");
  const lines = Object.keys(draft.lyricTimes).length;
  if (lines > 0) labels.push(`歌詞タイミング ${lines} 行`);
  const shots = Object.keys(draft.shots).length;
  if (shots > 0) labels.push(`ショット ${shots} 件`);
  return { count: labels.length, labels };
}

/**
 * `PUT /api/videos/{id}/edit` body: only changed fields. `text_style` is sent
 * in full (it replaces the video's overrides; `{}` clears them),
 * `lyric_offsets` / `shots` only for changed lines / shots (merged server-side).
 */
export function draftToEdit(draft: EditDraft): VideoEdit {
  const edit: VideoEdit = {};
  if (draft.start !== undefined) edit.start = round2(draft.start);
  if (draft.lyricStyle !== undefined) edit.lyric_style = draft.lyricStyle;
  if (draft.textStyle !== undefined) edit.text_style = compactTextStyle(draft.textStyle) ?? {};
  if (draft.colorGrade !== undefined) edit.color_grade = draft.colorGrade;
  const offsets = Object.entries(draft.lyricTimes);
  if (offsets.length > 0) edit.lyric_offsets = Object.fromEntries(offsets);
  const shots: ShotEdit[] = Object.entries(draft.shots).map(([index, clipId]) => ({ index: Number(index), clip_id: clipId }));
  if (shots.length > 0) edit.shots = shots.sort((a, b) => a.index - b.index);
  return edit;
}

/** Lines overlapping `[start, start + duration]` (untimed lines are excluded). */
export function linesInWindow(lines: readonly LyricLine[], start: number, duration: number): LyricLine[] {
  const end = start + duration;
  return lines.filter((line) => line.start !== null && line.start < end && (line.end ?? line.start) > start);
}
