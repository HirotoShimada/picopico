"use client";

import { Film, Pencil, RotateCw } from "lucide-react";
import Link from "next/link";
import * as React from "react";

import { editorHref } from "@/components/generate/links";
import { QualityBadge } from "@/components/videos/quality-summary";
import { videoTitle, useVideoActions } from "@/components/videos/video-actions";
import { Button } from "@/components/ui/button";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Progress } from "@/components/ui/progress";
import { StatusBadge } from "@/components/ui/status-badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Thumbnail } from "@/components/ui/thumbnail";
import { formatDuration, formatPercent, formatTime } from "@/lib/format";
import type { VideoOut } from "@/lib/types";
import { cn } from "@/lib/utils";

function ProgressCell({ video }: { video: VideoOut }) {
  switch (video.status) {
    case "rendering":
      return (
        <div className="flex items-center gap-2">
          <Progress value={video.progress * 100} size="sm" className="w-24" aria-label={`${videoTitle(video)} のレンダリング進捗`} />
          <span className="w-9 font-mono text-2xs text-muted-foreground tabular">{formatPercent(video.progress)}</span>
        </div>
      );
    case "done":
      return video.quality ? <QualityBadge report={video.quality} /> : <span className="text-xs text-muted-foreground">—</span>;
    case "queued":
    case "waiting_clips":
      return <Progress value={0} size="sm" className="w-24" aria-label="未開始" />;
    default:
      return <span className="text-xs text-subtle-foreground">—</span>;
  }
}

export interface BatchVideoTableProps {
  videos: readonly VideoOut[];
  /** Hide retry while the whole batch is being cancelled etc. */
  disabled?: boolean;
}

/** One row per output video: status, progress, error and retry / edit actions. */
export function BatchVideoTable({ videos, disabled }: BatchVideoTableProps) {
  const actions = useVideoActions();
  const sorted = React.useMemo(() => [...videos].sort((a, b) => a.index - b.index), [videos]);

  return (
    <Card className="self-start">
      <CardHeader>
        <CardTitle>動画</CardTitle>
        <CardDescription>失敗した動画は個別に再試行できます。完成した動画はエディターで微調整できます。</CardDescription>
      </CardHeader>
      {sorted.length === 0 ? (
        <div className="px-5 pb-5">
          <EmptyState size="sm" icon={Film} title="動画の構成を準備しています" description="構成の計画が終わると、ここに動画が並びます。" />
        </div>
      ) : (
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead className="w-14">動画</TableHead>
              <TableHead className="min-w-48">タイトル</TableHead>
              <TableHead className="w-28">状態</TableHead>
              <TableHead className="w-40">進捗</TableHead>
              <TableHead className="w-24">区間</TableHead>
              <TableHead className="w-px text-right">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {sorted.map((video) => {
              const canRetry = video.status === "failed" || video.status === "cancelled";
              const busy = actions.pending[video.id];
              return (
                <TableRow key={video.id} className={cn(video.status === "failed" && "bg-danger-soft/40")}>
                  <TableCell>
                    <Thumbnail
                      src={video.thumbnail_url}
                      alt={`${videoTitle(video)} のサムネイル`}
                      className="w-10 rounded"
                      fallback={<Film className="size-3.5" aria-hidden />}
                    />
                  </TableCell>
                  <TableCell className="max-w-0">
                    <div className="flex min-w-0 flex-col gap-0.5">
                      <span className="truncate text-sm text-foreground">
                        <span className="mr-2 font-mono text-xs text-subtle-foreground">#{String(video.index + 1).padStart(2, "0")}</span>
                        {videoTitle(video)}
                        {video.render_version > 1 ? <span className="ml-2 text-2xs text-subtle-foreground">v{video.render_version}</span> : null}
                      </span>
                      {video.error ? (
                        <span
                          className={cn(
                            "line-clamp-2 text-xs leading-5",
                            video.status === "done" ? "text-warning-foreground" : video.status === "cancelled" ? "text-muted-foreground" : "text-danger-foreground",
                          )}
                          title={video.error}
                        >
                          {video.error}
                        </span>
                      ) : null}
                      {video.plan?.substitutions ? (
                        <span className="text-2xs text-subtle-foreground" title="予定していた素材を生成できなかったショットを、同じジョブの別の素材で補っています">
                          代替素材 {video.plan.substitutions} ショット
                        </span>
                      ) : null}
                    </div>
                  </TableCell>
                  <TableCell>
                    <StatusBadge kind="video" status={video.status} />
                  </TableCell>
                  <TableCell>
                    <ProgressCell video={video} />
                  </TableCell>
                  <TableCell className="font-mono text-xs whitespace-nowrap text-muted-foreground tabular">
                    {video.plan?.start !== undefined ? `${formatTime(video.plan.start)}〜` : "—"}
                    {video.plan?.duration ? <span className="block font-sans text-2xs text-subtle-foreground">{formatDuration(video.plan.duration)}</span> : null}
                  </TableCell>
                  <TableCell className="text-right">
                    <div className="flex justify-end gap-1.5 whitespace-nowrap">
                      {canRetry ? (
                        <Button
                          size="xs"
                          variant="secondary"
                          onClick={() => void actions.retry(video)}
                          loading={busy === "retry"}
                          disabled={disabled || !!busy}
                        >
                          {busy === "retry" ? null : <RotateCw aria-hidden />}
                          再試行
                        </Button>
                      ) : null}
                      {video.status === "done" ? (
                        <Button size="xs" variant="ghost" asChild>
                          <Link href={editorHref(video.id)}>
                            <Pencil aria-hidden />
                            編集
                          </Link>
                        </Button>
                      ) : null}
                    </div>
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      )}
    </Card>
  );
}
