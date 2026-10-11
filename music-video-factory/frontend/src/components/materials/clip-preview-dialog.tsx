"use client";

import { Trash2 } from "lucide-react";
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { StatusBadge } from "@/components/ui/status-badge";
import { formatDateTime, formatDuration, formatResolution, formatUsd } from "@/lib/format";
import { isClipActive } from "@/lib/status";
import type { ClipOut } from "@/lib/types";

import { clipSourceLabel, isVertical } from "./clip-labels";

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[6rem_minmax(0,1fr)] gap-3 py-1.5 text-sm">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words text-foreground">{children}</dd>
    </div>
  );
}

export interface ClipPreviewDialogProps {
  clip: ClipOut | null;
  onOpenChange: (open: boolean) => void;
  onDelete: (clip: ClipOut) => void;
}

/** Large 9:16 player with the clip's metadata (prompt for generated clips). */
export function ClipPreviewDialog({ clip, onOpenChange, onDelete }: ClipPreviewDialogProps) {
  const vertical = clip ? isVertical(clip) : null;
  return (
    <Dialog open={clip !== null} onOpenChange={onOpenChange}>
      <DialogContent size="xl">
        {clip ? (
          <>
            <DialogHeader>
              <DialogTitle>{clipSourceLabel(clip)}の素材</DialogTitle>
              <DialogDescription>
                {clip.source === "upload"
                  ? "アップロードした素材です。プロバイダー「アップロード素材」で動画生成に使われます。"
                  : "動画生成で作られたクリップです。同じ条件の生成では再利用され、費用を節約します。"}
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-6 md:grid-cols-[minmax(0,320px)_minmax(0,1fr)]">
              <div className="mx-auto aspect-[9/16] w-full max-w-[320px] overflow-hidden rounded-md border border-border bg-black">
                {clip.url ? (
                  <video
                    key={clip.id}
                    src={clip.url}
                    poster={clip.thumbnail_url ?? undefined}
                    controls
                    autoPlay
                    muted
                    loop
                    playsInline
                    className="size-full object-contain"
                    aria-label="素材のプレビュー"
                  />
                ) : (
                  <div className="flex size-full items-center justify-center p-4 text-center text-sm text-muted-foreground">
                    {isClipActive(clip.status) ? "生成が完了するとプレビューできます。" : "プレビューできる動画がありません。"}
                  </div>
                )}
              </div>
              <dl className="flex min-w-0 flex-col divide-y divide-border">
                <Row label="状態">
                  <StatusBadge kind="clip" status={clip.status} />
                </Row>
                <Row label="長さ">{formatDuration(clip.duration)}</Row>
                <Row label="解像度">
                  {formatResolution(clip.width, clip.height)}
                  {vertical === false ? <span className="ml-2 text-xs text-warning-foreground">横長（9:16 に切り抜いて使用）</span> : null}
                </Row>
                <Row label="提供元">
                  {clipSourceLabel(clip)}
                  {clip.model ? <span className="ml-1 text-muted-foreground">（{clip.model}）</span> : null}
                </Row>
                {clip.source !== "upload" ? <Row label="費用">{formatUsd(clip.cost_usd)}</Row> : null}
                <Row label="使用動画">{clip.used_in_videos > 0 ? `${clip.used_in_videos}本` : "未使用"}</Row>
                {clip.scene_id ? <Row label="シーン">{clip.scene_id}</Row> : null}
                <Row label="追加日時">{formatDateTime(clip.created_at)}</Row>
                {clip.prompt ? (
                  <Row label="プロンプト">
                    <span className="block max-h-40 overflow-y-auto font-mono text-xs leading-5 whitespace-pre-wrap text-muted-foreground">
                      {clip.prompt}
                    </span>
                  </Row>
                ) : null}
                {clip.error ? (
                  <Row label="エラー">
                    <span className="text-danger-foreground">{clip.error}</span>
                  </Row>
                ) : null}
              </dl>
            </div>
            <DialogFooter>
              <Button type="button" variant="destructive-outline" onClick={() => onDelete(clip)} disabled={isClipActive(clip.status)}>
                <Trash2 aria-hidden />
                削除
              </Button>
              <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>
                閉じる
              </Button>
            </DialogFooter>
          </>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
