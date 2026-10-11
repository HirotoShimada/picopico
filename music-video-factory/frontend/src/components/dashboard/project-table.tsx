"use client";

import { FolderPlus, Music } from "lucide-react";
import Link from "next/link";
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Card, CardAction, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/ui/status-badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Thumbnail } from "@/components/ui/thumbnail";
import { useNow } from "@/hooks/use-now";
import type { ApiError } from "@/lib/api";
import { formatCount, formatDateTime, formatRelativeTime } from "@/lib/format";
import type { ProjectSummary } from "@/lib/types";

interface ProjectTableProps {
  projects: ProjectSummary[] | undefined;
  error: ApiError | undefined;
  loading: boolean;
  onRetry: () => void;
}

export function ProjectTable({ projects, error, loading, onRetry }: ProjectTableProps) {
  const now = useNow();
  const sorted = React.useMemo(
    () => (projects ? [...projects].sort((a, b) => b.updated_at.localeCompare(a.updated_at)) : []),
    [projects],
  );

  let body: React.ReactNode;
  if (loading) {
    body = (
      <div className="flex flex-col gap-2 px-5 pb-5">
        {Array.from({ length: 4 }, (_, i) => (
          <Skeleton key={i} className="h-14 w-full" />
        ))}
      </div>
    );
  } else if (error && !projects) {
    body = (
      <div className="px-5 pb-5">
        <ErrorState error={error} onRetry={onRetry} size="sm" />
      </div>
    );
  } else if (sorted.length === 0) {
    body = (
      <div className="px-5 pb-5">
        <EmptyState
          icon={Music}
          title="まだプロジェクトがありません"
          description="楽曲名とアーティスト名を入力し、音源をアップロードするとプロジェクトが作成されます。"
          action={
            <Button variant="primary" asChild>
              <Link href="/projects/new">
                <FolderPlus aria-hidden />
                最初のプロジェクトを作成
              </Link>
            </Button>
          }
        />
      </div>
    );
  } else {
    body = (
      <Table>
        <TableHeader>
          <TableRow className="hover:bg-transparent">
            <TableHead className="w-[52%]">楽曲 / アーティスト</TableHead>
            <TableHead>解析</TableHead>
            <TableHead className="text-right">歌詞</TableHead>
            <TableHead className="text-right">動画</TableHead>
            <TableHead className="text-right">更新</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {sorted.map((project) => (
            <TableRow key={project.id} className="relative">
              <TableCell>
                <div className="flex items-center gap-3">
                  <Thumbnail src={project.cover_url} alt="" className="w-8 shrink-0 rounded" fallback={<Music className="size-3.5" aria-hidden />} />
                  <div className="flex min-w-0 flex-col">
                    <Link
                      href={`/projects/${project.id}`}
                      className="focus-ring-custom truncate font-medium text-foreground outline-none after:absolute after:inset-0 focus-visible:underline"
                    >
                      {project.title}
                    </Link>
                    <span className="truncate text-xs text-muted-foreground">
                      {project.artist_name}
                      {project.has_audio ? null : <span className="ml-2 text-warning-foreground">音源未アップロード</span>}
                    </span>
                  </div>
                </div>
              </TableCell>
              <TableCell>
                <StatusBadge kind="analysis" status={project.analysis_status} />
              </TableCell>
              <TableCell className="text-right tabular text-muted-foreground">
                {project.lyric_line_count > 0 ? `${formatCount(project.lyric_line_count)} 行` : "—"}
              </TableCell>
              <TableCell className="text-right tabular">{formatCount(project.video_count)}</TableCell>
              <TableCell className="text-right text-xs whitespace-nowrap text-muted-foreground" title={formatDateTime(project.updated_at)}>
                {now ? formatRelativeTime(project.updated_at, now) : formatDateTime(project.updated_at)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>プロジェクト</CardTitle>
        <CardDescription>
          {projects && projects.length > 0 ? `${formatCount(projects.length)} 件 ・ 更新順` : "楽曲ごとに素材・歌詞・生成ジョブを管理します"}
        </CardDescription>
        {projects && projects.length > 0 ? (
          <CardAction>
            <Button variant="secondary" size="sm" asChild>
              <Link href="/projects/new">
                <FolderPlus aria-hidden />
                新規作成
              </Link>
            </Button>
          </CardAction>
        ) : null}
      </CardHeader>
      {body}
    </Card>
  );
}
