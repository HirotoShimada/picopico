"use client";

import useSWR from "swr";

import { apiPaths, swrFetcher, type ApiError } from "@/lib/api";
import type { LyricsOut } from "@/lib/types";

/** `GET /api/projects/{id}/lyrics`. Not revalidated on focus so local edits are not clobbered. */
export function useLyrics(projectId: string | null | undefined) {
  return useSWR<LyricsOut, ApiError>(projectId ? apiPaths.lyrics(projectId) : null, swrFetcher, {
    revalidateOnFocus: false,
  });
}
