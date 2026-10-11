"use client";

import { ChevronsLeft, ChevronsRight, Minus, Play, Plus, RotateCcw, TriangleAlert } from "lucide-react";
import Link from "next/link";
import * as React from "react";

import { projectTabHref } from "@/components/generate/links";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatDuration, formatTimePrecise } from "@/lib/format";
import type { LyricLine, LyricOffset } from "@/lib/types";
import { cn } from "@/lib/utils";

import { linesInWindow, MIN_LINE_SECONDS } from "./edit-draft";

const NUDGE = 0.1;

function round2(value: number): number {
  return Math.round(value * 100) / 100;
}

/** New timing after nudging `edge` by `delta`, keeping `start < end`. */
export function nudgeTiming(line: { start: number; end: number }, edge: "start" | "end" | "both", delta: number): LyricOffset {
  if (edge === "both") {
    const shift = Math.max(delta, -line.start);
    return { start: round2(line.start + shift), end: round2(line.end + shift) };
  }
  if (edge === "start") {
    return { start: round2(Math.min(Math.max(0, line.start + delta), line.end - MIN_LINE_SECONDS)), end: line.end };
  }
  return { start: line.start, end: round2(Math.max(line.start + MIN_LINE_SECONDS, line.end + delta)) };
}

function NudgeControl({
  label,
  value,
  relative,
  onNudge,
  changed,
}: {
  label: string;
  value: number;
  relative: number;
  onNudge: (delta: number) => void;
  changed: boolean;
}) {
  return (
    <div className="flex items-center justify-center gap-0.5">
      <Button variant="ghost" size="icon-xs" onClick={() => onNudge(-NUDGE)} aria-label={`${label}を 0.1 秒早める`} title="-0.1 秒">
        <Minus aria-hidden />
      </Button>
      <div className="flex w-[4.5rem] flex-col items-center leading-tight">
        <span className={cn("font-mono text-xs tabular", changed ? "text-foreground" : "text-muted-foreground")}>{formatTimePrecise(value)}</span>
        <span className="font-mono text-2xs text-subtle-foreground tabular" title="動画内の時刻">
          {relative < 0 ? "-" : ""}
          {formatTimePrecise(Math.abs(relative))}
        </span>
      </div>
      <Button variant="ghost" size="icon-xs" onClick={() => onNudge(NUDGE)} aria-label={`${label}を 0.1 秒遅らせる`} title="+0.1 秒">
        <Plus aria-hidden />
      </Button>
    </div>
  );
}

export interface LyricTimingEditorProps {
  projectId: string;
  /** Effective lines (draft applied), absolute song seconds. */
  lines: readonly LyricLine[];
  /** Lines of the current render, to mark changes. */
  baseLines: readonly LyricLine[];
  windowStart: number;
  duration: number;
  /** Start of the currently rendered video (the player's time 0). */
  renderStart: number;
  /** Player position in seconds (relative to the render). */
  playerTime: number | null;
  onChange: (lineId: string, timing: LyricOffset) => void;
  onReset: (lineId: string) => void;
  onSeek: (renderSeconds: number) => void;
}

/** Per-line timing of the lyrics inside this video, with ±0.1 s nudges for start/end/both. */
export function LyricTimingEditor({
  projectId,
  lines,
  baseLines,
  windowStart,
  duration,
  renderStart,
  playerTime,
  onChange,
  onReset,
  onSeek,
}: LyricTimingEditorProps) {
  const visible = linesInWindow(lines, windowStart, duration);
  const baseById = React.useMemo(() => new Map(baseLines.map((line) => [line.id, line])), [baseLines]);
  const untimed = lines.filter((line) => line.start === null).length;
  const playing = playerTime !== null ? renderStart + playerTime : null;

  function shiftAll(delta: number) {
    for (const line of visible) {
      if (line.start === null) continue;
      onChange(line.id, nudgeTiming({ start: line.start, end: line.end ?? line.start + 2 }, "both", delta));
    }
  }

  if (visible.length === 0) {
    return (
      <EmptyState
        size="sm"
        title="この動画の区間に歌詞はありません"
        description={
          untimed > 0
            ? `タイミング未設定の歌詞が ${untimed} 行あります。歌詞タブでタイミングを設定すると、ここで調整できます。`
            : "開始位置を変えるか、歌詞タブで歌詞を登録してください。"
        }
        action={
          <Button variant="secondary" size="sm" asChild>
            <Link href={projectTabHref(projectId, "lyrics")}>歌詞タブを開く</Link>
          </Button>
        }
      />
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-muted-foreground">
          この動画に含まれる {visible.length} 行。時刻は曲の頭からの秒数（下段は動画内の時刻）です。変更はこの動画だけに反映されます。
        </p>
        <div className="flex items-center gap-1">
          <span className="text-xs text-muted-foreground">全行をまとめて</span>
          <Button variant="secondary" size="xs" onClick={() => shiftAll(-NUDGE)}>
            <ChevronsLeft aria-hidden />
            0.1 秒早く
          </Button>
          <Button variant="secondary" size="xs" onClick={() => shiftAll(NUDGE)}>
            0.1 秒遅く
            <ChevronsRight aria-hidden />
          </Button>
        </div>
      </div>
      <div className="rounded-md border border-border">
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead className="w-10 pr-0">
                <span className="sr-only">再生</span>
              </TableHead>
              <TableHead className="min-w-40">歌詞</TableHead>
              <TableHead className="w-36 px-1 text-center">開始</TableHead>
              <TableHead className="w-36 px-1 text-center">終了</TableHead>
              <TableHead className="w-16 px-1 text-center">行全体</TableHead>
              <TableHead className="w-14 px-1 text-right">長さ</TableHead>
              <TableHead className="w-10">
                <span className="sr-only">元に戻す</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {visible.map((line, index) => {
              const start = line.start ?? 0;
              const end = line.end ?? start + 2;
              const base = baseById.get(line.id);
              const changed = !!base && (base.start !== line.start || base.end !== line.end);
              const previous = index > 0 ? visible[index - 1] : null;
              const overlaps = previous?.end !== null && previous?.end !== undefined && start < previous.end - 0.01;
              const active = playing !== null && playing >= start && playing < end;
              const clipped = start < windowStart || end > windowStart + duration;
              return (
                <TableRow key={line.id} data-state={active ? "selected" : undefined} className={cn(active && "bg-surface-raised")}>
                  <TableCell className="pr-0">
                    <Button
                      variant="ghost"
                      size="icon-xs"
                      onClick={() => onSeek(Math.max(0, start - renderStart))}
                      aria-label={`「${line.text}」から再生`}
                      title="現在のレンダリングでこの行から再生"
                    >
                      <Play aria-hidden />
                    </Button>
                  </TableCell>
                  <TableCell className="max-w-0">
                    <div className="flex min-w-0 flex-col gap-0.5">
                      <span className={cn("truncate text-sm", active ? "text-foreground" : "text-foreground/90")} title={line.text}>
                        {line.text}
                      </span>
                      <span className="flex flex-wrap items-center gap-1.5">
                        {changed ? <Badge tone="outline">変更</Badge> : null}
                        {overlaps ? (
                          <span className="flex items-center gap-1 text-2xs text-warning-foreground">
                            <TriangleAlert className="size-3" aria-hidden />
                            前の行と重なっています
                          </span>
                        ) : null}
                        {clipped ? <span className="text-2xs text-subtle-foreground">区間の端で切れます</span> : null}
                      </span>
                    </div>
                  </TableCell>
                  <TableCell className="px-1">
                    <NudgeControl
                      label="開始"
                      value={start}
                      relative={start - windowStart}
                      changed={changed && base?.start !== line.start}
                      onNudge={(delta) => onChange(line.id, nudgeTiming({ start, end }, "start", delta))}
                    />
                  </TableCell>
                  <TableCell className="px-1">
                    <NudgeControl
                      label="終了"
                      value={end}
                      relative={end - windowStart}
                      changed={changed && base?.end !== line.end}
                      onNudge={(delta) => onChange(line.id, nudgeTiming({ start, end }, "end", delta))}
                    />
                  </TableCell>
                  <TableCell className="px-1">
                    <div className="flex items-center justify-center gap-0.5">
                      <Button variant="ghost" size="icon-xs" onClick={() => onChange(line.id, nudgeTiming({ start, end }, "both", -NUDGE))} aria-label="行全体を 0.1 秒早める" title="行全体 -0.1 秒">
                        <ChevronsLeft aria-hidden />
                      </Button>
                      <Button variant="ghost" size="icon-xs" onClick={() => onChange(line.id, nudgeTiming({ start, end }, "both", NUDGE))} aria-label="行全体を 0.1 秒遅らせる" title="行全体 +0.1 秒">
                        <ChevronsRight aria-hidden />
                      </Button>
                    </div>
                  </TableCell>
                  <TableCell className="px-1 text-right font-mono text-xs whitespace-nowrap text-muted-foreground tabular">{formatDuration(end - start)}</TableCell>
                  <TableCell>
                    {changed ? (
                      <Button variant="ghost" size="icon-xs" onClick={() => onReset(line.id)} aria-label="この行の変更を元に戻す" title="元に戻す">
                        <RotateCcw aria-hidden />
                      </Button>
                    ) : null}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
      {untimed > 0 ? (
        <p className="text-2xs text-subtle-foreground">
          タイミング未設定の {untimed} 行は表示されません。
          <Link href={projectTabHref(projectId, "lyrics")} className="ml-1 text-muted-foreground underline underline-offset-2 hover:text-foreground">
            歌詞タブで設定
          </Link>
        </p>
      ) : null}
    </div>
  );
}
