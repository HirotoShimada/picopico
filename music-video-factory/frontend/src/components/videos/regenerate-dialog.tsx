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
      description="同じ設定のまま、ショットの順番・切り出し位置・開始位置・色味を変えた別バリエーションを作り、この動画を置き換えます。"
      confirmLabel="再生成する"
      onConfirm={() => onConfirm(newClips)}
    >
      <div className="flex items-start justify-between gap-4 rounded-md border border-border bg-surface-raised/50 px-3 py-2.5">
        <div className="flex flex-col gap-0.5">
          <Label htmlFor="regenerate-new-clips">新しい素材も生成する</Label>
          <p className="text-xs leading-5 text-muted-foreground">
            オフの場合は生成済みの素材だけで組み直します。オンにすると新規生成を行うため、外部 API のプロバイダーでは費用が発生します。
          </p>
        </div>
        <Switch id="regenerate-new-clips" checked={newClips} onCheckedChange={setNewClips} />
      </div>
    </ConfirmDialog>
  );
}
