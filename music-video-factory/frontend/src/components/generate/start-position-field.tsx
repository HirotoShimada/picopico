"use client";

import { Magnet } from "lucide-react";
import Link from "next/link";
import * as React from "react";

import { nearestGridPoint } from "@/components/audio/timeline";
import { Button } from "@/components/ui/button";
import { NumberInput } from "@/components/ui/number-input";
import { RadioCards, type RadioCardOption } from "@/components/ui/radio-cards";
import { Slider } from "@/components/ui/slider";
import { formatDuration, formatTime, formatTimePrecise } from "@/lib/format";
import type { AudioAnalysis, StartStrategy } from "@/lib/types";
import { clamp } from "@/lib/utils";

import { projectTabHref } from "./links";

export interface StartPositionFieldProps {
  projectId: string;
  analysis: AudioAnalysis | null;
  segment: { start: number; end: number } | null;
  /** Planned video length in seconds (`null` while invalid). */
  duration: number | null;
  strategy: StartStrategy;
  startSec: number | null;
  onChange: (patch: { startStrategy?: StartStrategy; startSec?: number | null }) => void;
  error?: string;
  disabled?: boolean;
}

/** Start strategy (segment / chorus / varied / custom) with a slider for a custom start. */
export function StartPositionField({
  projectId,
  analysis,
  segment,
  duration,
  strategy,
  startSec,
  onChange,
  error,
  disabled,
}: StartPositionFieldProps) {
  const songDuration = analysis?.duration ?? null;
  const chorus = analysis?.chorus_candidates.length
    ? [...analysis.chorus_candidates].sort((a, b) => b.score - a.score)[0]
    : null;
  const maxStart = songDuration !== null ? Math.max(0, songDuration - (duration ?? 0)) : null;

  const options: RadioCardOption<StartStrategy>[] = [
    {
      value: "segment",
      title: "選択した区間",
      description: segment
        ? `楽曲解析で選んだ区間（${formatTime(segment.start)}–${formatTime(segment.end)}）の先頭から始めます。`
        : "区間が未選択のため、解析の推奨区間から始めます。",
    },
    {
      value: "chorus",
      title: "サビから",
      description: chorus
        ? `検出したサビ候補（${formatTime(chorus.start)}〜）から始めます。`
        : "サビ候補が見つからない場合は推奨区間から始めます。",
    },
    {
      value: "varied",
      title: "動画ごとに変える",
      description: "推奨区間・サビ・Aメロなどから、動画ごとに異なる位置で始めます。",
    },
    { value: "custom", title: "位置を指定", description: "開始位置を秒単位で指定します。全動画で同じ位置になります。" },
  ];

  function setCustomStart(value: number | null) {
    onChange({ startSec: value === null ? null : Number(value.toFixed(2)) });
  }

  function chooseStrategy(next: StartStrategy) {
    if (next === "custom" && startSec === null) {
      onChange({ startStrategy: next, startSec: segment?.start ?? chorus?.start ?? 0 });
    } else {
      onChange({ startStrategy: next });
    }
  }

  const snapGrid = analysis?.downbeats.length ? analysis.downbeats : (analysis?.beats ?? []);
  const exceedsSong = startSec !== null && songDuration !== null && duration !== null && startSec + duration > songDuration + 0.01;

  return (
    <div className="flex flex-col gap-3">
      <RadioCards options={options} value={strategy} onValueChange={chooseStrategy} disabled={disabled} aria-label="開始位置の決め方" />
      <p className="text-2xs leading-4 text-subtle-foreground">
        区間は{" "}
        <Link href={projectTabHref(projectId, "analysis")} className="text-muted-foreground underline underline-offset-2 hover:text-foreground">
          楽曲解析タブ
        </Link>{" "}
        で変更できます。開始位置は小節の頭に合わせて調整されます。
      </p>

      {strategy === "custom" ? (
        <div className="flex flex-col gap-3 rounded-md border border-border bg-surface-raised/40 p-3.5">
          <div className="flex flex-wrap items-center gap-3">
            <NumberInput
              value={startSec}
              onValueChange={setCustomStart}
              min={0}
              max={maxStart ?? undefined}
              step={0.1}
              decimals={2}
              suffix="秒"
              className="w-32"
              aria-label="開始位置（秒）"
              aria-invalid={error ? true : undefined}
              disabled={disabled}
            />
            <span className="font-mono text-xs text-muted-foreground tabular">
              {formatTimePrecise(startSec)}
              {startSec !== null && duration !== null ? ` 〜 ${formatTimePrecise(startSec + duration)}（${formatDuration(duration)}）` : ""}
            </span>
            {snapGrid.length > 0 ? (
              <Button
                type="button"
                size="xs"
                variant="ghost"
                disabled={disabled || startSec === null}
                onClick={() => {
                  if (startSec === null) return;
                  const snapped = nearestGridPoint(snapGrid, startSec);
                  if (snapped !== null) setCustomStart(clamp(snapped, 0, maxStart ?? snapped));
                }}
              >
                <Magnet aria-hidden />
                小節の頭に合わせる
              </Button>
            ) : null}
          </div>
          {maxStart !== null && maxStart > 0 ? (
            <div className="flex flex-col gap-1">
              <Slider
                min={0}
                max={maxStart}
                step={0.1}
                value={[clamp(startSec ?? 0, 0, maxStart)]}
                onValueChange={([value]) => setCustomStart(value)}
                disabled={disabled}
                aria-label="開始位置"
              />
              <div className="flex justify-between font-mono text-2xs text-subtle-foreground tabular">
                <span>0:00</span>
                <span>{formatTime(maxStart)}</span>
              </div>
            </div>
          ) : null}
          {error ? (
            <p role="alert" className="text-xs text-danger-foreground">
              {error}
            </p>
          ) : exceedsSong ? (
            <p className="text-xs text-warning-foreground">曲の終わりを超えるため、開始位置は自動で前に調整されます。</p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
