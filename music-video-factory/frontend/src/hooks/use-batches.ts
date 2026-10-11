"use client";

import useSWR from "swr";

import { apiPaths, swrFetcher, type ApiError } from "@/lib/api";
import { isBatchActive } from "@/lib/status";
import type { BatchDetailOut, BatchOut } from "@/lib/types";

import { POLL_FAST_MS, POLL_NORMAL_MS } from "./polling";

/** `GET /api/batches/{id}` (with videos and clips), polled every 2 s while the batch is active. */
export function useBatch(batchId: string | null | undefined) {
  return useSWR<BatchDetailOut, ApiError>(batchId ? apiPaths.batch(batchId) : null, swrFetcher, {
    refreshInterval: (data) => (data && isBatchActive(data.status) ? POLL_FAST_MS : 0),
  });
}

/** `GET /api/projects/{id}/batches`, polled while any batch is active. */
export function useProjectBatches(projectId: string | null | undefined) {
  return useSWR<BatchOut[], ApiError>(projectId ? apiPaths.projectBatches(projectId) : null, swrFetcher, {
    refreshInterval: (data) => (data?.some((batch) => isBatchActive(batch.status)) ? POLL_NORMAL_MS : 0),
  });
}
