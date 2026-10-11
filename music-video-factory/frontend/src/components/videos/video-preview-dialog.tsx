"use client";

import { ChevronLeft, ChevronRight, Download, Pencil, RotateCw, Shuffle, Star } from "lucide-react";
import Link from "next/link";
import * as React from "react";

import { editorHref } from "@/components/generate/links";
import { colorGradeName, COLOR_GRADES_FALLBACK, toLyricStyleId } from "@/components/generate/text-style";
import { LYRIC_STYLES_FALLBACK } from "@/components/lyrics/lyric-styles";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Separator } from "@/components/ui/separator";
import { StatusBadge } from "@/components/ui/status-badge";
import { useCapabilities } from "@/hooks/use-capabilities";
import { formatDateTime, formatDuration, formatTime } from "@/lib/format";
import type { VideoOut } from "@/lib/types";

import { QualitySummary } from "./quality-summary";
import { RegenerateDialog } from "./regenerate-dialog";
import { useVideoActions, videoTitle } from "./video-actions";
import { VideoPlayer } from "./video-player";

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-0.5">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-right text-xs text-foreground tabular">{value}</dd>
    </div>
  );
}

export interface VideoPreviewDialogProps {
  /** Videos in display order (for previous / next). */
  videos: readonly VideoOut[];
  videoId: string | null;
  onVideoIdChange: (videoId: string | null) => void;
  /** Refresh signed URLs after an expiry. */
  onReload: () => void;
}

/** Large 9:16 preview with quality report, download, favourite, regenerate and edit. */
export function VideoPreviewDialog({ videos, videoId, onVideoIdChange, onReload }: VideoPreviewDialogProps) {
  const { data: capabilities } = useCapabilities();
  const actions = useVideoActions();
  const [regenerateOpen, setRegenerateOpen] = React.useState(false);

  const index = videoId ? videos.findIndex((video) => video.id === videoId) : -1;
  const video = index >= 0 ? videos[index] : null;
  const previous = index > 0 ? videos[index - 1] : null;
  const next = index >= 0 && index < videos.length - 1 ? videos[index + 1] : null;

  function handleKeyDown(event: React.KeyboardEvent) {
    const target = event.target as HTMLElement;
    if (target.closest("video, input, textarea, [role=slider], [role=switch]")) return;
    if (event.key === "ArrowLeft" && previous) {
      event.preventDefault();
      onVideoIdChange(previous.id);
    } else if (event.key === "ArrowRight" && next) {
      event.preventDefault();
      onVideoIdChange(next.id);
    }
  }

  const plan = video?.plan;
  const busy = video ? actions.pending[video.id] : undefined;
  const lyricStyle = plan
    ? (capabilities?.lyric_styles ?? LYRIC_STYLES_FALLBACK).find((style) => style.id === toLyricStyleId(plan.lyric_style))?.name_ja ?? plan.lyric_style
    : null;

  return (
    <>
      <Dialog open={video !== null} onOpenChange={(open) => !open && onVideoIdChange(null)}>
        <DialogContent size="xl" className="max-w-5xl gap-5" onKeyDown={handleKeyDown} aria-describedby={undefined}>
          {video ? (
            <>
              <DialogHeader>
                <div className="flex flex-wrap items-center gap-2">
                  <DialogTitle>{videoTitle(video)}</DialogTitle>
                  <StatusBadge kind="video" status={video.status} />
                  {video.render_version > 1 ? <span className="text-xs text-subtle-foreground">v{video.render_version}</span> : null}
                </div>
                <DialogDescription>
                  #{video.index + 1} ・ {videos.length} 本中 {index + 1} 本目
                  {video.rendered_at ? ` ・ ${formatDateTime(video.rendered_at)} にレンダリング` : ""}
                </DialogDescription>
              </DialogHeader>

              <div className="grid grid-cols-1 gap-6 md:grid-cols-[minmax(0,auto)_minmax(0,1fr)]">
                <div className="flex items-start justify-center gap-2">
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    className="mt-[30vh] hidden md:inline-flex"
                    onClick={() => previous && onVideoIdChange(previous.id)}
                    disabled={!previous}
                    aria-label="前の動画"
                  >
                    <ChevronLeft aria-hidden />
                  </Button>
                  <VideoPlayer video={video} onReload={onReload} className="h-[min(70vh,720px)] w-auto" />
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    className="mt-[30vh] hidden md:inline-flex"
                    onClick={() => next && onVideoIdChange(next.id)}
                    disabled={!next}
                    aria-label="次の動画"
                  >
                    <ChevronRight aria-hidden />
                  </Button>
                </div>

                <div className="flex min-w-0 flex-col gap-4">
                  <div className="flex flex-wrap gap-2">
                    <Button
                      variant="primary"
                      onClick={() => void actions.download(video)}
                      disabled={video.status !== "done" || !!busy}
                      loading={busy === "download"}
                    >
                      {busy === "download" ? null : <Download aria-hidden />}
                      ダウンロード
                    </Button>
                    <Button
                      variant="secondary"
                      onClick={() => void actions.setFavorite(video, !video.favorite)}
                      disabled={!!busy}
                      aria-pressed={video.favorite}
                    >
                      <Star className={video.favorite ? "fill-current" : undefined} aria-hidden />
                      {video.favorite ? "お気に入り済み" : "お気に入り"}
                    </Button>
                    {video.status === "done" ? (
                      <Button variant="secondary" asChild>
                        <Link href={editorHref(video.id)}>
                          <Pencil aria-hidden />
                          編集
                        </Link>
                      </Button>
                    ) : null}
                    {video.status === "failed" || video.status === "cancelled" ? (
                      <Button variant="secondary" onClick={() => void actions.retry(video)} loading={busy === "retry"} disabled={!!busy}>
                        {busy === "retry" ? null : <RotateCw aria-hidden />}
                        再試行
                      </Button>
                    ) : null}
                    <Button
                      variant="secondary"
                      onClick={() => setRegenerateOpen(true)}
                      disabled={!!busy || video.status === "rendering" || video.status === "queued" || video.status === "waiting_clips"}
                      loading={busy === "regenerate"}
                    >
                      {busy === "regenerate" ? null : <Shuffle aria-hidden />}
                      再生成
                    </Button>
                  </div>

                  {video.error ? (
                    <Alert tone="danger" title="エラー">
                      {video.error}
                    </Alert>
                  ) : null}

                  {plan && plan.shots ? (
                    <dl className="flex flex-col">
                      <Row label="使用区間" value={`${formatTime(plan.start)}〜${formatTime(plan.start + plan.duration)}（${formatDuration(plan.duration)}）`} />
                      <Row label="歌詞スタイル" value={lyricStyle} />
                      <Row label="カラー" value={colorGradeName(plan.color_grade, capabilities?.color_grades ?? COLOR_GRADES_FALLBACK)} />
                      <Row label="ショット" value={`${plan.shots.length} カット${plan.transition === "crossfade" ? "（クロスフェード）" : ""}`} />
                    </dl>
                  ) : null}

                  <Separator />
                  <div className="flex flex-col gap-2">
                    <h3 className="text-xs font-medium text-foreground">品質チェック</h3>
                    <QualitySummary report={video.quality} />
                  </div>
                </div>
              </div>
            </>
          ) : null}
        </DialogContent>
      </Dialog>

      {video ? (
        <RegenerateDialog
          open={regenerateOpen}
          onOpenChange={setRegenerateOpen}
          title={videoTitle(video)}
          onConfirm={async (newClips) => {
            await actions.regenerate(video, newClips);
          }}
        />
      ) : null}
    </>
  );
}
