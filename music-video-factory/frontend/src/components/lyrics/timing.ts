/**
 * Pure helpers for editing lyric timing (ARCHITECTURE.md §6.2). Times are
 * absolute song seconds. Nothing here changes a line's text: text is only
 * changed by an explicit user edit (`setLineText`).
 */

import type { LyricLine, LyricSource, WordTiming } from "@/lib/types";

import { roundTime } from "../audio/timeline";

/** Default length of a line without an end and without a following line (same as the LRC parser). */
export const DEFAULT_LINE_SECONDS = 4;
/** A derived end never holds a line longer than this when the next line starts much later. */
export const MAX_DERIVED_LINE_SECONDS = 8;
export const MIN_LINE_SECONDS = 0.1;

export const SOURCE_LABELS: Record<LyricSource, string> = {
  manual: "手動",
  lrc: "LRC",
  srt: "SRT",
  txt: "未設定",
  heuristic: "自動推定",
  whisper: "Whisper",
};

export function isTimed(line: LyricLine): boolean {
  return line.start !== null;
}

export function countTimed(lines: readonly LyricLine[]): number {
  return lines.reduce((count, line) => count + (line.start !== null ? 1 : 0), 0);
}

/** Next timed line start after `index` (skipping untimed lines), or `null`. */
function nextStart(lines: readonly LyricLine[], index: number): number | null {
  for (let i = index + 1; i < lines.length; i += 1) {
    const start = lines[i].start;
    if (start !== null) return start;
  }
  return null;
}

/**
 * End used for display/preview/export when `end` is empty: the next line's
 * start (at most 8 s later), else start + 4 s, capped at the song length.
 */
export function effectiveEnd(lines: readonly LyricLine[], index: number, songDuration?: number | null): number | null {
  const line = lines[index];
  if (!line || line.start === null) return null;
  if (line.end !== null) return line.end;
  const following = nextStart(lines, index);
  let end =
    following !== null && following > line.start
      ? Math.min(following, line.start + MAX_DERIVED_LINE_SECONDS)
      : line.start + DEFAULT_LINE_SECONDS;
  if (songDuration && songDuration > line.start) end = Math.min(end, songDuration);
  return roundTime(end);
}

/** Index of the line on screen at `time` (start ≤ time < end), or -1. Later lines win on overlap. */
export function activeLineIndex(lines: readonly LyricLine[], time: number, songDuration?: number | null): number {
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    const start = lines[i].start;
    if (start === null || start > time) continue;
    const end = effectiveEnd(lines, i, songDuration);
    if (end !== null && time < end) return i;
  }
  return -1;
}

/**
 * Linearly remap word timings from the old line span to the new one so a
 * karaoke sweep stays proportional. Returns `null` when that is not possible
 * (the renderer then splits the line evenly).
 */
export function remapWords(
  words: readonly WordTiming[] | null,
  from: { start: number | null; end: number | null },
  to: { start: number | null; end: number | null },
): WordTiming[] | null {
  if (!words || words.length === 0) return null;
  const { start: fromStart, end: fromEnd } = from;
  const { start: toStart, end: toEnd } = to;
  if (fromStart === null || fromEnd === null || toStart === null || toEnd === null) return null;
  const oldSpan = fromEnd - fromStart;
  const newSpan = toEnd - toStart;
  if (oldSpan <= 0 || newSpan <= 0) return null;
  const map = (t: number | null) => (t === null ? null : roundTime(toStart + ((t - fromStart) / oldSpan) * newSpan, 0.001));
  return words.map((word) => ({ text: word.text, start: map(word.start), end: map(word.end) }));
}

/** Set a line's start/end (user edit): marks it `manual`, clears confidence, remaps words. */
export function setLineTiming(line: LyricLine, start: number | null, end: number | null): LyricLine {
  const nextStartValue = start === null ? null : roundTime(start);
  const nextEndValue = end === null ? null : roundTime(end);
  if (nextStartValue === line.start && nextEndValue === line.end) return line;
  return {
    ...line,
    start: nextStartValue,
    end: nextEndValue,
    words: remapWords(line.words, line, { start: nextStartValue, end: nextEndValue }),
    source: "manual",
    confidence: null,
  };
}

/** Explicit user text edit. Word timings no longer match the text, so they are dropped. */
export function setLineText(line: LyricLine, text: string): LyricLine {
  if (text === line.text) return line;
  return { ...line, text, words: null, source: line.start !== null ? "manual" : line.source };
}

export function replaceLine(lines: readonly LyricLine[], index: number, line: LyricLine): LyricLine[] {
  if (lines[index] === line) return lines as LyricLine[];
  const next = lines.slice();
  next[index] = line;
  return next;
}

/** Shift a line's start or end by `delta` seconds (keeps start < end). */
export function nudgeLine(
  lines: readonly LyricLine[],
  index: number,
  edge: "start" | "end",
  delta: number,
  songDuration?: number | null,
): LyricLine[] {
  const line = lines[index];
  if (!line) return lines as LyricLine[];
  const max = songDuration ?? Number.POSITIVE_INFINITY;
  if (edge === "start") {
    if (line.start === null) return lines as LyricLine[];
    const end = line.end;
    let start = Math.min(Math.max(0, line.start + delta), max);
    if (end !== null) start = Math.min(start, end - MIN_LINE_SECONDS);
    return replaceLine(lines, index, setLineTiming(line, Math.max(0, start), end));
  }
  const currentEnd = effectiveEnd(lines, index, songDuration);
  if (currentEnd === null || line.start === null) return lines as LyricLine[];
  const end = Math.min(Math.max(line.start + MIN_LINE_SECONDS, currentEnd + delta), max);
  return replaceLine(lines, index, setLineTiming(line, line.start, end));
}

/**
 * "Tap" timing: line `index` starts at `time`. An explicit end of the previous
 * timed line that would overlap is trimmed to `time` (an empty end is derived
 * from this start anyway), and this line's end is cleared when it would no
 * longer be after its start (it is then derived from the next line).
 */
export function tapLineStart(lines: readonly LyricLine[], index: number, time: number): LyricLine[] {
  const line = lines[index];
  if (!line) return lines as LyricLine[];
  const start = roundTime(Math.max(0, time));
  const end = line.end !== null && line.end > start + MIN_LINE_SECONDS ? line.end : null;
  let next = replaceLine(lines, index, setLineTiming(line, start, end));
  for (let i = index - 1; i >= 0; i -= 1) {
    const previous = next[i];
    if (previous.start === null) continue;
    if (previous.start < start && previous.end !== null && previous.end > start) {
      next = replaceLine(next, i, setLineTiming(previous, previous.start, start));
    }
    break;
  }
  return next;
}

/** Set a line's end at `time` (tap the end of a held phrase). */
export function tapLineEnd(lines: readonly LyricLine[], index: number, time: number): LyricLine[] {
  const line = lines[index];
  if (!line || line.start === null || time <= line.start + MIN_LINE_SECONDS) return lines as LyricLine[];
  return replaceLine(lines, index, setLineTiming(line, line.start, roundTime(time)));
}

/** Clear a line's timing (start, end and words). */
export function clearLineTiming(lines: readonly LyricLine[], index: number): LyricLine[] {
  const line = lines[index];
  if (!line || (line.start === null && line.end === null)) return lines as LyricLine[];
  return replaceLine(lines, index, { ...line, start: null, end: null, words: null, source: "manual", confidence: null });
}

/** Fill empty ends with their derived value before saving (lines without a start keep `end: null`). */
export function fillMissingEnds(lines: readonly LyricLine[], songDuration?: number | null): LyricLine[] {
  return lines.map((line, index) => {
    if (line.start === null || line.end !== null) return line;
    return { ...line, end: effectiveEnd(lines, index, songDuration) };
  });
}

export type IssueLevel = "error" | "warning";

export interface TimingIssue {
  level: IssueLevel;
  message: string;
}

/** Per-line validation. Errors block saving; warnings are informational. */
export function validateLines(lines: readonly LyricLine[], songDuration?: number | null): TimingIssue[][] {
  return lines.map((line, index) => {
    const issues: TimingIssue[] = [];
    if (line.start === null) {
      if (line.end !== null) issues.push({ level: "error", message: "開始時刻がありません" });
      return issues;
    }
    if (line.end !== null && line.end <= line.start) issues.push({ level: "error", message: "終了が開始より前になっています" });
    if (songDuration && line.start >= songDuration) issues.push({ level: "error", message: "開始が曲の長さを超えています" });
    else if (songDuration && line.end !== null && line.end > songDuration + 0.05)
      issues.push({ level: "warning", message: "終了が曲の長さを超えています" });
    if (!line.text.trim()) issues.push({ level: "warning", message: "歌詞が空です" });

    for (let i = index - 1; i >= 0; i -= 1) {
      const previousStart = lines[i].start;
      if (previousStart === null) continue;
      if (line.start < previousStart) issues.push({ level: "warning", message: "前の行より早い開始時刻です" });
      break;
    }
    const end = effectiveEnd(lines, index, songDuration);
    const following = nextStart(lines, index);
    if (end !== null && following !== null && following >= line.start && following < end - 0.01) {
      issues.push({ level: "warning", message: "次の行と表示時間が重なっています" });
    }
    return issues;
  });
}

export function hasErrors(issues: readonly TimingIssue[][]): boolean {
  return issues.some((list) => list.some((issue) => issue.level === "error"));
}

function wordsEqual(a: readonly WordTiming[] | null, b: readonly WordTiming[] | null): boolean {
  if (a === b) return true;
  if (!a || !b || a.length !== b.length) return false;
  return a.every((word, i) => word.text === b[i].text && word.start === b[i].start && word.end === b[i].end);
}

export function linesEqual(a: readonly LyricLine[], b: readonly LyricLine[]): boolean {
  if (a === b) return true;
  if (a.length !== b.length) return false;
  return a.every((line, i) => {
    const other = b[i];
    return (
      line === other ||
      (line.id === other.id &&
        line.text === other.text &&
        line.start === other.start &&
        line.end === other.end &&
        line.source === other.source &&
        line.confidence === other.confidence &&
        wordsEqual(line.words, other.words))
    );
  });
}

export interface WordSpan {
  text: string;
  /** Index range in the line text. */
  from: number;
  to: number;
  start: number;
  end: number;
}

/**
 * Character spans with times for a karaoke-style sweep. Uses `words` when they
 * are exact substrings of the text (in order); otherwise splits the non-space
 * characters evenly over the line.
 */
export function karaokeSpans(line: LyricLine, end: number): WordSpan[] {
  const start = line.start;
  if (start === null || end <= start) return [];
  const spans: WordSpan[] = [];
  if (line.words && line.words.length > 0) {
    let cursor = 0;
    let ok = true;
    for (const word of line.words) {
      const at = word.text ? line.text.indexOf(word.text, cursor) : -1;
      if (at < 0 || word.start === null || word.end === null) {
        ok = false;
        break;
      }
      spans.push({ text: word.text, from: at, to: at + word.text.length, start: word.start, end: word.end });
      cursor = at + word.text.length;
    }
    if (ok) return spans;
    spans.length = 0;
  }
  const characters = Array.from(line.text);
  const visible = characters.filter((ch) => ch.trim() !== "").length;
  if (visible === 0) return [];
  const step = (end - start) / visible;
  let offset = 0;
  let k = 0;
  for (const ch of characters) {
    const length = ch.length;
    if (ch.trim() !== "") {
      spans.push({ text: ch, from: offset, to: offset + length, start: start + k * step, end: start + (k + 1) * step });
      k += 1;
    }
    offset += length;
  }
  return spans;
}
