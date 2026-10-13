"use client";

import { FlipHorizontal2, Play, Replace, RotateCcw } from "lucide-react";
import * as React from "react";

import { panLabel } from "@/components/generate/vocab";
import { clipSourceLabel } from "@/components/materials/clip-labels";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { Thumbnail } from "@/components/ui/thumbnail";
import { formatDuration, formatTimePrecise } from "@/lib/format";
import type { ClipOut, ShotPlan } from "@/lib/types";
import { cn } from "@/lib/utils";

import { shotSourceSeconds } from "./clip-picker-dialog";

export interface ShotListProps {
  /** Effective shots (draft applied). */
  shots: readonly ShotPlan[];
  baseShots: readonly ShotPlan[];
  clipsById: ReadonlyMap<string, ClipOut>;
  /** Player position relative to the render, to highlight the current shot. */
  playerTime: number | null;
  onSwap: (index: number) => void;
  onReset: (index: number) => void;
  onSeek: (renderSeconds: number) => void;
}

function describeMotion(shot: ShotPlan): string {
  const parts: string[] = [];
  if (Math.abs(shot.zoom_to - shot.zoom_from) > 0.005) parts.push(shot.zoom_to > shot.zoom_from ? "ズームイン" : "ズームアウト");
  if (shot.pan && shot.pan !== "center") parts.push(panLabel(shot.pan));
  if (Math.abs((shot.speed || 1) - 1) > 0.01) parts.push(`${(shot.speed || 1).toFixed(2)}倍速`);
  return parts.join(" ・ ") || "固定";
}

/** Source windows of one clip closer than this (seconds) show the same footage (planner REPEAT_GAP_SEC). */
const REPEAT_GAP_SEC = 0.5;

/**
 * Per shot: does it show footage an earlier shot of this video already showed? A generated clip repeats
 * whenever it was used before; an uploaded clip only when its source window comes within REPEAT_GAP_SEC
 * of an earlier window of it (a long upload gives disjoint windows), as the planner counts repeats (§6.4).
 */
export function repeatedShots(shots: readonly ShotPlan[], clipsById: ReadonlyMap<string, ClipOut>): boolean[] {
  const windows = new Map<string, [number, number][]>();
  const gap = REPEAT_GAP_SEC - 1e-6; // windows exactly the gap apart are disjoint
  return shots.map((shot) => {
    if (!shot.clip_id) return false;
    const window: [number, number] = [shot.src_in, shotSourceSeconds(shot)];
    const earlier = windows.get(shot.clip_id) ?? [];
    windows.set(shot.clip_id, [...earlier, window]);
    if (clipsById.get(shot.clip_id)?.source !== "upload") return earlier.length > 0;
    return earlier.some(([start, end]) => window[0] < end + gap && start - gap < window[1]);
  });
}

/** Shots in order with their clip thumbnail, timing and motion; each clip can be swapped. */
export function ShotList({ shots, baseShots, clipsById, playerTime, onSwap, onReset, onSeek }: ShotListProps) {
  if (shots.length === 0) {
    return <EmptyState size="sm" title="ショット情報がありません" description="この動画の構成（プラン）がまだ作成されていません。" />;
  }

  const starts = shots.reduce<number[]>((acc, shot, index) => [...acc, index === 0 ? 0 : acc[index - 1] + shots[index - 1].duration], []);
  const repeats = repeatedShots(shots, clipsById);
  return (
    <ol className="flex flex-col divide-y divide-border rounded-md border border-border">
      {shots.map((shot, index) => {
        const begin = starts[index];
        const clip = shot.clip_id ? clipsById.get(shot.clip_id) : undefined;
        const changed = baseShots[index]?.clip_id !== shot.clip_id;
        const active = playerTime !== null && playerTime >= begin && playerTime < begin + shot.duration;
        const short = clip?.duration !== null && clip?.duration !== undefined && clip.duration < shotSourceSeconds(shot);
        const repeated = repeats[index];
        return (
          <li key={index} className={cn("flex items-center gap-3 px-3 py-2.5", active && "bg-surface-raised")}>
            <span className="w-6 shrink-0 text-right font-mono text-xs text-subtle-foreground">{index + 1}</span>
            <button
              type="button"
              onClick={() => onSeek(begin)}
              className="focus-ring-custom group relative w-12 shrink-0 cursor-pointer rounded outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
              aria-label={`ショット ${index + 1} から再生`}
              title="現在のレンダリングでこのショットから再生"
            >
              <Thumbnail
                src={clip?.thumbnail_url}
                alt={`ショット ${index + 1} の素材`}
                className={cn("rounded", shot.hflip && "[&_img]:-scale-x-100")}
                overlay={
                  <span className="absolute inset-0 flex items-center justify-center bg-black/40 opacity-0 transition-opacity group-hover:opacity-100">
                    <Play className="size-3.5 fill-current text-white" aria-hidden />
                  </span>
                }
              />
            </button>
            <div className="flex min-w-0 flex-1 flex-col gap-0.5">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="font-mono text-xs text-foreground tabular">
                  {formatTimePrecise(begin)}–{formatTimePrecise(begin + shot.duration)}
                </span>
                <span className="text-2xs text-subtle-foreground">{formatDuration(shot.duration)}</span>
                {changed ? <Badge tone="outline">変更あり</Badge> : null}
                {!changed && shot.substituted_from ? (
                  <Badge tone="neutral" title="予定していた素材を生成できなかったため、別の素材で補っています">
                    代替素材
                  </Badge>
                ) : null}
                {short ? <Badge tone="warning">素材が短い</Badge> : null}
                {repeated ? (
                  <Badge tone="warning" title="この動画の前のショットでも使われている素材です">
                    同じ素材
                  </Badge>
                ) : null}
              </div>
              <span className="truncate text-2xs text-muted-foreground">
                {clip ? [clip.scene_id, clipSourceLabel(clip)].filter(Boolean).join(" ・ ") : shot.clip_id ? "素材情報なし" : "未割り当て"}
                {` ・ 開始 ${shot.src_in.toFixed(1)}秒 ・ ${describeMotion(shot)}`}
              </span>
            </div>
            {shot.hflip ? <FlipHorizontal2 className="size-3.5 shrink-0 text-subtle-foreground" aria-label="左右反転" /> : null}
            <div className="flex shrink-0 items-center gap-1">
              {changed ? (
                <Button variant="ghost" size="icon-xs" onClick={() => onReset(index)} aria-label={`ショット ${index + 1} の差し替えを元に戻す`} title="元に戻す">
                  <RotateCcw aria-hidden />
                </Button>
              ) : null}
              <Button variant="secondary" size="xs" onClick={() => onSwap(index)} aria-label={`ショット ${index + 1} の素材を差し替え`}>
                <Replace aria-hidden />
                差し替え
              </Button>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
