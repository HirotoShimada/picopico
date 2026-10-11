"use client";

import { Play } from "lucide-react";
import * as React from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { formatBpm, formatDuration, formatPercent, formatTime } from "@/lib/format";
import type { AudioAnalysis } from "@/lib/types";

import { useAudioPlayer, useAudioState } from "./audio-player";
import { MODE_LABELS, moodTagLabel, sectionColor, sectionLabel, TEMPO_CLASS_LABELS } from "./sections";

function Meter({ value, label }: { value: number; label: string }) {
  const pct = Math.round(Math.min(1, Math.max(0, value)) * 100);
  return (
    <div className="flex items-center gap-2" role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}>
      <div className="h-1.5 w-16 overflow-hidden rounded-full bg-zinc-800">
        <div className="h-full rounded-full bg-zinc-400" style={{ width: `${pct}%` }} />
      </div>
      <span className="font-mono text-2xs tabular text-muted-foreground">{pct}%</span>
    </div>
  );
}

function Stat({ label, value, hint }: { label: string; value: React.ReactNode; hint?: React.ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="truncate text-base font-semibold tracking-tight text-foreground tabular">{value}</dd>
      {hint ? <dd className="truncate text-2xs text-subtle-foreground">{hint}</dd> : null}
    </div>
  );
}

function formatSampleRate(rate: number | null | undefined): string {
  if (!rate) return "—";
  return `${(rate / 1000).toFixed(rate % 1000 === 0 ? 0 : 1)} kHz`;
}

function formatChannels(channels: number | null | undefined): string {
  if (channels === 1) return "モノラル";
  if (channels === 2) return "ステレオ";
  return channels ? `${channels} ch` : "—";
}

function formatBitRate(bitRate: number | null | undefined): string {
  return bitRate ? `${Math.round(bitRate / 1000)} kbps` : "—";
}

/** BPM / length / mood / format, plus the detected sections (click to play). */
export function AnalysisSummary({ analysis }: { analysis: AudioAnalysis }) {
  const player = useAudioPlayer();
  const audio = useAudioState();
  const mood = analysis.mood;

  return (
    <Card>
      <CardHeader>
        <CardTitle>解析結果</CardTitle>
        <CardDescription>自動解析による推定値です。区間は上の波形で自由に変更できます。</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        <dl className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3 2xl:grid-cols-6">
          <Stat label="テンポ" value={formatBpm(analysis.bpm)} hint={`信頼度 ${formatPercent(analysis.bpm_confidence)}`} />
          <Stat label="長さ" value={formatDuration(analysis.duration)} hint={`${analysis.downbeats.length} 小節 ・ ${analysis.beats.length} 拍`} />
          <Stat label="テンポ感" value={TEMPO_CLASS_LABELS[mood.tempo_class] ?? mood.tempo_class} />
          <Stat label="調性（推定）" value={MODE_LABELS[mood.mode_guess] ?? mood.mode_guess} />
          <div className="flex min-w-0 flex-col gap-1">
            <dt className="text-xs text-muted-foreground">エネルギー</dt>
            <dd>
              <Meter value={mood.energy} label="エネルギー" />
            </dd>
          </div>
          <div className="flex min-w-0 flex-col gap-1">
            <dt className="text-xs text-muted-foreground">明るさ</dt>
            <dd>
              <Meter value={mood.brightness} label="明るさ" />
            </dd>
          </div>
        </dl>

        <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-xs text-muted-foreground">
          <div className="flex flex-wrap items-center gap-1.5">
            <span>ムード</span>
            {mood.tags.length > 0 ? (
              mood.tags.map((tag) => (
                <Badge key={tag} tone="outline">
                  {moodTagLabel(tag)}
                </Badge>
              ))
            ) : (
              <span>—</span>
            )}
          </div>
          <span>
            形式 <span className="text-foreground">{analysis.codec || "—"}</span> ・ {formatSampleRate(analysis.sample_rate)} ・{" "}
            {formatChannels(analysis.channels)} ・ {formatBitRate(analysis.bit_rate)}
          </span>
        </div>

        {analysis.sections.length > 0 ? (
          <div className="overflow-hidden rounded-md border border-border">
            <table className="w-full text-sm">
              <caption className="sr-only">検出したセクション</caption>
              <thead className="bg-surface-raised text-xs text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 text-left font-medium">セクション</th>
                  <th className="px-3 py-2 text-left font-medium">時間</th>
                  <th className="px-3 py-2 text-left font-medium">長さ</th>
                  <th className="px-3 py-2 text-left font-medium">エネルギー</th>
                  <th className="px-3 py-2 text-right font-medium">
                    <span className="sr-only">再生</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {analysis.sections.map((section, index) => (
                  <tr key={`${section.start}-${index}`} className="border-t border-border">
                    <td className="px-3 py-1.5">
                      <span className="flex items-center gap-2">
                        <span className="size-2.5 rounded-sm" style={{ backgroundColor: sectionColor(section.label) }} aria-hidden />
                        {sectionLabel(section.label)}
                        {section.is_chorus_candidate ? <Badge tone="warning">サビ候補</Badge> : null}
                      </span>
                    </td>
                    <td className="px-3 py-1.5 font-mono text-xs tabular text-muted-foreground">
                      {formatTime(section.start)}–{formatTime(section.end)}
                    </td>
                    <td className="px-3 py-1.5 text-xs tabular text-muted-foreground">{formatDuration(section.end - section.start)}</td>
                    <td className="px-3 py-1.5">
                      <Meter value={section.energy} label={`${sectionLabel(section.label)}のエネルギー`} />
                    </td>
                    <td className="px-3 py-1.5 text-right">
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-xs"
                        disabled={!audio.hasSource}
                        onClick={() => player.playRange(section.start, section.end)}
                        aria-label={`${sectionLabel(section.label)}（${formatTime(section.start)}〜）を再生`}
                      >
                        <Play aria-hidden />
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
