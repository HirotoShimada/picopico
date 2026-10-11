"use client";

import useSWR from "swr";

import { apiPaths, swrFetcher, type ApiError } from "@/lib/api";
import { isClipActive } from "@/lib/status";
import type { ClipListFilters, ClipOut } from "@/lib/types";

import { POLL_NORMAL_MS } from "./polling";

/**
 * `GET /api/projects/{id}/clips` (`include_cancelled: false` hides cancelled clips), polled while any
 * clip is pending/generating. Revalidate every variant with `mutate(isListKey(apiPaths.clips(id)))`.
 */
export function useClips(projectId: string | null | undefined, filters: ClipListFilters = {}) {
  return useSWR<ClipOut[], ApiError>(projectId ? apiPaths.clips(projectId, filters) : null, swrFetcher, {
    refreshInterval: (data) => (data?.some((clip) => isClipActive(clip.status)) ? POLL_NORMAL_MS : 0),
  });
}
