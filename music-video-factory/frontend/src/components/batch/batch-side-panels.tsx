"use client";

import { Clapperboard, Film, Layers } from "lucide-react";
import * as React from "react";

import { clipSourceLabel } from "@/components/materials/clip-labels";
import { colorGradeName, COLOR_GRADES_FALLBACK, lyricStyleChoiceName } from "@/components/generate/text-style";
import { reuseLabel, startStrategyLabel } from "@/components/generate/vocab";
import { LYRIC_STYLES_FALLBACK } from "@/components/lyrics/lyric-styles";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { isRunway, PoweredByRunway } from "@/components/ui/powered-by-runway";
import { StatusBadge } from "@/components/ui/status-badge";
import { Thumbnail } from "@/components/ui/thumbnail";
import { useCapabilities, usePresets } from "@/hooks/use-capabilities";
import { providerLabel } from "@/lib/constants";
import { formatCount, formatDuration, formatTime, formatUsd } from "@/lib/format";
import type { BatchDetailOut, ClipOut } from "@/lib/types";

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-0.5">
      <dt className="shrink-0 text-xs text-muted-foreground">{label}</dt>
      <dd className="min-w-0 truncate text-right text-xs text-foreground">{value}</dd>
    </div>
  );
}

/** What the batch was started with (`BatchOut.settings` + estimate). */
export function BatchSettingsCard({ batch }: { batch: BatchDetailOut }) {
  const { data: capabilities } = useCapabilities();
  const { presets } = usePresets();
  const settings = batch.settings;
  const provider = capabilities?.providers.find((p) => p.id === settings.provider);
  const model = batch.estimate?.model ?? settings.model ?? provider?.default_model ?? null;
  // mock / library have a single built-in model; naming it only repeats the provider.
  const modelName = provider && provider.kind !== "api" ? null : (provider?.models.find((m) => m.id === model)?.name ?? model);
  // `auto` is stored as is; each video's plan holds the style it was given.
  const lyricStyle = lyricStyleChoiceName(settings.lyric_style, capabilities?.lyric_styles ?? LYRIC_STYLES_FALLBACK);

  return (
    <Card>
      <CardHeader>
        <CardTitle>生成設定</CardTitle>
      </CardHeader>
      <CardContent>
        <dl className="flex flex-col">
          <Row label="本数 × 長さ" value={`${formatCount(settings.count)}本 × ${formatDuration(settings.duration_sec)}`} />
          <Row label="世界観" value={presets.find((p) => p.id === settings.preset)?.name_ja ?? settings.preset} />
          <Row label="生成エンジン" value={`${provider?.name ?? providerLabel(settings.provider)}${modelName ? ` / ${modelName}` : ""}`} />
          <Row label="歌詞スタイル" value={lyricStyle} />
          <Row label="カラー" value={colorGradeName(settings.color_grade, capabilities?.color_grades ?? COLOR_GRADES_FALLBACK)} />
          <Row
            label="開始位置"
            value={`${startStrategyLabel(settings.start_strategy)}${settings.start_strategy === "custom" && settings.start_sec !== null ? `（${formatTime(settings.start_sec)}）` : ""}`}
          />
          <Row label="素材の再利用" value={reuseLabel(settings.reuse)} />
          {settings.max_new_clips !== null && settings.max_new_clips !== undefined ? (
            <Row label="新規生成の上限" value={`${formatCount(settings.max_new_clips)}本`} />
          ) : null}
          {batch.estimate ? (
            <>
              <Row label="見積もり" value={formatUsd(batch.estimate.total_usd)} />
              <Row
                label="予算上限"
                value={batch.estimate.budget_limit_usd !== null ? formatUsd(batch.estimate.budget_limit_usd) : "なし"}
              />
            </>
          ) : null}
        </dl>
        {isRunway(settings.provider) ? <PoweredByRunway className="mt-3" /> : null}
      </CardContent>
    </Card>
  );
}

const MAX_CLIPS_SHOWN = 24;

function ClipTile({ clip }: { clip: ClipOut }) {
  return (
    <li className="flex min-w-0 flex-col gap-1">
      <Thumbnail
        src={clip.thumbnail_url}
        alt={`素材 ${clip.scene_id ?? clip.id} のサムネイル`}
        className="rounded"
        fallback={<Film className="size-3.5" aria-hidden />}
        overlay={
          clip.status !== "ready" ? (
            <div className="absolute inset-x-1 bottom-1 flex justify-center">
              <StatusBadge kind="clip" status={clip.status} showIcon={clip.status !== "pending"} />
            </div>
          ) : null
        }
        title={clip.error ?? clip.prompt ?? undefined}
      />
      <span className="truncate text-2xs text-subtle-foreground">
        {clip.scene_id ?? (clip.source === "upload" ? "素材" : clipSourceLabel(clip))}
        {clip.duration ? ` ・ ${formatDuration(clip.duration)}` : ""}
      </span>
    </li>
  );
}

/** The clip pool of the batch (generated + reused). */
export function BatchClipsCard({ batch }: { batch: BatchDetailOut }) {
  const clips = batch.clips ?? [];
  const shown = clips.slice(0, MAX_CLIPS_SHOWN);
  const { clip_counts: counts } = batch;
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <Layers className="size-4 text-subtle-foreground" aria-hidden />
          素材
        </CardTitle>
        <CardDescription>
          {counts.total > 0
            ? `全 ${formatCount(counts.total)} 本（新規 ${formatCount(Math.max(0, counts.total - counts.reused))} ・ 再利用 ${formatCount(counts.reused)}）`
            : "構成の計画後に表示されます"}
        </CardDescription>
      </CardHeader>
      {shown.length > 0 ? (
        <CardContent className="flex flex-col gap-2">
          <ul className="grid grid-cols-4 gap-2">
            {shown.map((clip) => (
              <ClipTile key={clip.id} clip={clip} />
            ))}
          </ul>
          {clips.length > shown.length ? (
            <p className="text-2xs text-subtle-foreground">ほか {formatCount(clips.length - shown.length)} 本</p>
          ) : null}
          {counts.failed > 0 ? (
            <p className="text-xs text-danger-foreground">{formatCount(counts.failed)} 本の素材の生成に失敗しました。</p>
          ) : null}
        </CardContent>
      ) : null}
    </Card>
  );
}

/** World summary and scene list of the batch storyboard (collapsed by default). */
export function BatchStoryboardCard({ batch }: { batch: BatchDetailOut }) {
  const storyboard = batch.storyboard;
  if (!storyboard) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <Clapperboard className="size-4 text-subtle-foreground" aria-hidden />
          絵コンテ
        </CardTitle>
        <CardDescription>
          {storyboard.source === "claude" ? "Claude" : "テンプレート"}で作成 ・ {formatCount(storyboard.scenes.length)} シーン
          {storyboard.cost_usd > 0 ? ` ・ ${formatUsd(storyboard.cost_usd)}` : ""}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {storyboard.world_bible.summary_ja ? <p className="text-xs leading-5 text-foreground/90">{storyboard.world_bible.summary_ja}</p> : null}
        <details className="group">
          <summary className="cursor-pointer text-xs text-muted-foreground select-none hover:text-foreground">シーン一覧を表示</summary>
          <ol className="mt-2 flex flex-col gap-2">
            {storyboard.scenes.map((scene, index) => (
              <li key={scene.id} className="flex gap-2 text-xs leading-5">
                <span className="font-mono text-subtle-foreground">{String(index + 1).padStart(2, "0")}</span>
                <span className="text-foreground/90">{scene.description_ja}</span>
              </li>
            ))}
          </ol>
        </details>
      </CardContent>
    </Card>
  );
}
