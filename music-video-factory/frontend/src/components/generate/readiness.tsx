"use client";

import { ArrowRight, CircleAlert, CircleCheck, LoaderCircle, TriangleAlert } from "lucide-react";
import Link from "next/link";
import * as React from "react";

import { formatBpm, formatCount } from "@/lib/format";
import type { Estimate, ProjectOut } from "@/lib/types";
import { cn } from "@/lib/utils";

import { projectTabHref } from "./links";

export type ReadinessLevel = "ok" | "blocked" | "warning" | "pending";

export interface ReadinessItem {
  id: string;
  level: ReadinessLevel;
  title: string;
  description?: string;
  href?: string;
  linkLabel?: string;
}

export interface ReadinessInput {
  project: ProjectOut;
  providerName: string;
  /** Why the selected provider/model cannot start, if anything. */
  providerReason: string | null;
  formInvalid: boolean;
  estimate: Estimate | undefined;
  estimateError: boolean;
  estimateStale: boolean;
}

/**
 * Why no estimate can be computed yet (the planner needs the analysis), or
 * `null`. Rights are not required for an estimate.
 */
export function estimateUnavailableReason(project: ProjectOut): string | null {
  if (!project.audio) return "音源をアップロードして楽曲解析が完了すると、費用を見積もれます。";
  if (project.analysis_status === "queued" || project.analysis_status === "running") {
    return "楽曲解析の完了後に見積もりを表示します。";
  }
  if (project.analysis_status !== "done") return "楽曲解析が完了していないため、見積もりを計算できません。";
  return null;
}

function projectChecks(project: ProjectOut): ReadinessItem[] {
  const items: ReadinessItem[] = [];
  const id = project.id;

  if (!project.audio) {
    items.push({
      id: "audio",
      level: "blocked",
      title: "音源がアップロードされていません",
      description: "楽曲ファイル（MP3 / WAV など）をアップロードすると解析が始まります。",
      href: projectTabHref(id, "analysis"),
      linkLabel: "音源をアップロード",
    });
  } else {
    items.push({ id: "audio", level: "ok", title: `音源: ${project.audio.filename}` });
    switch (project.analysis_status) {
      case "done":
        items.push({
          id: "analysis",
          level: "ok",
          title: `楽曲解析済み${project.analysis ? `（${formatBpm(project.analysis.bpm)}）` : ""}`,
        });
        break;
      case "queued":
      case "running":
        items.push({
          id: "analysis",
          level: "pending",
          title: "楽曲を解析しています",
          description: "解析が終わると生成を開始できます（通常 1 分以内）。",
          href: projectTabHref(id, "analysis"),
          linkLabel: "解析状況を見る",
        });
        break;
      case "failed":
        items.push({
          id: "analysis",
          level: "blocked",
          title: "楽曲解析に失敗しました",
          description: project.analysis_error ?? "音源を確認し、解析を再実行してください。",
          href: projectTabHref(id, "analysis"),
          linkLabel: "解析を再実行",
        });
        break;
      default:
        items.push({
          id: "analysis",
          level: "blocked",
          title: "楽曲が解析されていません",
          href: projectTabHref(id, "analysis"),
          linkLabel: "楽曲解析タブへ",
        });
    }
  }

  if (project.rights_confirmed_at) {
    items.push({ id: "rights", level: "ok", title: "権利確認済み" });
  } else {
    items.push({
      id: "rights",
      level: "blocked",
      title: "権利確認が完了していません",
      description: "音源・歌詞の権利や肖像の扱いなど、すべての項目の確認が必要です。",
      href: projectTabHref(id, "rights"),
      linkLabel: "権利確認タブへ",
    });
  }
  return items;
}

function lyricsCheck(project: ProjectOut): ReadinessItem {
  const lines = project.lyric_lines;
  if (lines.length === 0) {
    return {
      id: "lyrics",
      level: "warning",
      title: "歌詞が登録されていません",
      description: "このまま生成すると歌詞なしの動画になります。",
      href: projectTabHref(project.id, "lyrics"),
      linkLabel: "歌詞タブへ",
    };
  }
  const untimed = lines.filter((line) => line.start === null).length;
  if (untimed > 0) {
    return {
      id: "lyrics",
      level: "warning",
      title: `タイミング未設定の歌詞が ${formatCount(untimed)} 行あります`,
      description: "タイミングのない行は動画に表示されません。歌詞タブで自動タイミングを実行できます。",
      href: projectTabHref(project.id, "lyrics"),
      linkLabel: "歌詞タブへ",
    };
  }
  return { id: "lyrics", level: "ok", title: `歌詞 ${formatCount(lines.length)} 行（タイミング設定済み）` };
}

/** Every check shown next to the 生成開始 button, in display order. */
export function computeReadiness(input: ReadinessInput): ReadinessItem[] {
  const items = [...projectChecks(input.project), lyricsCheck(input.project)];

  items.push(
    input.providerReason
      ? { id: "provider", level: "blocked", title: `${input.providerName} は使用できません`, description: input.providerReason }
      : { id: "provider", level: "ok", title: `生成エンジン: ${input.providerName}` },
  );

  if (input.formInvalid) {
    items.push({ id: "form", level: "blocked", title: "入力内容にエラーがあります", description: "赤字の項目を修正してください。" });
  }

  const projectReady = !items.some((item) => (item.level === "blocked" || item.level === "pending") && item.id !== "provider");
  if (projectReady && !input.formInvalid) {
    if (input.estimateStale) {
      items.push({ id: "estimate", level: "pending", title: "見積もりを計算しています" });
    } else if (input.estimateError) {
      items.push({ id: "estimate", level: "blocked", title: "見積もりを取得できません", description: "見積もりパネルのエラーを確認してください。" });
    } else if (input.estimate && !input.estimate.within_budget) {
      items.push({
        id: "estimate",
        level: "blocked",
        title: "見積もりが予算上限を超えています",
        description: "本数・長さ・再利用方針を見直すか、予算上限を引き上げてください。",
      });
    } else if (input.estimate) {
      items.push({ id: "estimate", level: "ok", title: "見積もりは予算内です" });
    }
  }
  return items;
}

export function isReady(items: readonly ReadinessItem[]): boolean {
  return items.every((item) => item.level === "ok" || item.level === "warning");
}

const LEVEL_ICONS: Record<ReadinessLevel, { icon: React.ComponentType<{ className?: string }>; className: string }> = {
  ok: { icon: CircleCheck, className: "text-success" },
  blocked: { icon: CircleAlert, className: "text-danger" },
  warning: { icon: TriangleAlert, className: "text-warning" },
  pending: { icon: LoaderCircle, className: "animate-spin text-muted-foreground" },
};

export function ReadinessList({ items, className }: { items: readonly ReadinessItem[]; className?: string }) {
  return (
    <ul className={cn("flex flex-col gap-2", className)} aria-label="生成前チェック">
      {items.map((item) => {
        const { icon: Icon, className: iconClass } = LEVEL_ICONS[item.level];
        return (
          <li key={item.id} className="flex items-start gap-2.5">
            <Icon className={cn("mt-0.5 size-4 shrink-0", iconClass)} aria-hidden />
            <div className="flex min-w-0 flex-col gap-0.5">
              <span
                className={cn(
                  "text-xs leading-5",
                  item.level === "blocked" ? "font-medium text-danger-foreground" : item.level === "ok" ? "text-muted-foreground" : "text-foreground",
                )}
              >
                <span className="sr-only">
                  {item.level === "ok" ? "OK: " : item.level === "blocked" ? "要対応: " : item.level === "warning" ? "注意: " : "処理中: "}
                </span>
                {item.title}
              </span>
              {item.description ? <span className="text-2xs leading-4 text-subtle-foreground">{item.description}</span> : null}
              {item.href ? (
                <Link
                  href={item.href}
                  className="focus-ring-custom inline-flex w-fit items-center gap-1 rounded text-2xs font-medium text-foreground underline-offset-2 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring/40"
                >
                  {item.linkLabel ?? "開く"}
                  <ArrowRight className="size-3" aria-hidden />
                </Link>
              ) : null}
            </div>
          </li>
        );
      })}
    </ul>
  );
}
