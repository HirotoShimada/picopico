"use client";

import useSWR from "swr";

import { apiPaths, swrFetcher, type ApiError } from "@/lib/api";
import { isVideoActive } from "@/lib/status";
import type { VideoDetailOut, VideoListFilters, VideoOut } from "@/lib/types";

import { POLL_FAST_MS, POLL_NORMAL_MS } from "./polling";

/** `GET /api/projects/{id}/videos`, polled while any listed video is still rendering. */
export function useVideos(projectId: string | null | undefined, filters: VideoListFilters = {}) {
  return useSWR<VideoOut[], ApiError>(projectId ? apiPaths.projectVideos(projectId, filters) : null, swrFetcher, {
    keepPreviousData: true,
    refreshInterval: (data) => (data?.some((video) => isVideoActive(video.status)) ? POLL_NORMAL_MS : 0),
  });
}

/** `GET /api/videos/{id}` (with effective lyric lines and swappable clips), polled while rendering. */
export function useVideo(videoId: string | null | undefined) {
  return useSWR<VideoDetailOut, ApiError>(videoId ? apiPaths.video(videoId) : null, swrFetcher, {
    refreshInterval: (data) => (data && isVideoActive(data.status) ? POLL_FAST_MS : 0),
  });
}
