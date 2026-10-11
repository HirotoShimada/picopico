"use client";

import { Ban, Check, CircleAlert, LoaderCircle } from "lucide-react";
import * as React from "react";

import { Card, CardContent } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { formatCount, formatDateTime, formatDuration, formatPercent, formatUsd } from "@/lib/format";
import { isBatchActive } from "@/lib/status";
import type { BatchDetailOut, BatchStatus } from "@/lib/types";
import { cn } from "@/lib/utils";

const STAGES: { key: "planning" | "generating_clips" | "rendering" | "finished"; label: string }[] = [
  { key: "planning", label: "構成の計画" },
  { key: "generating_clips", label: "素材の生成" },
  { key: "rendering", label: "レンダリング" },
  { key: "finished", label: "完了" },
];

function stageIndex(status: BatchStatus): number {
  switch (status) {
    case "queued":
    case "planning":
      return 0;
    case "generating_clips":
      return 1;
    case "rendering":
      return 2;
    default:
      return 3;
  }
}

/** Rendering progress over all videos: finished ones count fully, rendering ones by their progress. */
export function videoProgressFraction(batch: BatchDetailOut): number | null {
  const videos = batch.videos ?? [];
  const total = videos.length || batch.counts.total;
  if (total === 0) return null;
  if (videos.length === 0) return (batch.counts.done + batch.counts.failed + batch.counts.cancelled) / total;
  const sum = videos.reduce((acc, video) => {
    if (video.status === "done" || video.status === "failed" || video.status === "cancelled") return acc + 1;
    if (video.status === "rendering") return acc + Math.min(1, Math.max(0, video.progress));
    return acc;
  }, 0);
  return sum / total;
}

/** Wall clock refreshed every second while `running` (0 before the first tick). */
function useClock(running: boolean): number {
  const [now, setNow] = React.useState(0);
  React.useEffect(() => {
    if (!running) return;
    const update = () => setNow(Date.now());
    const first = window.setTimeout(update, 0);
    const timer = window.setInterval(update, 1000);
    return () => {
      window.clearTimeout(first);
      window.clearInterval(timer);
    };
  }, [running]);
  return now;
}

/** Whole seconds between two instants as `1分05秒`, or `—`. */
function elapsedLabel(startIso: string, endMs: number | null): string {
  const start = new Date(startIso).getTime();
  if (!endMs || Number.isNaN(start)) return "—";
  return formatDuration(Math.max(0, Math.floor((endMs - start) / 1000)));
}

const FINAL_LABELS: Partial<Record<BatchStatus, string>> = {
  completed: "完了",
  partial: "完了（一部失敗）",
  failed: "失敗",
  cancelled: "キャンセル",
  budget_exceeded: "予算超過で停止",
};

type StepState = "done" | "current" | "stopped" | "pending";

/**
 * Per-stage state. While running, earlier stages are done; once the batch
 * stopped, a stage is ticked only when its work actually finished.
 */
function stepStates(batch: BatchDetailOut): StepState[] {
  const { status, counts, clip_counts: clips } = batch;
  if (isBatchActive(status)) {
    const current = stageIndex(status);
    return STAGES.map((_, index) => (index < current ? "done" : index === current ? "current" : "pending"));
  }
  if (status === "completed" || status === "partial") return STAGES.map(() => "done");
  const planned = (batch.videos?.length ?? 0) > 0 || clips.total > 0;
  const clipsDone = clips.total > 0 && clips.generating === 0 && clips.ready + clips.failed >= clips.total;
  const rendered = counts.total > 0 && counts.done + counts.failed === counts.total;
  return [planned ? "done" : "pending", clipsDone ? "done" : "pending", rendered ? "done" : "pending", "stopped"];
}

function StageSteps({ batch }: { batch: BatchDetailOut }) {
  const states = stepStates(batch);
  const stopTone = batch.status === "cancelled" ? "neutral" : "danger";
  return (
    <ol className="flex flex-wrap items-center gap-x-2 gap-y-2" aria-label="処理の段階">
      {STAGES.map((stage, index) => {
        const state = states[index];
        const last = index === STAGES.length - 1;
        const label = last ? (FINAL_LABELS[batch.status] ?? stage.label) : stage.label;
        return (
          <li key={stage.key} className="flex items-center gap-2">
            <span
              className={cn(
                "flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs",
                state === "current"
                  ? "border-primary/50 bg-primary-soft text-blue-200"
                  : state === "done"
                    ? "border-border-strong text-foreground"
                    : state === "stopped"
                      ? stopTone === "danger"
                        ? "border-danger/40 text-danger-foreground"
                        : "border-border-strong text-muted-foreground"
                      : "border-border text-subtle-foreground",
              )}
              aria-current={state === "current" ? "step" : undefined}
            >
              {state === "current" ? (
                <LoaderCircle className="size-3.5 animate-spin" aria-hidden />
              ) : state === "done" ? (
                <Check className="size-3.5" aria-hidden />
              ) : state === "stopped" ? (
                batch.status === "cancelled" ? <Ban className="size-3.5" aria-hidden /> : <CircleAlert className="size-3.5" aria-hidden />
              ) : (
                <span className="font-mono text-2xs">{index + 1}</span>
              )}
              {label}
            </span>
            {!last ? <span className="h-px w-4 bg-border" aria-hidden /> : null}
          </li>
        );
      })}
    </ol>
  );
}

function Metric({ label, value, hint, tone }: { label: string; value: React.ReactNode; hint?: React.ReactNode; tone?: "danger" }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5 px-4 py-3 first:pl-0">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className={cn("font-mono text-lg font-semibold leading-7 tabular", tone === "danger" ? "text-danger-foreground" : "text-foreground")}>
        {value}
      </dd>
      {hint ? <dd className="truncate text-2xs text-subtle-foreground">{hint}</dd> : null}
    </div>
  );
}

/** Stage steps, clip/render progress bars and the key counters of a batch. */
export function BatchProgress({ batch }: { batch: BatchDetailOut }) {
  const active = isBatchActive(batch.status);
  const now = useClock(active);
  const { counts, clip_counts: clips } = batch;
  const newClipsTotal = Math.max(0, clips.total - clips.reused);
  const newClipsReady = Math.max(0, clips.ready - clips.reused);
  const clipFraction = newClipsTotal > 0 ? (newClipsReady + clips.failed) / newClipsTotal : clips.total > 0 ? 1 : null;
  // While running: finished + in-flight progress; once stopped: share of completed videos.
  const renderFraction = active ? videoProgressFraction(batch) : counts.total > 0 ? counts.done / counts.total : null;
  const estimateTotal = batch.estimate?.total_usd ?? null;
  const elapsedEnd = batch.finished_at ? new Date(batch.finished_at).getTime() : now > 0 ? now : null;
  const waiting = Math.max(0, counts.total - counts.done - counts.rendering - counts.failed - counts.cancelled);

  return (
    <Card className="mb-6">
      <CardContent className="flex flex-col gap-5 pt-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <StageSteps batch={batch} />
          <p className="text-xs text-muted-foreground tabular">
            開始 {formatDateTime(batch.started_at ?? batch.created_at)}
            {batch.started_at ? ` ・ ${active ? "経過" : "所要"} ${elapsedLabel(batch.started_at, elapsedEnd)}` : ""}
          </p>
        </div>

        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <div className="flex flex-col gap-1.5">
            <div className="flex items-baseline justify-between text-xs">
              <span className="text-muted-foreground">素材の生成</span>
              <span className="font-mono text-foreground tabular">
                {newClipsTotal > 0 ? `${formatCount(newClipsReady)} / ${formatCount(newClipsTotal)} 本` : clips.total > 0 ? "新規生成なし" : "計画中"}
              </span>
            </div>
            <Progress
              value={batch.status === "queued" || batch.status === "planning" ? null : clipFraction === null ? 0 : clipFraction * 100}
              tone={clips.failed > 0 && !active ? "danger" : "primary"}
              aria-label="素材の生成の進捗"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <div className="flex items-baseline justify-between text-xs">
              <span className="text-muted-foreground">動画のレンダリング</span>
              <span className="font-mono text-foreground tabular">
                {formatCount(counts.done)} / {formatCount(counts.total)} 本{active && renderFraction !== null ? `（${formatPercent(renderFraction)}）` : ""}
              </span>
            </div>
            <Progress value={renderFraction === null ? 0 : renderFraction * 100} aria-label="動画のレンダリングの進捗" />
          </div>
        </div>

        <dl className="grid grid-cols-2 divide-border border-t border-border sm:grid-cols-3 lg:grid-cols-6 lg:divide-x">
          <Metric label="完成" value={`${formatCount(counts.done)} / ${formatCount(counts.total)}`} />
          <Metric label="レンダリング中" value={formatCount(counts.rendering)} />
          <Metric label="待機中" value={formatCount(waiting)} hint="素材待ち・順番待ち" />
          <Metric
            label="失敗"
            value={formatCount(counts.failed)}
            hint={counts.cancelled > 0 ? `キャンセル ${formatCount(counts.cancelled)}` : undefined}
            tone={counts.failed > 0 ? "danger" : undefined}
          />
          <Metric
            label="素材"
            value={`${formatCount(newClipsReady)} 生成`}
            hint={`再利用 ${formatCount(clips.reused)} ・ 生成中 ${formatCount(clips.generating)}${clips.failed > 0 ? ` ・ 失敗 ${formatCount(clips.failed)}` : ""}`}
          />
          <Metric
            label="費用（これまで）"
            value={formatUsd(batch.cost_usd)}
            hint={estimateTotal !== null ? `見積もり ${formatUsd(estimateTotal)}` : undefined}
          />
        </dl>
      </CardContent>
    </Card>
  );
}
