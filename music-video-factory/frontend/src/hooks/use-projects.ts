"use client";

import useSWR from "swr";

import { apiPaths, swrFetcher, type ApiError } from "@/lib/api";
import { isAnalysisActive } from "@/lib/status";
import type { ProjectOut, ProjectSummary } from "@/lib/types";

import { POLL_NORMAL_MS } from "./polling";

/** `GET /api/projects`, polled while any project's analysis is running. */
export function useProjects() {
  return useSWR<ProjectSummary[], ApiError>(apiPaths.projects, swrFetcher, {
    refreshInterval: (data) => (data?.some((p) => isAnalysisActive(p.analysis_status)) ? POLL_NORMAL_MS : 0),
  });
}

/** `GET /api/projects/{id}`, polled while the audio analysis is queued/running. */
export function useProject(projectId: string | null | undefined) {
  return useSWR<ProjectOut, ApiError>(projectId ? apiPaths.project(projectId) : null, swrFetcher, {
    refreshInterval: (data) => (data && isAnalysisActive(data.analysis_status) ? POLL_NORMAL_MS : 0),
  });
}
