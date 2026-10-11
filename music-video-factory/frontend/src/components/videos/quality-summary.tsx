import { CircleAlert, CircleCheck, ShieldCheck, TriangleAlert } from "lucide-react";
import * as React from "react";

import { Badge } from "@/components/ui/badge";
import { formatPercent } from "@/lib/format";
import type { QualityCheck, QualityReport } from "@/lib/types";
import { cn } from "@/lib/utils";

/** Check names from `quality.validate_output` (§6.6). */
const CHECK_LABELS: Record<string, string> = {
  readable: "ファイルの読み込み",
  container: "コンテナ（MP4）",
  video_codec: "映像コーデック（H.264）",
  pix_fmt: "画素形式（yuv420p）",
  resolution: "解像度（1080×1920）",
  fps: "フレームレート（30fps）",
  duration: "長さ",
  audio_codec: "音声コーデック（AAC）",
  audio_duration: "音声の長さ",
  faststart: "Web 再生向け配置（faststart）",
  black_frames: "黒い画面の割合",
  loudness: "ラウドネス（参考値）",
};

export function qualityCheckLabel(name: string): string {
  return CHECK_LABELS[name] ?? name;
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(2);
  if (typeof value === "boolean") return value ? "はい" : "いいえ";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function failedChecks(report: QualityReport): QualityCheck[] {
  return report.checks.filter((check) => !check.passed);
}

/** One-badge summary for cards: 品質OK / 要確認 (n). */
export function QualityBadge({ report, className }: { report: QualityReport | null | undefined; className?: string }) {
  if (!report) return null;
  const failed = failedChecks(report).length;
  return report.passed ? (
    <Badge tone="success" className={className} title="出力ファイルの自動チェックに合格">
      <ShieldCheck aria-hidden />
      品質OK
    </Badge>
  ) : (
    <Badge tone="warning" className={className} title="出力ファイルの自動チェックで問題が見つかりました">
      <TriangleAlert aria-hidden />
      要確認{failed > 0 ? ` ${failed}` : ""}
    </Badge>
  );
}

/** Full quality report: failed checks first, loudness and black-frame ratio. */
export function QualitySummary({ report, className }: { report: QualityReport | null | undefined; className?: string }) {
  if (!report) {
    return <p className={cn("text-xs text-muted-foreground", className)}>品質チェックの結果はまだありません。</p>;
  }
  const checks = [...report.checks].sort((a, b) => Number(a.passed) - Number(b.passed));
  return (
    <div className={cn("flex flex-col gap-2.5", className)}>
      <div className="flex flex-wrap items-center gap-2">
        <QualityBadge report={report} />
        {report.loudness_lufs !== null && report.loudness_lufs !== undefined ? (
          <span className="text-xs text-muted-foreground tabular">ラウドネス {report.loudness_lufs.toFixed(1)} LUFS</span>
        ) : null}
        {report.black_ratio !== null && report.black_ratio !== undefined ? (
          <span className="text-xs text-muted-foreground tabular">黒画面 {formatPercent(report.black_ratio, 1)}</span>
        ) : null}
      </div>
      <ul className="flex flex-col gap-1">
        {checks.map((check) => (
          <li key={check.name} className="flex items-start gap-2 text-xs leading-5">
            {check.passed ? (
              <CircleCheck className="mt-0.5 size-3.5 shrink-0 text-success" aria-label="合格" />
            ) : (
              <CircleAlert className="mt-0.5 size-3.5 shrink-0 text-danger" aria-label="不合格" />
            )}
            <span className={cn("min-w-0 flex-1", check.passed ? "text-muted-foreground" : "text-foreground")}>{qualityCheckLabel(check.name)}</span>
            <span className="max-w-[45%] truncate text-right font-mono text-2xs text-subtle-foreground" title={`値: ${formatValue(check.value)} / 期待値: ${formatValue(check.expected)}`}>
              {formatValue(check.value)}
              {!check.passed && check.expected !== null && check.expected !== undefined ? ` ≠ ${formatValue(check.expected)}` : ""}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
