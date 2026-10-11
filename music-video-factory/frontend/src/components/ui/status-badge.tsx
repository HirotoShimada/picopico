import { Ban, CircleAlert, CircleCheck, CircleDashed, LoaderCircle, TriangleAlert } from "lucide-react";
import * as React from "react";

import { statusMeta, type StatusKind, type StatusTone } from "@/lib/status";
import type { AnalysisStatus, BatchStatus, ClipStatus, VideoStatus } from "@/lib/types";

import { Badge } from "./badge";

type StatusBadgeProps = (
  | { kind: "batch"; status: BatchStatus }
  | { kind: "video"; status: VideoStatus }
  | { kind: "clip"; status: ClipStatus }
  | { kind: "analysis"; status: AnalysisStatus }
) & { className?: string; showIcon?: boolean; /** Overrides the status label (e.g. "一部キャンセル"). */ label?: string };

const TONE_ICONS: Record<StatusTone, React.ComponentType<{ className?: string }>> = {
  neutral: CircleDashed,
  progress: LoaderCircle,
  success: CircleCheck,
  warning: TriangleAlert,
  danger: CircleAlert,
};

/** Job / video / clip / analysis status as a labelled badge (icon + Japanese label). */
function StatusBadge({ kind, status, className, showIcon = true, label }: StatusBadgeProps) {
  const meta = statusMeta(kind as StatusKind, status);
  const Icon = status === "cancelled" ? Ban : TONE_ICONS[meta.tone];
  return (
    <Badge tone={meta.tone} className={className} data-status={status}>
      {showIcon ? <Icon className={meta.tone === "progress" ? "animate-spin" : undefined} aria-hidden /> : null}
      {label ?? meta.label}
    </Badge>
  );
}

export { StatusBadge };
