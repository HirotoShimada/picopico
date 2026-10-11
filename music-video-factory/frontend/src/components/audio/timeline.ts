/**
 * Pure timeline math shared by the waveform, the segment editor and the lyric
 * timing editor. All times are absolute seconds in the song.
 */

export interface TimeRange {
  start: number;
  end: number;
}

/** Backend rule for `PUT /segment`: `end - start >= 3`. */
export const MIN_SEGMENT_SECONDS = 3;

/** Time precision used for stored values (10 ms). */
export const TIME_PRECISION = 0.01;

export function roundTime(seconds: number, step = TIME_PRECISION): number {
  const rounded = Math.round(seconds / step) * step;
  // Strip floating point noise such as 12.340000000000002.
  return Number(rounded.toFixed(3));
}

export function clampTime(seconds: number, duration: number): number {
  return Math.min(Math.max(0, seconds), Math.max(0, duration));
}

/** Index of the last element `<= value` in an ascending array, or -1. */
export function lastIndexAtOrBefore(sorted: readonly number[], value: number): number {
  let low = 0;
  let high = sorted.length - 1;
  let result = -1;
  while (low <= high) {
    const mid = (low + high) >> 1;
    if (sorted[mid] <= value) {
      result = mid;
      low = mid + 1;
    } else {
      high = mid - 1;
    }
  }
  return result;
}

/** Nearest grid point to `value` (ascending grid), or `null` for an empty grid. */
export function nearestGridPoint(sorted: readonly number[], value: number): number | null {
  if (sorted.length === 0) return null;
  const index = lastIndexAtOrBefore(sorted, value);
  if (index < 0) return sorted[0];
  if (index >= sorted.length - 1) return sorted[sorted.length - 1];
  const before = sorted[index];
  const after = sorted[index + 1];
  return value - before <= after - value ? before : after;
}

/** Previous grid point strictly before `value - epsilon`, or `null`. */
export function previousGridPoint(sorted: readonly number[], value: number, epsilon = 0.02): number | null {
  const index = lastIndexAtOrBefore(sorted, value - epsilon);
  return index >= 0 ? sorted[index] : null;
}

/** Next grid point strictly after `value + epsilon`, or `null`. */
export function nextGridPoint(sorted: readonly number[], value: number, epsilon = 0.02): number | null {
  const index = lastIndexAtOrBefore(sorted, value + epsilon);
  return index + 1 < sorted.length ? sorted[index + 1] : null;
}

/** Returns a snapping function: to the nearest grid point, or to 10 ms without a grid. */
export function makeSnapper(grid: readonly number[] | null): (seconds: number) => number {
  if (!grid || grid.length === 0) return (seconds) => roundTime(seconds);
  return (seconds) => roundTime(nearestGridPoint(grid, seconds) ?? seconds);
}

export function rangesEqual(a: TimeRange | null | undefined, b: TimeRange | null | undefined, epsilon = 0.005): boolean {
  if (!a || !b) return !a && !b;
  return Math.abs(a.start - b.start) <= epsilon && Math.abs(a.end - b.end) <= epsilon;
}

/**
 * Fit a range into `[0, duration]` with at least `minLength` seconds (when the
 * song is long enough), keeping the start where possible.
 */
export function normalizeRange(range: TimeRange, duration: number, minLength = MIN_SEGMENT_SECONDS): TimeRange {
  const min = Math.min(minLength, duration);
  let start = clampTime(Math.min(range.start, range.end), duration);
  let end = clampTime(Math.max(range.start, range.end), duration);
  if (end - start < min) {
    end = start + min;
    if (end > duration) {
      end = duration;
      start = Math.max(0, duration - min);
    }
  }
  return { start: roundTime(start), end: roundTime(end) };
}

export type RangeDragMode = "start" | "end" | "move" | "create";

export interface RangeDragOptions {
  duration: number;
  minLength?: number;
  /** Snapping applied to the moving edge(s). */
  snap?: (seconds: number) => number;
}

/**
 * New selection for a pointer drag.
 * - `start` / `end`: move one edge (the other stays fixed).
 * - `move`: shift the whole range by `current - anchor`, keeping its length.
 * - `create`: a new range between `anchor` and `current`.
 */
export function applyRangeDrag(
  mode: RangeDragMode,
  origin: TimeRange | null,
  anchor: number,
  current: number,
  { duration, minLength = MIN_SEGMENT_SECONDS, snap = roundTime }: RangeDragOptions,
): TimeRange {
  const min = Math.min(minLength, duration);
  const at = (seconds: number) => clampTime(snap(clampTime(seconds, duration)), duration);

  if (mode === "start" && origin) {
    const limit = origin.end - min;
    let start = at(current);
    if (start > limit) start = Math.max(0, limit);
    return { start: roundTime(start), end: origin.end };
  }
  if (mode === "end" && origin) {
    const limit = origin.start + min;
    let end = at(current);
    if (end < limit) end = Math.min(duration, limit);
    return { start: origin.start, end: roundTime(end) };
  }
  if (mode === "move" && origin) {
    const length = origin.end - origin.start;
    let start = at(origin.start + (current - anchor));
    start = Math.min(Math.max(0, start), Math.max(0, duration - length));
    return { start: roundTime(start), end: roundTime(start + length) };
  }

  const a = at(anchor);
  const b = at(current);
  let start = Math.min(a, b);
  let end = Math.max(a, b);
  if (end - start < min) {
    if (current >= anchor) end = start + min;
    else start = end - min;
  }
  if (start < 0) {
    start = 0;
    end = min;
  }
  if (end > duration) {
    end = duration;
    start = Math.max(0, duration - min);
  }
  return { start: roundTime(start), end: roundTime(end) };
}

const TICK_STEPS = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600];

/** Ruler step (seconds) so that labels are at least `minSpacingPx` apart. */
export function chooseTickStep(pxPerSecond: number, minSpacingPx = 64): number {
  for (const step of TICK_STEPS) {
    if (step * pxPerSecond >= minSpacingPx) return step;
  }
  return TICK_STEPS[TICK_STEPS.length - 1];
}

/**
 * Parse user input for a time: `75.5`, `1:15.5`, `1:15` or `0:01:15.5`.
 * Returns `null` for empty input and `NaN` for invalid input.
 */
export function parseTimeInput(text: string): number | null {
  const value = text.trim().replace(/[：]/g, ":").replace(/[．。]/g, ".").replace(/[０-９]/g, (digit) =>
    String.fromCharCode(digit.charCodeAt(0) - 0xfee0),
  );
  if (!value) return null;
  if (/^\d+(\.\d+)?$/.test(value)) return Number(value);
  const match = /^(?:(\d+):)?(\d+):(\d{1,2}(?:\.\d+)?)$/.exec(value);
  if (!match) return Number.NaN;
  const hours = match[1] ? Number(match[1]) : 0;
  const minutes = Number(match[2]);
  const seconds = Number(match[3]);
  if (seconds >= 60) return Number.NaN;
  return hours * 3600 + minutes * 60 + seconds;
}
