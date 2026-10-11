"use client";

import { Pause, Play, Repeat, Save, Undo2 } from "lucide-react";
import * as React from "react";
import { useSWRConfig } from "swr";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { toast } from "@/components/ui/toast";
import { SimpleTooltip } from "@/components/ui/tooltip";
import { apiPaths, errorMessage, updateSegment } from "@/lib/api";
import { formatDuration, formatTime, formatTimePrecise } from "@/lib/format";
import type { AnalysisOut, AudioAnalysis, ProjectOut, Segment } from "@/lib/types";
import { cn } from "@/lib/utils";

import { useAudioPlayer, useAudioState, useAudioTimeSelector } from "./audio-player";
import { AudioTransport } from "./audio-transport";
import { CHORUS_MARKER_COLOR, HIGHLIGHT_MARKER_COLOR, SECTION_COLORS, SECTION_LABELS_JA } from "./sections";
import { TimeInput } from "./time-input";
import { MIN_SEGMENT_SECONDS, normalizeRange, rangesEqual, type TimeRange } from "./timeline";
import { Waveform } from "./waveform";

export interface SegmentEditorProps {
  projectId: string;
  analysis: AudioAnalysis;
  /** Segment saved on the server (`analysis.segment`). */
  savedSegment: Segment | null;
}

/** Initial draft: the saved segment, else the best 15 s suggestion, else the first 15 s. */
function initialRange(analysis: AudioAnalysis, saved: Segment | null): TimeRange {
  if (saved) return { start: saved.start, end: saved.end };
  const suggestions = [...analysis.suggested_segments].sort((a, b) => b.score - a.score);
  const best = suggestions.find((s) => s.duration === 15) ?? suggestions[0];
  if (best) return normalizeRange(best, analysis.duration);
  return normalizeRange({ start: 0, end: 15 }, analysis.duration);
}

function Legend({ analysis }: { analysis: AudioAnalysis }) {
  // Only the section kinds present in this song, in a stable order.
  const present = new Set(analysis.sections.map((section) => section.label));
  const labels = Object.keys(SECTION_LABELS_JA).filter((key) => present.has(key));
  return (
    <ul className="flex flex-wrap items-center gap-x-4 gap-y-1 text-2xs text-muted-foreground" aria-label="凡例">
      {labels.map((key) => (
        <li key={key} className="flex items-center gap-1.5">
          <span className="size-2.5 rounded-sm" style={{ backgroundColor: SECTION_COLORS[key] }} aria-hidden />
          {SECTION_LABELS_JA[key]}
        </li>
      ))}
      <li className="flex items-center gap-1.5">
        <span className="h-1.5 w-4 rounded-sm" style={{ backgroundColor: CHORUS_MARKER_COLOR }} aria-hidden />
        サビ候補
      </li>
      <li className="flex items-center gap-1.5">
        <span className="h-1.5 w-4 rounded-sm" style={{ backgroundColor: HIGHLIGHT_MARKER_COLOR }} aria-hidden />
        盛り上がり
      </li>
      <li className="flex items-center gap-1.5">
        <span className="h-2.5 w-px bg-zinc-400" aria-hidden />
        小節頭（ダウンビート）
      </li>
    </ul>
  );
}

interface QuickPick {
  key: string;
  label: string;
  range: TimeRange;
  detail: string;
  tooltip: string;
}

function quickPicks(analysis: AudioAnalysis): { suggested: QuickPick[]; chorus: QuickPick[] } {
  const suggested = [...analysis.suggested_segments]
    .sort((a, b) => a.duration - b.duration || b.score - a.score)
    .map((segment, index) => ({
      key: `s${index}`,
      label: `${segment.duration}秒`,
      range: normalizeRange(segment, analysis.duration),
      detail: `${formatTime(segment.start)}–${formatTime(segment.end)}`,
      tooltip: segment.reason || `おすすめ度 ${Math.round(segment.score * 100)}%`,
    }));
  const chorus = [...analysis.chorus_candidates]
    .sort((a, b) => a.start - b.start)
    .map((candidate, index) => ({
      key: `c${index}`,
      label: `サビ候補${index + 1}`,
      range: normalizeRange(candidate, analysis.duration),
      detail: `${formatTime(candidate.start)}–${formatTime(candidate.end)}`,
      tooltip: `サビらしさ ${Math.round(candidate.score * 100)}% ・ ${formatDuration(candidate.end - candidate.start)}`,
    }));
  return { suggested, chorus };
}

function PickButton({ pick, active, onSelect }: { pick: QuickPick; active: boolean; onSelect: (range: TimeRange) => void }) {
  return (
    <SimpleTooltip content={pick.tooltip}>
      <button
        type="button"
        aria-pressed={active}
        onClick={() => onSelect(pick.range)}
        className={cn(
          "focus-ring-custom inline-flex h-8 cursor-pointer items-center gap-2 rounded-md border px-2.5 text-xs outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring/40",
          active
            ? "border-zinc-300 bg-zinc-200 text-zinc-900"
            : "border-border bg-surface-raised text-foreground hover:border-border-strong hover:bg-surface-hover",
        )}
      >
        <span className="font-medium">{pick.label}</span>
        <span className={cn("font-mono tabular", active ? "text-zinc-700" : "text-muted-foreground")}>{pick.detail}</span>
      </button>
    </SimpleTooltip>
  );
}

/** Waveform with a draggable segment selection, quick picks and save (`PUT /segment`). */
export function SegmentEditor({ projectId, analysis, savedSegment }: SegmentEditorProps) {
  const { mutate } = useSWRConfig();
  const player = useAudioPlayer();
  const audio = useAudioState();
  const [{ range: draft, touched }, setDraftState] = React.useState(() => ({
    range: initialRange(analysis, savedSegment),
    touched: false,
  }));
  const [syncedSaved, setSyncedSaved] = React.useState<Segment | null>(savedSegment);
  const [snapEnabled, setSnapEnabled] = React.useState(true);
  const [loop, setLoop] = React.useState(false);
  const [saving, setSaving] = React.useState(false);

  // Adopt a new server segment (e.g. the default set after analysis) unless the user is editing.
  if (!rangesEqual(savedSegment, syncedSaved)) {
    setSyncedSaved(savedSegment);
    if (savedSegment && !touched) setDraftState({ range: { start: savedSegment.start, end: savedSegment.end }, touched: false });
  }

  const setDraft = (range: TimeRange) => setDraftState({ range, touched: true });
  const resetDraft = (range: TimeRange) => setDraftState({ range, touched: false });

  const duration = analysis.duration;
  const dirty = !rangesEqual(draft, savedSegment);
  const length = draft.end - draft.start;
  const snapGrid = snapEnabled ? (analysis.downbeats.length > 0 ? analysis.downbeats : analysis.beats) : null;
  const picks = React.useMemo(() => quickPicks(analysis), [analysis]);
  const draftStart = draft.start;
  const draftEnd = draft.end;
  const playingInside = useAudioTimeSelector((time) => time >= draftStart - 0.05 && time <= draftEnd + 0.05);
  const playingSelection = audio.playing && playingInside;

  function select(range: TimeRange, seekToStart = true) {
    const next = normalizeRange(range, duration);
    setDraft(next);
    if (seekToStart && !audio.playing) player.seek(next.start);
  }

  function updateEdge(edge: "start" | "end", value: number | null) {
    if (value === null) return;
    const next = edge === "start" ? { start: value, end: Math.max(draft.end, value + MIN_SEGMENT_SECONDS) } : { start: draft.start, end: value };
    if (edge === "end" && value - draft.start < MIN_SEGMENT_SECONDS) next.start = Math.max(0, value - MIN_SEGMENT_SECONDS);
    setDraft(normalizeRange(next, duration));
  }

  async function save() {
    setSaving(true);
    try {
      const result = await updateSegment(projectId, { start: draft.start, end: draft.end });
      resetDraft({ start: result.segment.start, end: result.segment.end });
      await Promise.all([
        mutate<AnalysisOut>(apiPaths.analysis(projectId), (current) => (current ? { ...current, segment: result.segment } : current), {
          revalidate: false,
        }),
        mutate<ProjectOut>(
          apiPaths.project(projectId),
          (current) => (current ? { ...current, segment_start: result.segment.start, segment_end: result.segment.end } : current),
          { revalidate: false },
        ),
      ]);
      toast.success("区間を保存しました", `${formatTimePrecise(result.segment.start)} – ${formatTimePrecise(result.segment.end)}`);
    } catch (error) {
      toast.error("区間を保存できませんでした", errorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  const isActive = (range: TimeRange) => rangesEqual(range, draft, 0.02);

  return (
    <Card>
      <CardHeader>
        <CardTitle>動画に使う区間</CardTitle>
        <CardDescription>
          波形をクリックで再生位置を移動、ドラッグで区間を選択します。ハンドルで開始・終了を、選択範囲の中をドラッグで区間全体を動かせます。
        </CardDescription>
        <CardAction>
          <div className="flex items-center gap-2">
            <Switch id="snap-downbeats" checked={snapEnabled} onCheckedChange={setSnapEnabled} />
            <Label htmlFor="snap-downbeats" className="text-xs font-normal text-muted-foreground">
              小節頭にスナップ
            </Label>
          </div>
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <AudioTransport duration={duration} />
        <div className="rounded-md border border-border bg-background px-3 pt-3 pb-1">
          <Waveform
            analysis={analysis}
            selection={draft}
            savedSelection={savedSegment}
            onSelectionChange={setDraft}
            snapGrid={snapGrid}
          />
        </div>
        <Legend analysis={analysis} />

        <div className="flex flex-wrap items-end gap-x-6 gap-y-3 border-t border-border pt-4">
          <div className="flex items-end gap-3">
            <div className="flex flex-col gap-1">
              <Label htmlFor="segment-start" className="text-xs font-normal text-muted-foreground">
                開始
              </Label>
              <TimeInput id="segment-start" aria-label="区間の開始" value={draft.start} max={duration} onCommit={(v) => updateEdge("start", v)} />
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="segment-end" className="text-xs font-normal text-muted-foreground">
                終了
              </Label>
              <TimeInput id="segment-end" aria-label="区間の終了" value={draft.end} max={duration} onCommit={(v) => updateEdge("end", v)} />
            </div>
            <div className="flex flex-col gap-1 pb-1.5">
              <span className="text-xs text-muted-foreground">長さ</span>
              <span className="font-mono text-sm tabular text-foreground">{length.toFixed(2)}秒</span>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="secondary"
              size="sm"
              disabled={!audio.hasSource}
              onClick={() => (playingSelection ? player.pause() : player.playRange(draft.start, draft.end, { loop }))}
            >
              {playingSelection ? <Pause aria-hidden /> : <Play aria-hidden />}
              {playingSelection ? "停止" : "区間を再生"}
            </Button>
            <SimpleTooltip content="区間の再生をループする">
              <Button
                type="button"
                variant={loop ? "outline" : "ghost"}
                size="icon-sm"
                aria-pressed={loop}
                aria-label="ループ再生"
                onClick={() => setLoop((value) => !value)}
              >
                <Repeat aria-hidden />
              </Button>
            </SimpleTooltip>
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            {savedSegment ? (
              <span className="text-xs text-muted-foreground">
                保存済み:{" "}
                <span className="font-mono tabular text-foreground">
                  {formatTimePrecise(savedSegment.start)}–{formatTimePrecise(savedSegment.end)}
                </span>
              </span>
            ) : (
              <Badge tone="warning">未保存</Badge>
            )}
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={!dirty || !savedSegment || saving}
              onClick={() => savedSegment && resetDraft({ start: savedSegment.start, end: savedSegment.end })}
            >
              <Undo2 aria-hidden />
              元に戻す
            </Button>
            <Button type="button" variant="primary" size="sm" onClick={save} disabled={!dirty} loading={saving}>
              {saving ? null : <Save aria-hidden />}
              区間を保存
            </Button>
          </div>
        </div>

        {picks.suggested.length > 0 || picks.chorus.length > 0 ? (
          <div className="flex flex-col gap-2">
            {picks.suggested.length > 0 ? (
              <div className="flex flex-wrap items-center gap-2">
                <span className="w-20 shrink-0 text-xs text-muted-foreground">おすすめ区間</span>
                {picks.suggested.map((pick) => (
                  <PickButton key={pick.key} pick={pick} active={isActive(pick.range)} onSelect={(range) => select(range)} />
                ))}
              </div>
            ) : null}
            {picks.chorus.length > 0 ? (
              <div className="flex flex-wrap items-center gap-2">
                <span className="w-20 shrink-0 text-xs text-muted-foreground">サビ候補</span>
                {picks.chorus.map((pick) => (
                  <PickButton key={pick.key} pick={pick} active={isActive(pick.range)} onSelect={(range) => select(range)} />
                ))}
              </div>
            ) : null}
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
