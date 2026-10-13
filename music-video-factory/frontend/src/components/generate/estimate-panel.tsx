"use client";

import { Calculator, CircleCheck, CircleMinus, TriangleAlert } from "lucide-react";
import * as React from "react";

import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { errorMessage } from "@/lib/api";
import { providerLabel } from "@/lib/constants";
import { formatCount, formatDuration, formatPercent, formatPricePerSecond, formatUsd } from "@/lib/format";
import { isOpenRouter } from "@/lib/llm";
import type { Estimate, EstimateComparison } from "@/lib/types";
import { cn } from "@/lib/utils";

import type { EstimateState } from "./use-estimate";

/** Variety below this is flagged by the planner (§6.4). */
const VARIETY_WARNING_THRESHOLD = 0.35;
/** A slowest shot at or above this speed is shown as 等速 (natural speed). */
const NATURAL_SPEED = 0.995;

function isNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** True when the slowest shot plays at (about) natural speed: shown as 等速. */
export function isNaturalSpeed(speed: number): boolean {
  return speed >= NATURAL_SPEED;
}

/** Shots per video of the planned (fitted) cut: `5` or `3〜5`; null for an estimate without the field. */
export function shotsPerVideoRange(estimate: Pick<Estimate, "shots_per_video_min" | "shots_per_video_max">): string | null {
  const max = estimate.shots_per_video_max;
  if (!isNumber(max)) return null;
  const min = isNumber(estimate.shots_per_video_min) ? estimate.shots_per_video_min : max;
  return min === max ? formatCount(max) : `${formatCount(min)}〜${formatCount(max)}`;
}

/** Shots that reuse a clip inside a video, or null for an estimate without the field. */
export function repeatShots(estimate: Pick<Estimate, "repeat_shots">): number | null {
  return isNumber(estimate.repeat_shots) ? estimate.repeat_shots : null;
}

/**
 * How the repeated shots are shown, naming only what holds for every one of them (as the backend's
 * `repeat_warning` does): not back to back unless the pool has one clip, another section only when no
 * repeat shows footage of an earlier window, and always a punch-in framing.
 */
export function repeatHint(estimate: Pick<Estimate, "adjacent_repeat_shots" | "overlap_repeat_shots">): string {
  return [
    estimate.adjacent_repeat_shots === 0 ? "連続しない" : null,
    estimate.overlap_repeat_shots === 0 ? "別の区間" : null,
    "寄りの構図",
  ]
    .filter(Boolean)
    .join("・");
}

function Row({
  label,
  value,
  hint,
  strong = false,
  className,
}: {
  label: React.ReactNode;
  value: React.ReactNode;
  hint?: React.ReactNode;
  strong?: boolean;
  className?: string;
}) {
  return (
    <div className={cn("flex items-baseline justify-between gap-3 py-1", className)}>
      <dt className={cn("text-xs", strong ? "font-medium text-foreground" : "text-muted-foreground")}>
        {label}
        {hint ? <span className="ml-1 text-2xs text-subtle-foreground">{hint}</span> : null}
      </dt>
      <dd className={cn("text-right font-mono tabular", strong ? "text-sm font-semibold text-foreground" : "text-xs text-foreground")}>{value}</dd>
    </div>
  );
}

function UnverifiedBadge({ provider, asOf }: { provider: string; asOf?: string | null }) {
  return (
    <Badge
      tone="warning"
      title={
        isOpenRouter(provider)
          ? `${asOf ? `${asOf} 時点の ` : ""}OpenRouter のモデル一覧（最新の一覧を取得できないときは組み込みの写し）の料金に基づく価格です。実際の API キーでは未検証です`
          : "実際の API キーでの検証前の公式料金表に基づく価格です"
      }
    >
      <TriangleAlert aria-hidden />
      未検証
    </Badge>
  );
}

function savingsPercent(estimate: Estimate): number | null {
  return estimate.naive_total_usd > 0 ? estimate.savings_usd / estimate.naive_total_usd : null;
}

export interface EstimatePanelProps {
  state: EstimateState;
  /** Why no estimate is requested (blocking precondition), if any. */
  unavailableReason?: React.ReactNode;
  /** Where the budget comes from when the form leaves it empty. */
  budgetSource?: string;
  /** Apply `Estimate.budget_fit` (set the new-clip cap to `maxNewClips`; same seed = the same plan). */
  onApplyMaxNewClips?: (maxNewClips: number) => void;
  className?: string;
}

/** Live cost estimate: new vs cached clips, price, LLM, total, savings, budget, shots per video and variety. */
export function EstimatePanel({ state, unavailableReason, budgetSource, onApplyMaxNewClips, className }: EstimatePanelProps) {
  const { data: estimate, error, stale } = state;

  return (
    <Card className={className} aria-busy={stale || undefined}>
      <CardHeader>
        <CardTitle>
          <Calculator className="size-4 text-subtle-foreground" aria-hidden />
          費用の見積もり
        </CardTitle>
        <CardDescription>設定を変えると自動で再計算します</CardDescription>
        <CardAction>{stale && !unavailableReason ? <Spinner label="再計算中" /> : null}</CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {unavailableReason ? (
          <p className="text-sm leading-6 text-muted-foreground">{unavailableReason}</p>
        ) : error ? (
          <Alert
            tone="danger"
            title="見積もりを取得できませんでした"
            action={
              <Button size="xs" variant="secondary" onClick={state.retry}>
                再試行
              </Button>
            }
          >
            {errorMessage(error)}
          </Alert>
        ) : !estimate ? (
          <div className="flex flex-col gap-3">
            <Skeleton className="h-9 w-32" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-24 w-full" />
          </div>
        ) : (
          <EstimateBody estimate={estimate} stale={stale} budgetSource={budgetSource} onApplyMaxNewClips={onApplyMaxNewClips} />
        )}
      </CardContent>
    </Card>
  );
}

function EstimateBody({
  estimate,
  stale,
  budgetSource,
  onApplyMaxNewClips,
}: {
  estimate: Estimate;
  stale: boolean;
  budgetSource?: string;
  onApplyMaxNewClips?: (maxNewClips: number) => void;
}) {
  const limit = estimate.budget_limit_usd;
  const usage = limit && limit > 0 ? Math.min(1, estimate.total_usd / limit) : null;
  const savings = savingsPercent(estimate);
  // Variety is cross-video (§6.4): a single video has no bar, only a warning if the planner gives one.
  const variety = estimate.videos > 1 && isNumber(estimate.variety_score) ? estimate.variety_score : null;
  const unverified = estimate.pricing_verified === false && estimate.price_per_second_usd > 0;
  const shotsRange = shotsPerVideoRange(estimate);
  const beatMax = estimate.beat_shots_per_video_max;
  const recut =
    isNumber(estimate.fitted_videos) && estimate.fitted_videos > 0 && isNumber(beatMax) && isNumber(estimate.shots_per_video_max) && beatMax > estimate.shots_per_video_max;
  const repeats = repeatShots(estimate);
  const budgetFit = !estimate.within_budget ? (estimate.budget_fit ?? null) : null;
  // Shown on their own lines: keep them out of the notes list (the variety warning used to appear twice).
  const shownElsewhere = new Set([estimate.variety_warning, estimate.repeat_warning, estimate.budget_fit?.note].filter(Boolean));
  const notes = estimate.notes.filter((note) => !shownElsewhere.has(note));

  return (
    <div className={cn("flex flex-col gap-4 transition-opacity", stale && "opacity-50")}>
      <div className="flex flex-col gap-2">
        <div className="flex items-end justify-between gap-3">
          <div>
            <p className="text-xs text-muted-foreground">合計（概算）</p>
            <p className="font-mono text-3xl font-semibold tracking-tight text-foreground tabular">{formatUsd(estimate.total_usd)}</p>
          </div>
          {estimate.within_budget ? (
            <Badge tone="success">
              <CircleCheck aria-hidden />
              予算内
            </Badge>
          ) : (
            <Badge tone="danger">
              <TriangleAlert aria-hidden />
              予算超過
            </Badge>
          )}
        </div>
        {limit !== null && limit !== undefined ? (
          <>
            <Progress value={usage === null ? 0 : usage * 100} tone={estimate.within_budget ? "neutral" : "danger"} size="sm" aria-label="予算に対する見積もり" />
            <p className="flex justify-between text-2xs text-subtle-foreground tabular">
              <span>予算上限 {formatUsd(limit)}{budgetSource ? `（${budgetSource}）` : ""}</span>
              {usage !== null ? <span>{formatPercent(estimate.total_usd / limit)}</span> : null}
            </p>
          </>
        ) : (
          <p className="text-2xs text-subtle-foreground">予算上限なし</p>
        )}
        {budgetFit ? (
          <div className="flex items-start justify-between gap-3 rounded-md border border-border bg-surface-raised/50 px-3 py-2">
            <p className="text-xs leading-5 text-muted-foreground">{budgetFit.note}</p>
            {onApplyMaxNewClips ? (
              // A stale estimate's cap was priced for the previous settings: only a current one can be applied.
              <Button
                size="xs"
                variant="secondary"
                className="shrink-0"
                disabled={stale}
                onClick={() => onApplyMaxNewClips(budgetFit.max_new_clips)}
              >
                上限を{formatCount(budgetFit.max_new_clips)}本にする
              </Button>
            ) : null}
          </div>
        ) : null}
      </div>

      <dl className="flex flex-col">
        <Row label="動画" value={`${formatCount(estimate.videos)}本 ・ ${formatCount(estimate.shots_total)}ショット`} />
        {/* Fit to the clips (§6.4): each row only when the backend reports the field (older estimates do not). */}
        {shotsRange !== null ? (
          <Row label="1本あたりのショット" value={`${shotsRange}ショット${recut ? `（通常 ${formatCount(beatMax)}）` : ""}`} />
        ) : null}
        {/* The backend rounds both as its notes print them (0.1 s, 0.01x), so toFixed shows the same digits. */}
        {isNumber(estimate.max_shot_sec) && isNumber(estimate.min_speed) ? (
          <Row
            label="ショットの長さ・速度"
            value={`最長 ${estimate.max_shot_sec.toFixed(1)}秒 ・ ${isNaturalSpeed(estimate.min_speed) ? "等速" : `最も遅い ${estimate.min_speed.toFixed(2)}倍速`}`}
          />
        ) : null}
        {repeats !== null ? (
          <Row
            label="動画内の同じ素材"
            value={repeats > 0 ? <span className="whitespace-nowrap text-warning-foreground">{`${formatCount(repeats)}ショット`}</span> : "なし"}
            hint={repeats > 0 ? repeatHint(estimate) : undefined}
          />
        ) : null}
        <Row label="使用する素材" value={`${formatCount(estimate.unique_clips)}本`} />
        <Row
          label="新規生成"
          value={`${formatCount(estimate.new_clips)}本 × ${formatDuration(estimate.clip_duration)}`}
        />
        <Row label="生成済み素材の再利用" value={`${formatCount(estimate.cached_clips)}本`} hint="費用なし" />
        <div className="flex items-baseline justify-between gap-3 py-1">
          <dt className="flex items-center gap-1.5 text-xs text-muted-foreground">
            単価
            {unverified ? <UnverifiedBadge provider={estimate.provider} asOf={estimate.as_of} /> : null}
          </dt>
          <dd className="text-right font-mono text-xs text-foreground tabular">{formatPricePerSecond(estimate.price_per_second_usd)}</dd>
        </div>
        {/* Separators are row borders: a <dl> may only contain dt/dd groups (axe definition-list). */}
        <Row label="動画生成" value={formatUsd(estimate.video_generation_usd)} className="mt-1.5 border-t border-border pt-2.5" />
        <Row label="LLM（絵コンテ）" value={formatUsd(estimate.llm_usd)} />
        <Row label="合計" value={formatUsd(estimate.total_usd)} strong />
        <Row
          label="全ショットを新規生成した場合"
          value={formatUsd(estimate.naive_total_usd)}
          className="mt-1.5 border-t border-border pt-2.5"
        />
        <Row
          label="素材の使い回しによる節約"
          value={
            estimate.savings_usd > 0 ? `${formatUsd(estimate.savings_usd)}${savings !== null ? `（${formatPercent(savings)}）` : ""}` : "—"
          }
        />
      </dl>

      {estimate.repeat_warning ? (
        <p className="flex items-start gap-1.5 text-xs leading-5 text-warning-foreground">
          <TriangleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          {estimate.repeat_warning}
        </p>
      ) : null}

      {variety !== null || estimate.variety_warning ? (
        <div className="flex flex-col gap-1.5">
          {variety !== null ? (
            <>
              <div className="flex items-baseline justify-between text-xs">
                <span className="text-muted-foreground">動画間の映像バリエーション</span>
                <span className="font-mono text-foreground tabular">{formatPercent(variety)}</span>
              </div>
              <Progress
                value={variety * 100}
                tone={variety < VARIETY_WARNING_THRESHOLD ? "danger" : "neutral"}
                size="sm"
                aria-label="動画間の映像バリエーション"
              />
            </>
          ) : null}
          {estimate.variety_warning ? (
            <p className="flex items-start gap-1.5 text-xs leading-5 text-warning-foreground">
              <TriangleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden />
              {estimate.variety_warning}
            </p>
          ) : null}
        </div>
      ) : null}

      {notes.length > 0 ? (
        <ul className="flex list-disc flex-col gap-1 pl-4 text-2xs leading-4 text-muted-foreground marker:text-subtle-foreground">
          {notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export interface ProviderComparisonProps {
  estimate: Estimate | undefined;
  stale: boolean;
  /** Switch the form to this provider/model. */
  onSelect: (row: EstimateComparison) => void;
  className?: string;
}

/** The same plan priced on every provider/model (`Estimate.comparison`). */
export function ProviderComparison({ estimate, stale, onSelect, className }: ProviderComparisonProps) {
  const rows = estimate?.comparison ?? [];
  return (
    <Card className={className}>
      <CardHeader>
        <CardTitle>プロバイダー比較</CardTitle>
        <CardDescription>同じ構成を各モデルで生成した場合の概算</CardDescription>
      </CardHeader>
      <CardContent className="px-0 pb-2">
        {rows.length === 0 ? (
          <p className="px-5 pb-3 text-xs text-muted-foreground">見積もりの取得後に表示されます。</p>
        ) : (
          <ul className={cn("flex flex-col transition-opacity", stale && "opacity-50")}>
            {rows.map((row) => {
              const [providerName, modelName] = (row.name ?? "").split(" / ");
              const selected = row.selected ?? (row.provider === estimate?.provider && row.model === estimate?.model);
              return (
                <li
                  key={`${row.provider}:${row.model}`}
                  className={cn("flex items-center gap-3 border-t border-border px-5 py-2", selected && "bg-surface-raised")}
                >
                  <div className="flex min-w-0 flex-1 flex-col">
                    <span className="truncate text-xs font-medium text-foreground">
                      {providerName || providerLabel(row.provider)}
                    </span>
                    <span className="flex items-center gap-1.5 truncate text-2xs text-subtle-foreground">
                      {modelName || row.model}
                      {row.price_per_second_usd !== undefined ? <span className="font-mono">{formatPricePerSecond(row.price_per_second_usd)}</span> : null}
                      {row.verified === false && (row.price_per_second_usd ?? 0) > 0 ? <span className="text-warning-foreground">未検証</span> : null}
                    </span>
                  </div>
                  <span className="font-mono text-xs text-foreground tabular">{formatUsd(row.total_usd)}</span>
                  <div className="flex w-16 justify-end">
                    {selected ? (
                      <Badge tone="outline">選択中</Badge>
                    ) : row.configured ? (
                      <Button size="xs" variant="ghost" onClick={() => onSelect(row)}>
                        選択
                      </Button>
                    ) : (
                      <span className="flex items-center gap-1 text-2xs text-subtle-foreground" title="API キーが未設定です">
                        <CircleMinus className="size-3" aria-hidden />
                        未設定
                      </span>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
