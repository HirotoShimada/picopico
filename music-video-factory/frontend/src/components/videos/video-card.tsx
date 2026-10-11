"use client";

import { Film, Play, Star } from "lucide-react";
import * as React from "react";

import { Checkbox } from "@/components/ui/checkbox";
import { Progress } from "@/components/ui/progress";
import { StatusBadge } from "@/components/ui/status-badge";
import { Thumbnail } from "@/components/ui/thumbnail";
import { formatTime } from "@/lib/format";
import type { VideoOut } from "@/lib/types";
import { cn } from "@/lib/utils";

import { QualityBadge } from "./quality-summary";
import { videoTitle } from "./video-actions";

export interface VideoCardProps {
  video: VideoOut;
  selected: boolean;
  /** Show the checkbox even without hover (some video is selected). */
  selecting: boolean;
  onSelectedChange: (selected: boolean) => void;
  onOpen: () => void;
  onToggleFavorite: () => void;
  favoritePending: boolean;
}

/** Large 9:16 gallery tile: thumbnail with status/progress, selection checkbox and favourite toggle. */
export function VideoCard({ video, selected, selecting, onSelectedChange, onOpen, onToggleFavorite, favoritePending }: VideoCardProps) {
  const title = videoTitle(video);
  const rendering = video.status === "rendering";
  const selectable = video.status === "done";
  const duration = video.duration ?? video.plan?.duration ?? null;

  return (
    <li className="flex min-w-0 flex-col gap-2">
      <div className="group relative">
        <button
          type="button"
          onClick={onOpen}
          className="focus-ring-custom block w-full cursor-pointer rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
          aria-label={`${title} をプレビュー`}
        >
          <Thumbnail
            src={video.thumbnail_url}
            alt={`${title} のサムネイル`}
            className={cn("transition-colors group-hover:border-border-strong", selected && "border-zinc-300")}
            fallback={<Film className="size-8" aria-hidden />}
            overlay={
              <>
                {video.status !== "done" ? (
                  <div className="absolute bottom-2 left-2">
                    <StatusBadge kind="video" status={video.status} />
                  </div>
                ) : (
                  <span className="pointer-events-none absolute inset-0 flex items-center justify-center opacity-0 transition-opacity group-hover:opacity-100">
                    <span className="flex size-12 items-center justify-center rounded-full bg-black/60 text-white">
                      <Play className="size-5 translate-x-px fill-current" aria-hidden />
                    </span>
                  </span>
                )}
                {duration && video.status === "done" ? (
                  <span className="absolute right-2 bottom-2 rounded bg-black/70 px-1.5 py-px font-mono text-2xs text-white">
                    {formatTime(duration)}
                  </span>
                ) : null}
                {rendering ? (
                  <div className="absolute inset-x-2 bottom-9">
                    <Progress value={video.progress * 100} size="sm" aria-label={`${title} のレンダリング進捗`} />
                  </div>
                ) : null}
                {selected ? <span className="pointer-events-none absolute inset-0 rounded-md ring-2 ring-zinc-200 ring-inset" aria-hidden /> : null}
              </>
            }
          />
        </button>

        {selectable ? (
          <div
            className={cn(
              "absolute top-2 left-2 flex size-7 items-center justify-center rounded bg-black/60 transition-opacity",
              selecting || selected ? "opacity-100" : "opacity-0 group-hover:opacity-100 focus-within:opacity-100",
            )}
          >
            <Checkbox
              checked={selected}
              onCheckedChange={(checked) => onSelectedChange(checked === true)}
              aria-label={`${title} を選択`}
            />
          </div>
        ) : null}

        <button
          type="button"
          onClick={onToggleFavorite}
          disabled={favoritePending}
          aria-pressed={video.favorite}
          aria-label={video.favorite ? `${title} をお気に入りから外す` : `${title} をお気に入りに追加`}
          title={video.favorite ? "お気に入りから外す" : "お気に入りに追加"}
          className={cn(
            "focus-ring-custom absolute top-2 right-2 flex size-8 cursor-pointer items-center justify-center rounded-full bg-black/60 text-white outline-none transition-opacity focus-visible:ring-2 focus-visible:ring-ring/50 disabled:cursor-wait",
            video.favorite ? "opacity-100" : "opacity-0 group-hover:opacity-100 focus-visible:opacity-100",
          )}
        >
          <Star className={cn("size-4", video.favorite && "fill-current")} aria-hidden />
        </button>
      </div>

      <div className="flex min-w-0 items-start justify-between gap-2">
        <div className="flex min-w-0 flex-col">
          <span className="truncate text-sm text-foreground">{title}</span>
          <span className="text-2xs text-subtle-foreground tabular">
            #{video.index + 1}
            {video.plan?.start !== undefined ? ` ・ ${formatTime(video.plan.start)}〜` : ""}
            {video.render_version > 1 ? ` ・ v${video.render_version}` : ""}
            {video.plan?.substitutions ? ` ・ 代替素材 ${video.plan.substitutions}` : ""}
          </span>
          {video.status === "done" && video.error ? (
            <span className="line-clamp-2 text-2xs leading-4 text-warning-foreground" title={video.error}>
              {video.error}
            </span>
          ) : null}
        </div>
        {video.status === "done" ? <QualityBadge report={video.quality} /> : null}
      </div>
    </li>
  );
}
