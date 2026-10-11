"use client";

import { Film, RefreshCw, TriangleAlert } from "lucide-react";
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { StatusBadge } from "@/components/ui/status-badge";
import { formatPercent } from "@/lib/format";
import { isVideoActive } from "@/lib/status";
import type { VideoOut } from "@/lib/types";
import { cn } from "@/lib/utils";

import { videoTitle } from "./video-actions";

export interface VideoPlayerProps {
  video: Pick<VideoOut, "id" | "title" | "index" | "status" | "progress" | "url" | "thumbnail_url" | "render_version" | "error">;
  /** Ask for fresh signed URLs (they expire after `MEDIA_URL_TTL`). */
  onReload?: () => void;
  onTimeUpdate?: (seconds: number) => void;
  videoRef?: React.Ref<HTMLVideoElement>;
  className?: string;
}

/**
 * 9:16 player for a rendered video (signed URL). While a (re-)render is in
 * flight it shows the status and progress over the thumbnail.
 */
export function VideoPlayer({ video, onReload, onTimeUpdate, videoRef, className }: VideoPlayerProps) {
  const [failedSrc, setFailedSrc] = React.useState<string | null>(null);
  const rendering = isVideoActive(video.status);
  const playable = !!video.url && !rendering;
  const loadFailed = playable && failedSrc === video.url;

  return (
    <div className={cn("relative aspect-[9/16] w-full overflow-hidden rounded-lg border border-border bg-black", className)}>
      {playable && !loadFailed ? (
        <video
          key={`${video.id}:${video.render_version}`}
          ref={videoRef}
          src={video.url ?? undefined}
          poster={video.thumbnail_url ?? undefined}
          controls
          playsInline
          preload="metadata"
          className="size-full object-contain"
          onError={() => setFailedSrc(video.url)}
          onTimeUpdate={onTimeUpdate ? (event) => onTimeUpdate(event.currentTarget.currentTime) : undefined}
          aria-label={`${videoTitle(video)} の再生`}
        />
      ) : (
        <>
          {video.thumbnail_url ? (
            // eslint-disable-next-line @next/next/no-img-element -- signed, short-lived backend URL
            <img src={video.thumbnail_url} alt="" className="absolute inset-0 size-full object-cover opacity-40" />
          ) : null}
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 p-6 text-center">
            {loadFailed ? (
              <>
                <TriangleAlert className="size-6 text-warning" aria-hidden />
                <p className="text-sm text-foreground">動画を読み込めませんでした</p>
                <p className="text-xs leading-5 text-muted-foreground">再生用 URL の有効期限が切れた可能性があります。</p>
                {onReload ? (
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => {
                      setFailedSrc(null);
                      onReload();
                    }}
                  >
                    <RefreshCw aria-hidden />
                    再読み込み
                  </Button>
                ) : null}
              </>
            ) : rendering ? (
              <>
                <StatusBadge kind="video" status={video.status} />
                {video.status === "rendering" ? (
                  <div className="flex w-3/4 flex-col gap-1.5">
                    <Progress value={video.progress * 100} aria-label="レンダリング進捗" />
                    <span className="font-mono text-xs text-muted-foreground tabular">{formatPercent(video.progress)}</span>
                  </div>
                ) : (
                  <p className="text-xs text-muted-foreground">順番が来るとレンダリングを開始します。</p>
                )}
              </>
            ) : (
              <>
                <Film className="size-6 text-subtle-foreground" aria-hidden />
                <StatusBadge kind="video" status={video.status} />
                {video.error ? <p className="line-clamp-4 text-xs leading-5 text-danger-foreground">{video.error}</p> : null}
              </>
            )}
          </div>
        </>
      )}
    </div>
  );
}
