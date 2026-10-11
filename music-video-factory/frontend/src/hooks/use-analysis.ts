"use client";

import useSWR from "swr";

import { apiPaths, swrFetcher, type ApiError } from "@/lib/api";
import { isAnalysisActive } from "@/lib/status";
import type { AnalysisOut } from "@/lib/types";

import { POLL_FAST_MS } from "./polling";

/** `GET /api/projects/{id}/analysis`, polled while queued/running. */
export function useAnalysis(projectId: string | null | undefined) {
  return useSWR<AnalysisOut, ApiError>(projectId ? apiPaths.analysis(projectId) : null, swrFetcher, {
    refreshInterval: (data) => (data && isAnalysisActive(data.status) ? POLL_FAST_MS : 0),
  });
}
