"use client";

import * as React from "react";

import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";

export interface RegenerateDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  onConfirm: (newClips: boolean) => Promise<void>;
}

/** Confirmation for `POST /api/videos/{id}/regenerate` with the "new clips" option. */
export function RegenerateDialog({ open, onOpenChange, title, onConfirm }: RegenerateDialogProps) {
  const [newClips, setNewClips] = React.useState(false);

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={(next) => {
        onOpenChange(next);
        if (!next) setNewClips(false);
      }}
      title={`「${title}」を再生成しますか？`}
      description="開始位置・歌詞スタイル・手動で調整した歌詞タイミングと文字設定はそのままに、ショットの組み合わせ・順番・切り出し位置を変えた別バリエーションを作り、この動画を置き換えます（色味は自動設定の場合のみ変わります）。新しい動画が完成するまでは今の動画を表示します。"
      confirmLabel="再生成する"
      onConfirm={() => onConfirm(newClips)}
    >
      <div className="flex items-start justify-between gap-4 rounded-md border border-border bg-surface-raised/50 px-3 py-2.5">
        <div className="flex flex-col gap-0.5">
          <Label htmlFor="regenerate-new-clips">新しい素材も生成する</Label>
          <p className="text-xs leading-5 text-muted-foreground">
            オフの場合は生成済みの素材だけで組み直します（費用なし）。オンにすると新しい素材を生成するため、外部 API のプロバイダーでは費用が発生し、生成ジョブの予算上限を超える場合は開始できません。
          </p>
        </div>
        <Switch id="regenerate-new-clips" checked={newClips} onCheckedChange={setNewClips} />
      </div>
    </ConfirmDialog>
  );
}
