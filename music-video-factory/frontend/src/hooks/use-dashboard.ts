"use client";

import useSWR from "swr";

import { apiPaths, swrFetcher, type ApiError } from "@/lib/api";
import type { DashboardOut } from "@/lib/types";

import { POLL_IDLE_MS, POLL_NORMAL_MS } from "./polling";

/** `GET /api/dashboard`, polled quickly while jobs are running. */
export function useDashboard() {
  return useSWR<DashboardOut, ApiError>(apiPaths.dashboard, swrFetcher, {
    refreshInterval: (data) => (data && data.active_jobs.length > 0 ? POLL_NORMAL_MS : POLL_IDLE_MS),
  });
}
