"use client";

import useSWR from "swr";

import { apiPaths, swrFetcher, type ApiError } from "@/lib/api";
import { isClipActive } from "@/lib/status";
import type { ClipOut } from "@/lib/types";

import { POLL_NORMAL_MS } from "./polling";

/** `GET /api/projects/{id}/clips`, polled while any clip is pending/generating. */
export function useClips(projectId: string | null | undefined) {
  return useSWR<ClipOut[], ApiError>(projectId ? apiPaths.clips(projectId) : null, swrFetcher, {
    refreshInterval: (data) => (data?.some((clip) => isClipActive(clip.status)) ? POLL_NORMAL_MS : 0),
  });
}
