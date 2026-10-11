"use client";

import { Sparkles } from "lucide-react";
import Link from "next/link";

import { generateHref } from "@/components/generate/links";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { usePresets } from "@/hooks/use-capabilities";
import { useWorldEstimate } from "@/hooks/use-world-estimate";
import type { ProjectOut, WorldEstimate } from "@/lib/types";

/** Japanese names of the estimate's emotion / theme ids (same as the backend's `_EMOTION_JA`). */
export const EMOTION_LABELS: Record<string, string> = {
  melancholic: "切ない",
  nostalgic: "懐かしい",
  hopeful: "前向き",
  energetic: "エネルギッシュ",
  romantic: "ロマンチック",
  calm: "穏やか",
  dark: "ダーク",
  dreamy: "幻想的",
  night: "夜・都会",
  nature: "自然",
};

export function emotionLabel(id: string): string {
  return EMOTION_LABELS[id] ?? id;
}

const SOURCE_LABELS: { key: keyof WorldEstimate["sources"]; label: string }[] = [
  { key: "audio", label: "音源" },
  { key: "lyrics", label: "歌詞" },
  { key: "user", label: "ムード・目的" },
];

/** 感情・世界観推定: what the audio mood, the lyrics and the project's mood/purpose suggest. */
export function WorldEstimateCard({ project, analysisStatus }: { project: ProjectOut; analysisStatus?: string }) {
  const { data } = useWorldEstimate(project.id, `${project.updated_at}:${analysisStatus ?? project.analysis_status}`);
  const { presets } = usePresets();
  if (!data) return null;
  const presetName = (id: string) => presets.find((preset) => preset.id === id)?.name_ja ?? id;
  const tags = [...data.emotions, ...data.themes];

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Sparkles className="size-4 text-muted-foreground" aria-hidden />
          感情・世界観の推定
        </CardTitle>
        <CardDescription>
          楽曲の解析結果・歌詞・プロジェクトのムードと目的から推定した手がかりです（外部 API は使いません）。生成画面の世界観の初期値に使われます。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 text-sm">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-xs text-muted-foreground">推定される感情</span>
          {tags.length > 0 ? (
            tags.map((id) => (
              <Badge key={id} tone="outline">
                {emotionLabel(id)}
              </Badge>
            ))
          ) : (
            <span className="text-xs text-muted-foreground">手がかりが少ないため推定できませんでした</span>
          )}
        </div>
        <dl className="grid grid-cols-1 gap-1.5 text-xs sm:grid-cols-3">
          {SOURCE_LABELS.map(({ key, label }) => (
            <div key={key} className="flex min-w-0 gap-2">
              <dt className="shrink-0 text-muted-foreground">{label}</dt>
              <dd className="truncate text-foreground">
                {data.sources[key].length > 0 ? data.sources[key].map(emotionLabel).join("・") : "—"}
              </dd>
            </div>
          ))}
        </dl>
        <p className="text-xs leading-5 text-muted-foreground">
          おすすめの世界観:{" "}
          <span className="text-foreground">{data.recommended_presets.map(presetName).join("・")}</span>
          {data.analysis_used ? null : "（楽曲解析が終わると音源の手がかりも加わります）"}{" "}
          <Link href={generateHref(project.id)} className="font-medium text-foreground underline underline-offset-2">
            生成設定を開く
          </Link>
        </p>
      </CardContent>
    </Card>
  );
}
