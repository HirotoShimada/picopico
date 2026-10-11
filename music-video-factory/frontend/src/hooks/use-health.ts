"use client";

import useSWR from "swr";

import { apiPaths, swrFetcher, type ApiError } from "@/lib/api";
import type { HealthOut } from "@/lib/types";

/** `GET /api/health`, re-checked every 15 s (faster while unreachable). */
export function useHealth() {
  return useSWR<HealthOut, ApiError>(apiPaths.health, swrFetcher, {
    refreshInterval: 15_000,
    revalidateOnFocus: true,
    shouldRetryOnError: false,
    errorRetryCount: 0,
  });
}
