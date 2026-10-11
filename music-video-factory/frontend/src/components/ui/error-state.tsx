"use client";

import { CircleAlert, KeyRound, RefreshCw, ServerOff } from "lucide-react";
import Link from "next/link";
import * as React from "react";

import { API_BASE_URL, errorMessage, isApiError } from "@/lib/api";
import { cn } from "@/lib/utils";

import { Button } from "./button";

export interface ErrorStateProps extends Omit<React.ComponentProps<"div">, "title"> {
  /** The thrown value (usually an `ApiError` from SWR). */
  error: unknown;
  /** Overrides the derived title. */
  title?: React.ReactNode;
  onRetry?: () => void;
  retrying?: boolean;
  size?: "sm" | "md";
}

/**
 * Error panel with a Japanese explanation. Distinguishes "backend unreachable",
 * authentication errors (link to /settings) and other API errors.
 */
function ErrorState({ error, title, onRetry, retrying = false, size = "md", className, ...props }: ErrorStateProps) {
  const unreachable = isApiError(error) && error.isNetworkError;
  const unauthorized = isApiError(error) && error.status === 401;
  const Icon = unreachable ? ServerOff : unauthorized ? KeyRound : CircleAlert;
  const heading =
    title ?? (unreachable ? "バックエンドに接続できません" : unauthorized ? "認証が必要です" : "データを読み込めませんでした");

  return (
    <div
      data-slot="error-state"
      role="alert"
      className={cn(
        "flex flex-col items-center justify-center rounded-lg border border-danger/30 bg-danger-soft text-center",
        size === "md" ? "gap-3 px-6 py-10" : "gap-2 px-4 py-6",
        className,
      )}
      {...props}
    >
      <div className="flex size-10 items-center justify-center rounded-full border border-danger/40 bg-background">
        <Icon className="size-5 text-danger" aria-hidden />
      </div>
      <div className="flex max-w-lg flex-col gap-1">
        <p className="text-sm font-medium text-foreground">{heading}</p>
        {unreachable ? (
          <p className="text-sm leading-6 text-muted-foreground">
            <span className="font-mono text-xs text-foreground">{API_BASE_URL}</span> に接続できませんでした。
            バックエンド（FastAPI）が起動しているか、<span className="font-mono text-xs">NEXT_PUBLIC_API_BASE_URL</span>{" "}
            と CORS 設定が正しいか確認してください。
          </p>
        ) : (
          <p className="text-sm leading-6 text-muted-foreground">
            {errorMessage(error)}
            {isApiError(error) && error.status > 0 ? (
              <span className="ml-1 font-mono text-xs text-subtle-foreground">(HTTP {error.status})</span>
            ) : null}
          </p>
        )}
      </div>
      <div className="mt-1 flex flex-wrap items-center justify-center gap-2">
        {onRetry ? (
          <Button variant="secondary" size="sm" onClick={onRetry} loading={retrying}>
            {retrying ? null : <RefreshCw aria-hidden />}
            再試行
          </Button>
        ) : null}
        {unauthorized ? (
          <Button variant="outline" size="sm" asChild>
            <Link href="/settings">API トークンを設定</Link>
          </Button>
        ) : null}
      </div>
    </div>
  );
}

export { ErrorState };
