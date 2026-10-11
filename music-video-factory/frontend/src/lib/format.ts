/** Display formatting (ja-JP). Pure functions; safe in server and client components. */

const numberFormat = new Intl.NumberFormat("ja-JP");
const dateTimeFormat = new Intl.DateTimeFormat("ja-JP", {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});
const dateFormat = new Intl.DateTimeFormat("ja-JP", { year: "numeric", month: "2-digit", day: "2-digit" });

function isFiniteNumber(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** Seconds → `m:ss` (e.g. `1:05`). Negative values are clamped to 0. */
export function formatTime(seconds: number | null | undefined): string {
  if (!isFiniteNumber(seconds)) return "--:--";
  const total = Math.max(0, Math.floor(seconds));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

/** Seconds → `m:ss.cc` with centiseconds, for timing editors (e.g. `1:05.24`). */
export function formatTimePrecise(seconds: number | null | undefined): string {
  if (!isFiniteNumber(seconds)) return "--:--.--";
  const clamped = Math.max(0, seconds);
  const centis = Math.round(clamped * 100);
  const m = Math.floor(centis / 6000);
  const s = Math.floor((centis % 6000) / 100);
  const cs = centis % 100;
  return `${m}:${s.toString().padStart(2, "0")}.${cs.toString().padStart(2, "0")}`;
}

/** Seconds → Japanese length such as `15秒` / `1分05秒` / `12.5秒`. */
export function formatDuration(seconds: number | null | undefined): string {
  if (!isFiniteNumber(seconds)) return "—";
  if (seconds < 60) {
    const rounded = Math.round(seconds * 10) / 10;
    return `${Number.isInteger(rounded) ? rounded.toFixed(0) : rounded.toFixed(1)}秒`;
  }
  const total = Math.round(seconds);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}分${s.toString().padStart(2, "0")}秒`;
}

/** Bytes → `12.3 MB`. */
export function formatBytes(bytes: number | null | undefined): string {
  if (!isFiniteNumber(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 100 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
}

/** USD amount. Sub-cent amounts keep 4 decimals so tiny LLM costs stay visible. */
export function formatUsd(amount: number | null | undefined, options: { compact?: boolean } = {}): string {
  if (!isFiniteNumber(amount)) return "—";
  const abs = Math.abs(amount);
  const sign = amount < 0 ? "-" : "";
  if (abs === 0) return "$0.00";
  if (abs < 0.01) return `${sign}$${abs.toFixed(4)}`;
  if (options.compact && abs >= 10_000) {
    return `${sign}$${new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(abs)}`;
  }
  return `${sign}$${abs.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** Per-second price such as `$0.075/秒`. */
export function formatPricePerSecond(amount: number | null | undefined): string {
  if (!isFiniteNumber(amount)) return "—";
  if (amount === 0) return "無料";
  const digits = amount < 0.01 ? 4 : 3;
  return `$${amount.toFixed(digits).replace(/0+$/, "").replace(/\.$/, ".0")}/秒`;
}

export function formatCount(value: number | null | undefined): string {
  return isFiniteNumber(value) ? numberFormat.format(value) : "—";
}

/** 0..1 → `42%`. */
export function formatPercent(fraction: number | null | undefined, digits = 0): string {
  if (!isFiniteNumber(fraction)) return "—";
  return `${(fraction * 100).toFixed(digits)}%`;
}

export function formatBpm(bpm: number | null | undefined): string {
  return isFiniteNumber(bpm) ? `${bpm.toFixed(1).replace(/\.0$/, "")} BPM` : "—";
}

function toDate(value: string | Date | null | undefined): Date | null {
  if (!value) return null;
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** ISO timestamp → `2026/10/11 14:32` (viewer's time zone). */
export function formatDateTime(value: string | Date | null | undefined): string {
  const date = toDate(value);
  return date ? dateTimeFormat.format(date) : "—";
}

/** ISO timestamp → `2026/10/11`. */
export function formatDate(value: string | Date | null | undefined): string {
  const date = toDate(value);
  return date ? dateFormat.format(date) : "—";
}

/**
 * ISO timestamp → relative Japanese time (`3分前`). `now` must be supplied by
 * the caller (render-time `Date.now()` is not allowed in prerendered output).
 */
export function formatRelativeTime(value: string | Date | null | undefined, now: number): string {
  const date = toDate(value);
  if (!date) return "—";
  const diff = Math.round((now - date.getTime()) / 1000);
  if (diff < 0) return formatDateTime(date);
  if (diff < 45) return "たった今";
  if (diff < 3600) return `${Math.max(1, Math.round(diff / 60))}分前`;
  if (diff < 86_400) return `${Math.round(diff / 3600)}時間前`;
  if (diff < 7 * 86_400) return `${Math.round(diff / 86_400)}日前`;
  return formatDate(date);
}

/** Elapsed time between two timestamps as `1分05秒`, or `—`. */
export function formatElapsed(start: string | null | undefined, end: string | number | null | undefined): string {
  const startDate = toDate(start);
  if (!startDate || end === null || end === undefined) return "—";
  const endMs = typeof end === "number" ? end : toDate(end)?.getTime();
  if (endMs === undefined) return "—";
  return formatDuration(Math.max(0, (endMs - startDate.getTime()) / 1000));
}

/** `1080×1920` */
export function formatResolution(width: number | null | undefined, height: number | null | undefined): string {
  return isFiniteNumber(width) && isFiniteNumber(height) ? `${width}×${height}` : "—";
}
