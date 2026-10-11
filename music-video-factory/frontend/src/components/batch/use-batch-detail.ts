"use client";

import useSWR from "swr";

import { POLL_FAST_MS } from "@/hooks/polling";
import { apiPaths, swrFetcher, type ApiError } from "@/lib/api";
import { isBatchActive, isClipActive, isVideoActive } from "@/lib/status";
import type { BatchDetailOut } from "@/lib/types";

/** True while the batch or any of its videos/clips still has work in flight (e.g. a retried video). */
export function batchHasActiveWork(batch: BatchDetailOut | undefined): boolean {
  if (!batch) return false;
  return (
    isBatchActive(batch.status) ||
    (batch.videos ?? []).some((video) => isVideoActive(video.status)) ||
    (batch.clips ?? []).some((clip) => isClipActive(clip.status))
  );
}

/**
 * `GET /api/batches/{id}` polled every 2 s while anything is in flight. Same SWR
 * key as `useBatch`, but it also keeps polling after a single video is retried
 * in an already finished batch.
 */
export function useBatchDetail(batchId: string) {
  return useSWR<BatchDetailOut, ApiError>(apiPaths.batch(batchId), swrFetcher, {
    refreshInterval: (data) => (batchHasActiveWork(data) ? POLL_FAST_MS : 0),
  });
}
