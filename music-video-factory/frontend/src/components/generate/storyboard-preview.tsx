"use client";

import { Clapperboard, Copy, Sparkles, TriangleAlert } from "lucide-react";
import * as React from "react";

import { sectionColor } from "@/components/audio/sections";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/toast";
import { errorMessage, previewStoryboard } from "@/lib/api";
import { formatTime, formatUsd } from "@/lib/format";
import type { PresetId, Storyboard, StoryboardScene } from "@/lib/types";
import { cn } from "@/lib/utils";

import { cameraAngleLabel, cameraMoveLabel, sceneTypeLabel, songSectionLabel } from "./vocab";

const SCENE_COUNTS = [4, 8, 12, 16] as const;

export interface StoryboardPreviewProps {
  projectId: string;
  preset: PresetId;
  customWorld: string | null;
  /** Blocks the request (e.g. custom preset without text). */
  disabledReason?: string | null;
  claudeConfigured: boolean | undefined;
}

interface PreviewResult {
  storyboard: Storyboard;
  /** Settings the preview was made with, to flag it as outdated. */
  signature: string;
}

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text);
    toast.success("プロンプトをコピーしました");
  } catch {
    toast.error("コピーできませんでした", "ブラウザがクリップボードへのアクセスを許可していません。");
  }
}

function SceneCard({ scene, index }: { scene: StoryboardScene; index: number }) {
  const [expanded, setExpanded] = React.useState(false);
  const section = songSectionLabel(scene.section);
  const tags = [sceneTypeLabel(scene.scene_type), cameraAngleLabel(scene.camera_angle), cameraMoveLabel(scene.camera_move)].filter(Boolean);
  return (
    <li className="flex flex-col gap-2.5 rounded-md border border-border bg-surface p-3.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs text-subtle-foreground">#{String(index + 1).padStart(2, "0")}</span>
        {section ? (
          <Badge tone="neutral">
            <span className="size-2 rounded-full" style={{ backgroundColor: sectionColor(scene.section ?? "") }} aria-hidden />
            {section}
          </Badge>
        ) : null}
        {typeof scene.start === "number" ? (
          <span className="font-mono text-2xs text-subtle-foreground tabular">
            {formatTime(scene.start)}
            {typeof scene.end === "number" ? `–${formatTime(scene.end)}` : ""}
          </span>
        ) : null}
        {tags.map((tag) => (
          <Badge key={tag} tone="outline">
            {tag}
          </Badge>
        ))}
      </div>
      <p className="text-sm leading-6 text-foreground">{scene.description_ja || "（説明なし）"}</p>
      <div className="flex flex-col gap-1">
        <div className="flex items-center justify-between gap-2">
          <span className="text-2xs font-medium text-subtle-foreground">プロンプト（英語・生成 AI に送信）</span>
          <div className="flex items-center gap-1">
            <Button variant="ghost" size="xs" onClick={() => setExpanded((value) => !value)} aria-expanded={expanded}>
              {expanded ? "折りたたむ" : "全文を表示"}
            </Button>
            <Button variant="ghost" size="icon-xs" onClick={() => void copyText(scene.prompt_en)} aria-label="プロンプトをコピー" title="コピー">
              <Copy aria-hidden />
            </Button>
          </div>
        </div>
        <div className="rounded bg-background px-2.5 py-2">
          <p lang="en" className={cn("font-mono text-2xs leading-4 break-words text-muted-foreground", !expanded && "line-clamp-4")}>
            {scene.prompt_en}
          </p>
        </div>
        {expanded && scene.negative_prompt_en ? (
          <p lang="en" className="font-mono text-2xs leading-4 break-words text-subtle-foreground">
            <span className="font-sans">除外: </span>
            {scene.negative_prompt_en}
          </p>
        ) : null}
      </div>
    </li>
  );
}

/** `POST /api/projects/{id}/storyboard` preview (not saved): scenes with Japanese descriptions and English prompts. */
export function StoryboardPreview({ projectId, preset, customWorld, disabledReason, claudeConfigured }: StoryboardPreviewProps) {
  const [sceneCount, setSceneCount] = React.useState<number>(8);
  const [result, setResult] = React.useState<PreviewResult | null>(null);
  const [error, setError] = React.useState<unknown>(null);
  const [loading, setLoading] = React.useState(false);
  const signature = JSON.stringify([preset, customWorld, sceneCount]);

  async function run() {
    setLoading(true);
    setError(null);
    try {
      const storyboard = await previewStoryboard(projectId, { preset, custom_world: customWorld, scenes: sceneCount });
      setResult({ storyboard, signature });
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }

  const storyboard = result?.storyboard;
  const outdated = result !== null && result.signature !== signature;

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <Clapperboard className="size-4 text-subtle-foreground" aria-hidden />
          絵コンテのプレビュー
        </CardTitle>
        <CardDescription>
          世界観と歌詞からシーン構成と生成プロンプトを試作します（保存されません。生成開始時に改めて作成されます）。
          {claudeConfigured === false ? " Claude が未設定のため、テンプレートで作成します。" : null}
        </CardDescription>
        <CardAction>
          <Select value={String(sceneCount)} onValueChange={(value) => setSceneCount(Number(value))} disabled={loading}>
            <SelectTrigger size="sm" className="w-28" aria-label="シーン数">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {SCENE_COUNTS.map((count) => (
                <SelectItem key={count} value={String(count)}>
                  {count} シーン
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button size="sm" variant="secondary" onClick={() => void run()} loading={loading} disabled={!!disabledReason}>
            {loading ? null : <Sparkles aria-hidden />}
            {storyboard ? "作り直す" : "プレビューを作成"}
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {disabledReason ? <p className="text-xs text-muted-foreground">{disabledReason}</p> : null}
        {error ? (
          <Alert tone="danger" title="絵コンテを作成できませんでした">
            {errorMessage(error)}
          </Alert>
        ) : null}
        {loading && !storyboard ? (
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            {Array.from({ length: 4 }, (_, i) => (
              <Skeleton key={i} className="h-40 w-full" />
            ))}
          </div>
        ) : storyboard ? (
          <div className="flex flex-col gap-4" aria-busy={loading || undefined}>
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <Badge tone="outline">{storyboard.source === "claude" ? `Claude${storyboard.model ? `（${storyboard.model}）` : ""}` : "テンプレート"}</Badge>
              <span>{storyboard.scenes.length} シーン</span>
              {storyboard.cost_usd > 0 ? <span>LLM 費用 {formatUsd(storyboard.cost_usd)}</span> : null}
              {outdated ? (
                <Badge tone="warning">
                  <TriangleAlert aria-hidden />
                  設定変更後のため古い内容です
                </Badge>
              ) : null}
            </div>
            <div className="flex flex-col gap-2 rounded-md border border-border bg-surface-raised/40 p-3.5">
              <div className="flex items-center gap-2">
                <span className="text-xs font-medium text-foreground">世界観</span>
                <span className="flex gap-1" aria-hidden>
                  {storyboard.world_bible.palette.slice(0, 5).map((color, i) => (
                    <span key={`${color}-${i}`} className="h-3 w-5 rounded-sm border border-white/10" style={{ backgroundColor: color }} />
                  ))}
                </span>
              </div>
              {storyboard.world_bible.summary_ja ? <p className="text-sm leading-6 text-foreground">{storyboard.world_bible.summary_ja}</p> : null}
              {storyboard.world_bible.consistency_tokens_en ? (
                <p lang="en" className="font-mono text-2xs leading-4 text-subtle-foreground">
                  <span className="font-sans">全シーン共通: </span>
                  {storyboard.world_bible.consistency_tokens_en}
                </p>
              ) : null}
            </div>
            {storyboard.warnings.length > 0 ? (
              <Alert tone="warning" title="注意">
                <ul className="list-disc pl-4">
                  {storyboard.warnings.map((warning) => (
                    <li key={warning}>{warning}</li>
                  ))}
                </ul>
              </Alert>
            ) : null}
            <ol className="grid grid-cols-1 gap-3 lg:grid-cols-2">
              {storyboard.scenes.map((scene, index) => (
                <SceneCard key={scene.id} scene={scene} index={index} />
              ))}
            </ol>
          </div>
        ) : !error ? (
          <EmptyState
            size="sm"
            icon={Clapperboard}
            title="まだプレビューはありません"
            description="「プレビューを作成」を押すと、シーンごとの日本語の説明と英語プロンプトを確認できます。"
          />
        ) : null}
      </CardContent>
    </Card>
  );
}
