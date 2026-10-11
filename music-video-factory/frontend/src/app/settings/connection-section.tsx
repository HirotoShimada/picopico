"use client";

import { Copy, RefreshCw } from "lucide-react";
import * as React from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/toast";
import { useHealth } from "@/hooks/use-health";
import { API_BASE_URL, isBackendUnreachable } from "@/lib/api";

import { SettingsSection } from "./section";

function CheckRow({ label, ok, detail }: { label: string; ok: boolean | null; detail?: string }) {
  return (
    <div className="flex items-center justify-between gap-4 py-2">
      <dt className="text-sm text-muted-foreground">{label}</dt>
      <dd className="flex items-center gap-2 text-sm">
        {detail ? <span className="font-mono text-xs text-muted-foreground">{detail}</span> : null}
        {ok === null ? null : ok ? <Badge tone="success">正常</Badge> : <Badge tone="danger">停止</Badge>}
      </dd>
    </div>
  );
}

export function ConnectionSection() {
  const { data, error, isLoading, isValidating, mutate } = useHealth();

  async function copyUrl() {
    try {
      await navigator.clipboard.writeText(API_BASE_URL);
      toast.success("URL をコピーしました");
    } catch {
      toast.error("コピーできませんでした");
    }
  }

  let status: React.ReactNode;
  if (error) {
    status = <Badge tone="danger">{isBackendUnreachable(error) ? "未接続" : "エラー"}</Badge>;
  } else if (!data) {
    status = <Badge>確認中</Badge>;
  } else {
    status = data.status === "ok" ? <Badge tone="success">正常</Badge> : <Badge tone="warning">一部停止</Badge>;
  }

  return (
    <SettingsSection
      id="connection"
      title="バックエンド接続"
      description="ブラウザから FastAPI バックエンドへ直接接続します。"
      action={
        <Button variant="secondary" size="sm" onClick={() => void mutate()} loading={isValidating}>
          {isValidating ? null : <RefreshCw aria-hidden />}
          再確認
        </Button>
      }
    >
      <div className="flex flex-col gap-4 px-5 pb-5">
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-border bg-surface-raised px-3 py-2.5">
          <div className="flex min-w-0 flex-col">
            <span className="text-2xs text-muted-foreground">API ベース URL（NEXT_PUBLIC_API_BASE_URL）</span>
            <span className="truncate font-mono text-sm text-foreground">{API_BASE_URL}</span>
          </div>
          <div className="flex items-center gap-2">
            {status}
            <Button variant="ghost" size="icon-sm" onClick={() => void copyUrl()} aria-label="URL をコピー">
              <Copy aria-hidden />
            </Button>
          </div>
        </div>

        {error ? (
          <p className="text-sm leading-6 text-danger-foreground">{error.message}</p>
        ) : isLoading || !data ? (
          <div className="flex flex-col gap-2">
            <Skeleton className="h-6 w-full" />
            <Skeleton className="h-6 w-full" />
            <Skeleton className="h-6 w-full" />
          </div>
        ) : (
          <dl className="divide-y divide-border">
            <CheckRow label="データベース" ok={data.db} />
            <CheckRow label="FFmpeg / ffprobe" ok={data.ffmpeg} />
            <CheckRow
              label={data.task_mode === "celery" ? "Redis（Celery ブローカー・必須）" : "Redis（inline モードでは不要）"}
              ok={data.redis}
            />
            <CheckRow label="タスク実行モード" ok={null} detail={data.task_mode} />
            <CheckRow label="ストレージ" ok={null} detail={data.storage_backend} />
          </dl>
        )}
        <p className="text-xs leading-5 text-subtle-foreground">
          接続先はビルド時の環境変数 <code className="font-mono">NEXT_PUBLIC_API_BASE_URL</code>（既定値 http://localhost:8000）で変更します。
          バックエンドの <code className="font-mono">CORS_ORIGINS</code> にこの画面のオリジンを含めてください。
        </p>
      </div>
    </SettingsSection>
  );
}
