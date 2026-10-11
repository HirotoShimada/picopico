"use client";

import useSWR from "swr";

import { apiPaths, swrFetcher, type ApiError } from "@/lib/api";
import type { WorldEstimate } from "@/lib/types";

/**
 * `GET /api/projects/{id}/world-estimate` (感情・世界観推定). The estimate depends on the
 * analysis, the lyrics and the project's mood/purpose: pass something that changes with them
 * as `revision` (e.g. the project's `updated_at` + analysis status). It only becomes part of the
 * SWR key; the server ignores the extra query parameter.
 */
export function useWorldEstimate(projectId: string | null | undefined, revision: string) {
  const key = projectId ? `${apiPaths.worldEstimate(projectId)}?rev=${encodeURIComponent(revision)}` : null;
  return useSWR<WorldEstimate, ApiError>(key, swrFetcher, { revalidateOnFocus: false, keepPreviousData: true });
}
