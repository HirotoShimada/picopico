import type { AnalysisStatus, BatchStatus, ClipStatus, VideoStatus } from "./types";

/**
 * Visual tone of a status. `progress` is the only tone drawn in the primary
 * blue (work in flight); finished states use success/warning/danger.
 */
export type StatusTone = "neutral" | "progress" | "success" | "warning" | "danger";

export interface StatusMeta {
  label: string;
  tone: StatusTone;
  /** True while work is in flight (polling should continue). */
  active: boolean;
}

export const BATCH_STATUS_META: Record<BatchStatus, StatusMeta> = {
  queued: { label: "待機中", tone: "neutral", active: true },
  planning: { label: "構成を計画中", tone: "progress", active: true },
  generating_clips: { label: "素材を生成中", tone: "progress", active: true },
  rendering: { label: "レンダリング中", tone: "progress", active: true },
  completed: { label: "完了", tone: "success", active: false },
  partial: { label: "一部失敗", tone: "warning", active: false },
  failed: { label: "失敗", tone: "danger", active: false },
  cancelled: { label: "キャンセル済み", tone: "neutral", active: false },
  budget_exceeded: { label: "予算超過で停止", tone: "warning", active: false },
};

export const VIDEO_STATUS_META: Record<VideoStatus, StatusMeta> = {
  queued: { label: "待機中", tone: "neutral", active: true },
  waiting_clips: { label: "素材待ち", tone: "neutral", active: true },
  rendering: { label: "レンダリング中", tone: "progress", active: true },
  done: { label: "完成", tone: "success", active: false },
  failed: { label: "失敗", tone: "danger", active: false },
  cancelled: { label: "キャンセル済み", tone: "neutral", active: false },
};

export const CLIP_STATUS_META: Record<ClipStatus, StatusMeta> = {
  pending: { label: "生成待ち", tone: "neutral", active: true },
  generating: { label: "生成中", tone: "progress", active: true },
  ready: { label: "利用可能", tone: "success", active: false },
  failed: { label: "失敗", tone: "danger", active: false },
  cancelled: { label: "キャンセル済み", tone: "neutral", active: false },
};

export const ANALYSIS_STATUS_META: Record<AnalysisStatus, StatusMeta> = {
  none: { label: "未解析", tone: "neutral", active: false },
  queued: { label: "解析待ち", tone: "neutral", active: true },
  running: { label: "解析中", tone: "progress", active: true },
  done: { label: "解析済み", tone: "success", active: false },
  failed: { label: "解析失敗", tone: "danger", active: false },
};

export const STATUS_META = {
  batch: BATCH_STATUS_META,
  video: VIDEO_STATUS_META,
  clip: CLIP_STATUS_META,
  analysis: ANALYSIS_STATUS_META,
} as const;

export type StatusKind = keyof typeof STATUS_META;

const UNKNOWN_STATUS: StatusMeta = { label: "不明", tone: "neutral", active: false };

/** Metadata for a status value; tolerates values added server-side later. */
export function statusMeta(kind: StatusKind, status: string): StatusMeta {
  return (STATUS_META[kind] as Record<string, StatusMeta>)[status] ?? { ...UNKNOWN_STATUS, label: status };
}

export const ACTIVE_BATCH_STATUSES: readonly BatchStatus[] = ["queued", "planning", "generating_clips", "rendering"];
export const ACTIVE_VIDEO_STATUSES: readonly VideoStatus[] = ["queued", "waiting_clips", "rendering"];
export const ACTIVE_CLIP_STATUSES: readonly ClipStatus[] = ["pending", "generating"];
export const ACTIVE_ANALYSIS_STATUSES: readonly AnalysisStatus[] = ["queued", "running"];

export function isBatchActive(status: BatchStatus | string | null | undefined): boolean {
  return !!status && (ACTIVE_BATCH_STATUSES as readonly string[]).includes(status);
}

export function isVideoActive(status: VideoStatus | string | null | undefined): boolean {
  return !!status && (ACTIVE_VIDEO_STATUSES as readonly string[]).includes(status);
}

export function isClipActive(status: ClipStatus | string | null | undefined): boolean {
  return !!status && (ACTIVE_CLIP_STATUSES as readonly string[]).includes(status);
}

export function isAnalysisActive(status: AnalysisStatus | string | null | undefined): boolean {
  return !!status && (ACTIVE_ANALYSIS_STATUSES as readonly string[]).includes(status);
}
