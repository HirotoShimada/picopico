"use client";

import { Clapperboard, Film, ListChecks, Music } from "lucide-react";
import Link from "next/link";
import * as React from "react";

import { SimpleTooltip } from "@/components/ui/tooltip";
import { useProjectBatches } from "@/hooks/use-batches";
import { useProject } from "@/hooks/use-projects";
import { isBatchActive } from "@/lib/status";
import { cn } from "@/lib/utils";

/** Keys of the project-scoped sections. */
export type ProjectNavKey = "assets" | "generate" | "jobs" | "videos";

export interface ProjectNavProps {
  projectId: string;
  active: ProjectNavKey;
  /**
   * Batch shown by the "生成ジョブ" link. Defaults to the project's most recent
   * batch; pass the current batch on `/batches/[id]`.
   */
  batchId?: string;
  className?: string;
}

export function projectSectionHref(projectId: string, key: Exclude<ProjectNavKey, "jobs">): string {
  const base = `/projects/${encodeURIComponent(projectId)}`;
  if (key === "generate") return `${base}/generate`;
  if (key === "videos") return `${base}/videos`;
  return base;
}

const itemClasses =
  "focus-ring-custom -mb-px inline-flex h-10 items-center gap-2 border-b-2 px-3 text-sm whitespace-nowrap outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring/40";

/**
 * Project sub-navigation: 楽曲・歌詞・素材・権利 / 生成設定 / 生成ジョブ / 動画一覧.
 * Shared by `/projects/[id]`, `/projects/[id]/generate`, `/batches/[id]` and `/projects/[id]/videos`.
 */
export function ProjectNav({ projectId, active, batchId, className }: ProjectNavProps) {
  const { data: project } = useProject(projectId);
  const { data: batches } = useProjectBatches(projectId);

  const latestBatchId = React.useMemo(() => {
    if (!batches || batches.length === 0) return null;
    return [...batches].sort((a, b) => b.created_at.localeCompare(a.created_at))[0].id;
  }, [batches]);
  const activeJobs = batches?.filter((batch) => isBatchActive(batch.status)).length ?? 0;
  const jobsBatchId = batchId ?? latestBatchId;
  const videoCount = project?.counts.videos;

  const items: {
    key: ProjectNavKey;
    label: string;
    icon: React.ComponentType<{ className?: string }>;
    href: string | null;
    extra?: React.ReactNode;
    disabledReason?: string;
  }[] = [
    { key: "assets", label: "楽曲・歌詞・素材・権利", icon: Music, href: projectSectionHref(projectId, "assets") },
    { key: "generate", label: "生成設定", icon: Clapperboard, href: projectSectionHref(projectId, "generate") },
    {
      key: "jobs",
      label: "生成ジョブ",
      icon: ListChecks,
      href: jobsBatchId ? `/batches/${encodeURIComponent(jobsBatchId)}` : null,
      disabledReason: batches ? "まだ生成ジョブはありません。生成設定から開始してください。" : "読み込み中…",
      extra:
        activeJobs > 0 ? (
          <span className="inline-flex items-center gap-1 text-2xs text-blue-300">
            <span className="size-1.5 rounded-full bg-primary" aria-hidden />
            {activeJobs}件実行中
          </span>
        ) : null,
    },
    {
      key: "videos",
      label: "動画一覧",
      icon: Film,
      href: projectSectionHref(projectId, "videos"),
      extra:
        videoCount !== undefined && videoCount > 0 ? (
          <span className="rounded bg-surface-raised px-1.5 text-2xs leading-4 text-muted-foreground tabular">{videoCount}</span>
        ) : null,
    },
  ];

  return (
    <nav aria-label="プロジェクト" className={cn("mb-6 flex items-end gap-1 overflow-x-auto border-b border-border", className)}>
      {items.map((item) => {
        const Icon = item.icon;
        const isActive = item.key === active;
        const content = (
          <>
            <Icon className={cn("size-4", isActive ? "text-foreground" : "text-subtle-foreground")} />
            {item.label}
            {item.extra}
          </>
        );
        if (!item.href) {
          return (
            <SimpleTooltip key={item.key} content={item.disabledReason}>
              <span
                tabIndex={0}
                aria-disabled
                className={cn(itemClasses, "cursor-not-allowed border-transparent text-subtle-foreground/70")}
              >
                {content}
              </span>
            </SimpleTooltip>
          );
        }
        return (
          <Link
            key={item.key}
            href={item.href}
            aria-current={isActive ? "page" : undefined}
            className={cn(
              itemClasses,
              isActive
                ? "border-foreground font-medium text-foreground"
                : "border-transparent text-muted-foreground hover:border-border-strong hover:text-foreground",
            )}
          >
            {content}
          </Link>
        );
      })}
    </nav>
  );
}
