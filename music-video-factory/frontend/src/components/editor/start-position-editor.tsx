"use client";

import { Magnet, Minus, Plus, RotateCcw } from "lucide-react";
import * as React from "react";

import { sectionColor, sectionLabel } from "@/components/audio/sections";
import { nearestGridPoint } from "@/components/audio/timeline";
import { Button } from "@/components/ui/button";
import { Slider } from "@/components/ui/slider";
import { formatDuration, formatTime, formatTimePrecise } from "@/lib/format";
import type { AudioAnalysis } from "@/lib/types";
import { clamp } from "@/lib/utils";

function round2(value: number): number {
  return Math.round(value * 100) / 100;
}

/** Song sections with the video's window; click to move the window there. */
function SongWindowBar({
  analysis,
  songDuration,
  start,
  duration,
  renderStart,
  onPick,
}: {
  analysis: AudioAnalysis | null;
  songDuration: number;
  start: number;
  duration: number;
  renderStart: number;
  onPick: (seconds: number) => void;
}) {
  const pct = (seconds: number) => `${(clamp(seconds, 0, songDuration) / songDuration) * 100}%`;
  return (
    <div className="flex flex-col gap-1">
      <div
        className="relative h-10 cursor-pointer overflow-hidden rounded-md border border-border bg-surface-raised"
        onClick={(event) => {
          const rect = event.currentTarget.getBoundingClientRect();
          onPick(((event.clientX - rect.left) / rect.width) * songDuration);
        }}
        role="presentation"
      >
        {(analysis?.sections ?? []).map((section, index) => (
          <div
            key={`${section.start}-${index}`}
            className="absolute inset-y-0 border-r border-background/60"
            style={{ left: pct(section.start), width: `calc(${pct(section.end)} - ${pct(section.start)})`, backgroundColor: `${sectionColor(section.label)}33` }}
            title={`${sectionLabel(section.label)} ${formatTime(section.start)}–${formatTime(section.end)}`}
          >
            <span className="absolute top-0.5 left-1 truncate text-2xs text-muted-foreground">{sectionLabel(section.label)}</span>
          </div>
        ))}
        <div
          className="absolute inset-y-0 border-x border-dashed border-zinc-500"
          style={{ left: pct(renderStart), width: `calc(${pct(renderStart + duration)} - ${pct(renderStart)})` }}
          title="現在のレンダリングの区間"
          aria-hidden
        />
        <div
          className="absolute inset-y-0 border-2 border-zinc-200 bg-white/10"
          style={{ left: pct(start), width: `calc(${pct(start + duration)} - ${pct(start)})` }}
          aria-hidden
        />
      </div>
      <div className="flex justify-between font-mono text-2xs text-subtle-foreground tabular">
        <span>0:00</span>
        <span>{formatTime(songDuration)}</span>
      </div>
    </div>
  );
}

export interface StartPositionEditorProps {
  analysis: AudioAnalysis | null;
  songDuration: number | null;
  /** Effective start (draft applied). */
  start: number;
  /** Start of the current render. */
  renderStart: number;
  duration: number;
  onChange: (start: number) => void;
}

/** Move the video's window within the song: slider, ±0.1 / ±1 s, snap to bar, section quick picks. */
export function StartPositionEditor({ analysis, songDuration, start, renderStart, duration, onChange }: StartPositionEditorProps) {
  if (songDuration === null) {
    return <p className="text-xs text-muted-foreground">楽曲解析の結果を読み込めないため、開始位置を変更できません。</p>;
  }
  const maxStart = Math.max(0, songDuration - duration);
  const set = (value: number) => onChange(round2(clamp(value, 0, maxStart)));
  const grid = analysis?.downbeats.length ? analysis.downbeats : (analysis?.beats ?? []);
  const picks = [
    ...(analysis?.chorus_candidates ?? []).slice(0, 3).map((c, i) => ({ label: `サビ候補 ${i + 1}`, start: c.start })),
    ...(analysis?.suggested_segments ?? [])
      .filter((s) => Math.abs(s.duration - duration) < 1)
      .slice(0, 2)
      .map((s) => ({ label: `推奨区間（${formatDuration(s.duration)}）`, start: s.start })),
  ];

  return (
    <div className="flex flex-col gap-4">
      <SongWindowBar
        analysis={analysis}
        songDuration={songDuration}
        start={start}
        duration={duration}
        renderStart={renderStart}
        onPick={(seconds) => set(seconds - duration / 2)}
      />
      <div className="flex flex-col gap-2">
        <Slider min={0} max={maxStart} step={0.1} value={[clamp(start, 0, maxStart)]} onValueChange={([value]) => set(value)} aria-label="開始位置" disabled={maxStart === 0} />
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="secondary" size="icon-xs" onClick={() => set(start - 1)} aria-label="1 秒前へ" title="-1 秒">
            <Minus aria-hidden />
          </Button>
          <Button variant="ghost" size="xs" onClick={() => set(start - 0.1)} aria-label="0.1 秒前へ">
            -0.1
          </Button>
          <span className="w-56 text-center font-mono text-sm text-foreground tabular">
            {formatTimePrecise(start)} 〜 {formatTimePrecise(start + duration)}
          </span>
          <Button variant="ghost" size="xs" onClick={() => set(start + 0.1)} aria-label="0.1 秒後へ">
            +0.1
          </Button>
          <Button variant="secondary" size="icon-xs" onClick={() => set(start + 1)} aria-label="1 秒後へ" title="+1 秒">
            <Plus aria-hidden />
          </Button>
          {grid.length > 0 ? (
            <Button
              variant="ghost"
              size="xs"
              onClick={() => {
                const snapped = nearestGridPoint(grid, start);
                if (snapped !== null) set(snapped);
              }}
            >
              <Magnet aria-hidden />
              小節の頭に合わせる
            </Button>
          ) : null}
          {Math.abs(start - renderStart) > 0.005 ? (
            <Button variant="ghost" size="xs" onClick={() => onChange(renderStart)}>
              <RotateCcw aria-hidden />
              元に戻す
            </Button>
          ) : null}
        </div>
      </div>
      {picks.length > 0 ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-muted-foreground">候補から選ぶ:</span>
          {picks.map((pick) => (
            <Button key={`${pick.label}-${pick.start}`} variant="outline" size="xs" onClick={() => set(pick.start)}>
              {pick.label} {formatTime(pick.start)}
            </Button>
          ))}
        </div>
      ) : null}
      <p className="text-2xs leading-4 text-subtle-foreground">
        動画の長さ（{formatDuration(duration)}）は変わりません。音声と歌詞は新しい区間のものになります。バーをクリックすると、その位置を中心に区間を移動します（点線は現在のレンダリングの区間）。
      </p>
    </div>
  );
}
