/** Tabs of the project page `/projects/[id]` (ARCHITECTURE.md §8). */
export type ProjectTab = "analysis" | "lyrics" | "materials" | "rights";

/** Deep link to a tab of the project page, e.g. `/projects/abc?tab=rights`. */
export function projectTabHref(projectId: string, tab: ProjectTab): string {
  return `/projects/${encodeURIComponent(projectId)}?tab=${tab}`;
}

export function batchHref(batchId: string): string {
  return `/batches/${encodeURIComponent(batchId)}`;
}

export function galleryHref(projectId: string, batchId?: string | null): string {
  const base = `/projects/${encodeURIComponent(projectId)}/videos`;
  return batchId ? `${base}?batch=${encodeURIComponent(batchId)}` : base;
}

export function editorHref(videoId: string): string {
  return `/videos/${encodeURIComponent(videoId)}/edit`;
}

export function generateHref(projectId: string): string {
  return `/projects/${encodeURIComponent(projectId)}/generate`;
}
