"use client";

import { Check, Film } from "lucide-react";
import * as React from "react";

import { clipSourceLabel } from "@/components/materials/clip-labels";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { isRunway, PoweredByRunway } from "@/components/ui/powered-by-runway";
import { Thumbnail } from "@/components/ui/thumbnail";
import { formatDuration } from "@/lib/format";
import type { ClipOut, ShotPlan } from "@/lib/types";
import { cn } from "@/lib/utils";

/** Source seconds a shot consumes (`src_in + duration × speed`). */
export function shotSourceSeconds(shot: ShotPlan): number {
  return shot.src_in + shot.duration * (shot.speed || 1);
}

export interface ClipPickerDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  shotIndex: number | null;
  shot: ShotPlan | null;
  clips: readonly ClipOut[];
  onPick: (clipId: string) => void;
}

/** Choose a replacement clip for one shot from the video's `available_clips`. */
export function ClipPickerDialog({ open, onOpenChange, shotIndex, shot, clips, onPick }: ClipPickerDialogProps) {
  const [candidateId, setCandidateId] = React.useState<string | null>(null);
  // Start from the shot's current clip each time the dialog opens for a shot.
  const openKey = open ? shotIndex : null;
  const [lastOpenKey, setLastOpenKey] = React.useState<number | null>(null);
  if (openKey !== lastOpenKey) {
    setLastOpenKey(openKey);
    setCandidateId(shot?.clip_id ?? null);
  }

  const ready = React.useMemo(() => {
    const usable = clips.filter((clip) => clip.status === "ready");
    if (!shot?.scene_id) return usable;
    return [...usable].sort((a, b) => Number(b.scene_id === shot.scene_id) - Number(a.scene_id === shot.scene_id));
  }, [clips, shot]);
  const candidate = ready.find((clip) => clip.id === candidateId) ?? null;
  const needed = shot ? shotSourceSeconds({ ...shot, src_in: 0 }) : 0;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="xl" className="max-w-5xl">
        <DialogHeader>
          <DialogTitle>ショット {shotIndex !== null ? shotIndex + 1 : ""} の素材を差し替え</DialogTitle>
          <DialogDescription>
            {shot
              ? `ショットの長さは ${formatDuration(shot.duration)}（素材から ${formatDuration(needed)} 分を使用）です。`
              : null}
            素材が短い場合はスロー再生またはループで補われます。
          </DialogDescription>
        </DialogHeader>

        {ready.length === 0 ? (
          <EmptyState size="sm" icon={Film} title="差し替えに使える素材がありません" description="素材タブで動画をアップロードするか、生成済みの素材が揃うまでお待ちください。" />
        ) : (
          <div className="grid grid-cols-1 gap-5 md:grid-cols-[minmax(0,1fr)_240px]">
            <ul className="grid max-h-[60vh] grid-cols-3 gap-3 overflow-y-auto pr-1 sm:grid-cols-4 lg:grid-cols-5" aria-label="素材">
              {ready.map((clip) => {
                const selected = clip.id === candidateId;
                const current = clip.id === shot?.clip_id;
                const short = clip.duration !== null && clip.duration < needed;
                return (
                  <li key={clip.id} className="min-w-0">
                    <button
                      type="button"
                      onClick={() => setCandidateId(clip.id)}
                      aria-pressed={selected}
                      className="focus-ring-custom flex w-full cursor-pointer flex-col gap-1 rounded-md text-left outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
                    >
                      <Thumbnail
                        src={clip.thumbnail_url}
                        alt={`素材 ${clip.scene_id ?? clip.id}`}
                        className={cn(selected && "border-zinc-200 ring-2 ring-zinc-200")}
                        fallback={<Film className="size-4" aria-hidden />}
                        overlay={
                          <>
                            {current ? (
                              <span className="absolute top-1 left-1">
                                <Badge tone="outline" className="bg-black/70">
                                  使用中
                                </Badge>
                              </span>
                            ) : null}
                            {selected ? (
                              <span className="absolute top-1 right-1 flex size-5 items-center justify-center rounded-full bg-zinc-200 text-zinc-900">
                                <Check className="size-3" strokeWidth={3} aria-hidden />
                              </span>
                            ) : null}
                          </>
                        }
                      />
                      <span className="truncate text-2xs text-muted-foreground">
                        {clip.scene_id ?? clipSourceLabel(clip)} ・ {formatDuration(clip.duration)}
                      </span>
                      {short ? <span className="text-2xs text-warning-foreground">短い素材</span> : null}
                      {shot?.scene_id && clip.scene_id === shot.scene_id && !current ? (
                        <span className="text-2xs text-subtle-foreground">同じシーン</span>
                      ) : null}
                    </button>
                  </li>
                );
              })}
            </ul>
            <div className="flex flex-col gap-2">
              <span className="text-xs text-muted-foreground">プレビュー</span>
              {candidate?.url ? (
                <video
                  key={candidate.id}
                  src={candidate.url}
                  poster={candidate.thumbnail_url ?? undefined}
                  className="aspect-[9/16] w-full rounded-md border border-border bg-black object-contain"
                  autoPlay
                  muted
                  loop
                  playsInline
                  controls
                  aria-label="素材のプレビュー（無音）"
                />
              ) : (
                <div className="flex aspect-[9/16] w-full items-center justify-center rounded-md border border-dashed border-border text-xs text-subtle-foreground">
                  素材を選択してください
                </div>
              )}
              {candidate ? (
                <div className="flex flex-col gap-0.5 text-2xs leading-4 text-muted-foreground">
                  <span>
                    {clipSourceLabel(candidate)} ・ {formatDuration(candidate.duration)}
                    {candidate.width && candidate.height ? ` ・ ${candidate.width}×${candidate.height}` : ""}
                  </span>
                  {candidate.prompt ? (
                    <span lang="en" className="line-clamp-4 font-mono text-subtle-foreground">
                      {candidate.prompt}
                    </span>
                  ) : null}
                  {isRunway(candidate.source) ? <PoweredByRunway className="mt-1" /> : null}
                </div>
              ) : null}
            </div>
          </div>
        )}

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            キャンセル
          </Button>
          <Button
            variant="primary"
            disabled={!candidateId || candidateId === shot?.clip_id}
            onClick={() => {
              if (candidateId) onPick(candidateId);
              onOpenChange(false);
            }}
          >
            この素材に差し替える
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
