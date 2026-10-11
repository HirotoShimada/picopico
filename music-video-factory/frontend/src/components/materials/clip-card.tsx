"use client";

import { Film, LoaderCircle, Trash2 } from "lucide-react";
import * as React from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { isRunway, PoweredByRunway } from "@/components/ui/powered-by-runway";
import { StatusBadge } from "@/components/ui/status-badge";
import { SimpleTooltip } from "@/components/ui/tooltip";
import { formatDuration, formatResolution } from "@/lib/format";
import { isClipActive } from "@/lib/status";
import type { ClipOut } from "@/lib/types";

import { clipSourceLabel, isVertical } from "./clip-labels";

export interface ClipCardProps {
  clip: ClipOut;
  onOpen: (clip: ClipOut) => void;
  onDelete: (clip: ClipOut) => void;
}

/** 9:16 tile: thumbnail (muted video preview on hover), duration, source and status. */
export function ClipCard({ clip, onOpen, onDelete }: ClipCardProps) {
  const [hovering, setHovering] = React.useState(false);
  const ready = clip.status === "ready";
  const playable = ready && !!clip.url;
  const vertical = isVertical(clip);
  const busy = isClipActive(clip.status);
  const title = clip.duration !== null ? `${clipSourceLabel(clip)}の素材（${formatDuration(clip.duration)}）` : `${clipSourceLabel(clip)}の素材`;

  return (
    <div className="group flex min-w-0 flex-col gap-2">
      <button
        type="button"
        onClick={() => onOpen(clip)}
        onMouseEnter={() => setHovering(true)}
        onMouseLeave={() => setHovering(false)}
        onFocus={() => setHovering(true)}
        onBlur={() => setHovering(false)}
        aria-label={`${title}をプレビュー`}
        className="focus-ring-custom relative aspect-[9/16] w-full cursor-pointer overflow-hidden rounded-md border border-border bg-surface-raised outline-none transition-colors hover:border-border-strong focus-visible:ring-2 focus-visible:ring-ring/50"
      >
        {clip.thumbnail_url ? (
          // eslint-disable-next-line @next/next/no-img-element -- signed, short-lived backend URL
          <img src={clip.thumbnail_url} alt="" loading="lazy" decoding="async" className="absolute inset-0 size-full object-cover" />
        ) : (
          <span className="absolute inset-0 flex items-center justify-center text-subtle-foreground">
            {busy ? <LoaderCircle className="size-5 animate-spin" aria-hidden /> : <Film className="size-5" aria-hidden />}
          </span>
        )}
        {playable && hovering ? (
          <video
            src={clip.url ?? undefined}
            muted
            autoPlay
            loop
            playsInline
            preload="metadata"
            className="absolute inset-0 size-full object-cover"
            aria-hidden
          />
        ) : null}

        <span className="absolute top-1.5 left-1.5 flex flex-col items-start gap-1">
          {!ready ? <StatusBadge kind="clip" status={clip.status} /> : null}
          {vertical === false ? <Badge tone="neutral">横長</Badge> : null}
        </span>
        {clip.duration !== null ? (
          <span className="absolute right-1.5 bottom-1.5 rounded bg-black/70 px-1.5 py-0.5 font-mono text-2xs text-white tabular">
            {formatDuration(clip.duration)}
          </span>
        ) : null}
        {clip.used_in_videos > 0 ? (
          <span className="absolute bottom-1.5 left-1.5 rounded bg-black/70 px-1.5 py-0.5 text-2xs text-white">
            {clip.used_in_videos}本で使用
          </span>
        ) : null}
      </button>

      <div className="flex items-start justify-between gap-1">
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="truncate text-xs font-medium text-foreground">{clipSourceLabel(clip)}</span>
          <span className="truncate text-2xs text-subtle-foreground tabular">
            {[clip.width && clip.height ? formatResolution(clip.width, clip.height) : null, clip.model].filter(Boolean).join(" ・ ") || " "}
          </span>
          {isRunway(clip.source) ? <PoweredByRunway className="mt-0.5" /> : null}
          {clip.status === "failed" && clip.error ? (
            <span className="line-clamp-2 text-2xs text-danger-foreground" title={clip.error}>
              {clip.error}
            </span>
          ) : null}
        </div>
        <SimpleTooltip content={busy ? "生成中の素材は削除できません" : "削除"}>
          <span className="inline-flex">
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              onClick={() => onDelete(clip)}
              disabled={busy}
              aria-label={`${title}を削除`}
              className="opacity-70 group-hover:opacity-100 focus-visible:opacity-100"
            >
              <Trash2 aria-hidden />
            </Button>
          </span>
        </SimpleTooltip>
      </div>
    </div>
  );
}
