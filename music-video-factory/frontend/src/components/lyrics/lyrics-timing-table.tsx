"use client";

import { CircleAlert, Crosshair, Eraser, Minus, Play, Plus, TriangleAlert } from "lucide-react";
import * as React from "react";

import { useAudioPlayer, useAudioState, useAudioTimeSelector } from "@/components/audio/audio-player";
import { TimeInput } from "@/components/audio/time-input";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { fieldControlClasses } from "@/components/ui/input";
import { SimpleTooltip } from "@/components/ui/tooltip";
import { formatPercent, formatTimePrecise } from "@/lib/format";
import type { LyricLine } from "@/lib/types";
import { cn } from "@/lib/utils";

import {
  activeLineIndex,
  clearLineTiming,
  effectiveEnd,
  nudgeLine,
  replaceLine,
  setLineText,
  setLineTiming,
  SOURCE_LABELS,
  tapLineEnd,
  tapLineStart,
  type TimingIssue,
} from "./timing";

const NUDGE_SECONDS = 0.1;

/** Text field for an explicit lyric edit: commits on blur/Enter, Escape reverts. */
function LineTextInput({ value, onCommit, label }: { value: string; onCommit: (text: string) => void; label: string }) {
  const [draft, setDraft] = React.useState<string | null>(null);
  const skipBlur = React.useRef(false);
  return (
    <input
      type="text"
      aria-label={label}
      value={draft ?? value}
      onFocus={() => setDraft(value)}
      onChange={(event) => setDraft(event.target.value)}
      onBlur={() => {
        if (!skipBlur.current && draft !== null && draft !== value) onCommit(draft);
        skipBlur.current = false;
        setDraft(null);
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          if (draft !== null && draft !== value) onCommit(draft);
          skipBlur.current = true;
          setDraft(null);
          event.currentTarget.blur();
        } else if (event.key === "Escape") {
          event.preventDefault();
          skipBlur.current = true;
          setDraft(null);
          event.currentTarget.blur();
        }
      }}
      className={cn(fieldControlClasses, "h-8 px-2 text-sm")}
    />
  );
}

function NudgeButtons({ onNudge, disabled, label }: { onNudge: (delta: number) => void; disabled: boolean; label: string }) {
  return (
    <span className="flex flex-col">
      <button
        type="button"
        disabled={disabled}
        onClick={() => onNudge(NUDGE_SECONDS)}
        aria-label={`${label}を0.1秒遅く`}
        title="+0.1秒"
        className="focus-ring-custom flex h-4 w-5 cursor-pointer items-center justify-center rounded-t border border-border bg-surface-raised text-muted-foreground outline-none hover:bg-surface-hover hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/40 disabled:cursor-not-allowed disabled:opacity-40"
      >
        <Plus className="size-2.5" aria-hidden />
      </button>
      <button
        type="button"
        disabled={disabled}
        onClick={() => onNudge(-NUDGE_SECONDS)}
        aria-label={`${label}を0.1秒早く`}
        title="−0.1秒"
        className="focus-ring-custom -mt-px flex h-4 w-5 cursor-pointer items-center justify-center rounded-b border border-border bg-surface-raised text-muted-foreground outline-none hover:bg-surface-hover hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/40 disabled:cursor-not-allowed disabled:opacity-40"
      >
        <Minus className="size-2.5" aria-hidden />
      </button>
    </span>
  );
}

function IssueIcon({ issues }: { issues: readonly TimingIssue[] }) {
  if (issues.length === 0) return null;
  const error = issues.some((issue) => issue.level === "error");
  const Icon = error ? CircleAlert : TriangleAlert;
  return (
    <SimpleTooltip content={issues.map((issue) => issue.message).join(" / ")}>
      <span
        tabIndex={0}
        role="img"
        aria-label={issues.map((issue) => issue.message).join("、")}
        className={cn("inline-flex rounded outline-none focus-visible:ring-2 focus-visible:ring-ring/40", error ? "text-danger" : "text-warning")}
      >
        <Icon className="size-4" aria-hidden />
      </span>
    </SimpleTooltip>
  );
}

export interface LyricsTimingTableProps {
  lines: LyricLine[];
  issues: TimingIssue[][];
  duration: number | null;
  onChange: (lines: LyricLine[]) => void;
  /** Allow editing the lyric text of each line (explicit opt-in). */
  editText: boolean;
  /** Line that the next "tap" sets, or `null` when tap mode is off. */
  tapIndex: number | null;
  onTapIndexChange: (index: number) => void;
}

/** Line-by-line timing editor: start/end fields, ±0.1 s nudges, set-from-playhead, play-from-line. */
export function LyricsTimingTable({ lines, issues, duration, onChange, editText, tapIndex, onTapIndexChange }: LyricsTimingTableProps) {
  const player = useAudioPlayer();
  const audio = useAudioState();
  const active = useAudioTimeSelector((time) => activeLineIndex(lines, time, duration));
  const activeRow = React.useRef<HTMLTableRowElement>(null);
  const followPlayback = audio.playing;

  React.useEffect(() => {
    if (!followPlayback || active < 0) return;
    activeRow.current?.scrollIntoView({ block: "nearest" });
  }, [active, followPlayback]);

  const max = duration ?? undefined;

  return (
    <div className="max-h-[640px] overflow-auto rounded-md border border-border">
      <table className="w-full border-collapse text-sm">
        <caption className="sr-only">歌詞のタイミング</caption>
        <thead className="sticky top-0 z-10 bg-surface-raised text-xs text-muted-foreground">
          <tr>
            <th className="w-10 px-2 py-2 text-right font-medium">#</th>
            <th className="w-[9.75rem] px-2 py-2 text-left font-medium">開始</th>
            <th className="w-[9.75rem] px-2 py-2 text-left font-medium">終了</th>
            <th className="px-2 py-2 text-left font-medium">歌詞</th>
            <th className="w-24 px-2 py-2 text-left font-medium">状態</th>
            <th className="w-[4.5rem] px-2 py-2 text-right font-medium">
              <span className="sr-only">操作</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {lines.map((line, index) => {
            const derivedEnd = effectiveEnd(lines, index, duration);
            const isActive = index === active;
            const isTapTarget = tapIndex === index;
            const lineIssues = issues[index] ?? [];
            const hasError = lineIssues.some((issue) => issue.level === "error");
            const label = `${index + 1}行目`;
            return (
              <tr
                key={line.id}
                ref={isActive ? activeRow : undefined}
                aria-current={isActive ? "true" : undefined}
                className={cn(
                  "border-t border-border align-middle first:border-t-0",
                  isActive ? "bg-surface-hover" : "hover:bg-surface-raised/50",
                  hasError && "bg-danger-soft",
                )}
              >
                <td className={cn("relative px-2 py-1.5 text-right font-mono text-xs tabular", isActive ? "text-foreground" : "text-subtle-foreground")}>
                  {isActive ? <span className="absolute inset-y-0 left-0 w-0.5 bg-foreground" aria-hidden /> : null}
                  {tapIndex !== null ? (
                    <button
                      type="button"
                      onClick={() => onTapIndexChange(index)}
                      aria-label={`${label}からタップ入力する`}
                      aria-pressed={isTapTarget}
                      className={cn(
                        "focus-ring-custom cursor-pointer rounded px-1 outline-none focus-visible:ring-2 focus-visible:ring-ring/40",
                        isTapTarget ? "bg-zinc-200 font-semibold text-zinc-900" : "hover:bg-surface-hover hover:text-foreground",
                      )}
                    >
                      {index + 1}
                    </button>
                  ) : (
                    index + 1
                  )}
                </td>
                <td className="px-2 py-1.5">
                  <div className="flex items-center gap-1">
                    <TimeInput
                      aria-label={`${label}の開始`}
                      value={line.start}
                      max={max}
                      allowEmpty
                      placeholder="未設定"
                      invalid={hasError}
                      className="w-[4.75rem]"
                      onCommit={(value) => {
                        const end = line.end !== null && value !== null && line.end <= value ? null : line.end;
                        onChange(replaceLine(lines, index, setLineTiming(line, value, value === null ? null : end)));
                      }}
                    />
                    <NudgeButtons
                      label={`${label}の開始`}
                      disabled={line.start === null}
                      onNudge={(delta) => onChange(nudgeLine(lines, index, "start", delta, duration))}
                    />
                    <SimpleTooltip content="再生位置を開始に設定">
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-xs"
                        disabled={!audio.hasSource}
                        aria-label={`${label}の開始を現在の再生位置に設定`}
                        onClick={() => onChange(tapLineStart(lines, index, player.getTime()))}
                      >
                        <Crosshair aria-hidden />
                      </Button>
                    </SimpleTooltip>
                  </div>
                </td>
                <td className="px-2 py-1.5">
                  <div className="flex items-center gap-1">
                    <TimeInput
                      aria-label={`${label}の終了`}
                      value={line.end}
                      max={max}
                      allowEmpty
                      disabled={line.start === null}
                      placeholder={derivedEnd !== null ? formatTimePrecise(derivedEnd) : "—"}
                      invalid={hasError}
                      className={cn("w-[4.75rem]", line.end === null && "placeholder:text-subtle-foreground/80")}
                      onCommit={(value) => onChange(replaceLine(lines, index, setLineTiming(line, line.start, value)))}
                    />
                    <NudgeButtons
                      label={`${label}の終了`}
                      disabled={line.start === null}
                      onNudge={(delta) => onChange(nudgeLine(lines, index, "end", delta, duration))}
                    />
                    <SimpleTooltip content="再生位置を終了に設定">
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-xs"
                        disabled={!audio.hasSource || line.start === null}
                        aria-label={`${label}の終了を現在の再生位置に設定`}
                        onClick={() => onChange(tapLineEnd(lines, index, player.getTime()))}
                      >
                        <Crosshair aria-hidden />
                      </Button>
                    </SimpleTooltip>
                  </div>
                </td>
                <td className="px-2 py-1.5">
                  {editText ? (
                    <LineTextInput
                      value={line.text}
                      label={`${label}の歌詞`}
                      onCommit={(text) => onChange(replaceLine(lines, index, setLineText(line, text)))}
                    />
                  ) : (
                    <span className={cn("block break-words", isActive ? "text-foreground" : "text-foreground/90")}>{line.text}</span>
                  )}
                </td>
                <td className="px-2 py-1.5">
                  <div className="flex items-center gap-1.5">
                    <Badge tone={line.start === null ? "neutral" : line.source === "manual" ? "outline" : "neutral"}>
                      {line.start === null ? "未設定" : SOURCE_LABELS[line.source] ?? line.source}
                    </Badge>
                    {line.confidence !== null && line.start !== null ? (
                      <SimpleTooltip content="自動推定の確からしさ">
                        <span tabIndex={0} className="font-mono text-2xs text-subtle-foreground tabular outline-none">
                          {formatPercent(line.confidence)}
                        </span>
                      </SimpleTooltip>
                    ) : null}
                    <IssueIcon issues={lineIssues} />
                  </div>
                </td>
                <td className="px-2 py-1.5">
                  <div className="flex items-center justify-end gap-0.5">
                    <SimpleTooltip content="この行から再生">
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-xs"
                        disabled={!audio.hasSource || line.start === null}
                        aria-label={`${label}から再生`}
                        onClick={() => line.start !== null && player.playFrom(Math.max(0, line.start - 0.25))}
                      >
                        <Play aria-hidden />
                      </Button>
                    </SimpleTooltip>
                    <SimpleTooltip content="タイミングを消去">
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-xs"
                        disabled={line.start === null && line.end === null}
                        aria-label={`${label}のタイミングを消去`}
                        onClick={() => onChange(clearLineTiming(lines, index))}
                      >
                        <Eraser aria-hidden />
                      </Button>
                    </SimpleTooltip>
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
