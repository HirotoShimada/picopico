"use client";

import { RefreshCw } from "lucide-react";
import * as React from "react";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useHealth } from "@/hooks/use-health";
import { API_BASE_URL, isBackendUnreachable } from "@/lib/api";
import { cn } from "@/lib/utils";

type Indicator = { label: string; dot: string; text: string };

/** Backend connectivity pill for the top bar (polls `GET /api/health`). */
export function BackendStatus() {
  const { data, error, isLoading, isValidating, mutate } = useHealth();

  let indicator: Indicator;
  if (error) {
    indicator = isBackendUnreachable(error)
      ? { label: "バックエンド未接続", dot: "bg-danger", text: "text-danger-foreground" }
      : { label: "API エラー", dot: "bg-danger", text: "text-danger-foreground" };
  } else if (!data || isLoading) {
    indicator = { label: "接続確認中", dot: "bg-zinc-500", text: "text-muted-foreground" };
  } else if (data.status === "ok") {
    indicator = { label: "API 接続中", dot: "bg-success", text: "text-muted-foreground" };
  } else {
    indicator = { label: "一部機能が停止中", dot: "bg-warning", text: "text-warning-foreground" };
  }

  const checks = data
    ? [
        { name: "データベース", ok: data.db },
        { name: "FFmpeg", ok: data.ffmpeg },
        { name: data.task_mode === "celery" ? "Redis（必須）" : "Redis", ok: data.redis },
      ]
    : [];

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          onClick={() => void mutate()}
          className={cn(
            "focus-ring-custom inline-flex h-7 cursor-pointer items-center gap-2 rounded-full border border-border bg-surface px-2.5 text-xs outline-none transition-colors hover:border-border-strong",
            "focus-visible:ring-2 focus-visible:ring-ring/40",
            indicator.text,
          )}
          aria-label={`${indicator.label}（クリックで再確認）`}
        >
          {isValidating && !isLoading ? (
            <RefreshCw className="size-3 animate-spin text-subtle-foreground" aria-hidden />
          ) : (
            <span className={cn("size-2 rounded-full", indicator.dot)} aria-hidden />
          )}
          {indicator.label}
        </button>
      </TooltipTrigger>
      <TooltipContent align="end" className="max-w-sm">
        <div className="flex flex-col gap-1.5 py-0.5">
          <p className="font-mono text-2xs text-muted-foreground">{API_BASE_URL}</p>
          {error ? (
            <p>{error.message}</p>
          ) : data ? (
            <>
              <ul className="flex flex-col gap-0.5">
                {checks.map((check) => (
                  <li key={check.name} className="flex items-center justify-between gap-6">
                    <span>{check.name}</span>
                    <span className={check.ok ? "text-success-foreground" : "text-danger-foreground"}>
                      {check.ok ? "正常" : "停止"}
                    </span>
                  </li>
                ))}
              </ul>
              <p className="text-2xs text-muted-foreground">
                タスク実行: {data.task_mode} ・ ストレージ: {data.storage_backend}
              </p>
            </>
          ) : (
            <p>接続を確認しています…</p>
          )}
          <p className="text-2xs text-subtle-foreground">クリックで再確認</p>
        </div>
      </TooltipContent>
    </Tooltip>
  );
}
