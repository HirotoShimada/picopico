/** Tabs of `/projects/[id]`, selected with `?tab=` (e.g. `/projects/{id}?tab=rights`). */
export const WORKSPACE_TABS = ["analysis", "lyrics", "materials", "rights"] as const;
export type WorkspaceTab = (typeof WORKSPACE_TABS)[number];

export function parseWorkspaceTab(value: string | null | undefined): WorkspaceTab {
  return (WORKSPACE_TABS as readonly string[]).includes(value ?? "") ? (value as WorkspaceTab) : "analysis";
}

/** Link to a workspace tab, for other pages (e.g. the generate page's "権利確認へ"). */
export function workspaceTabHref(projectId: string, tab: WorkspaceTab): string {
  const base = `/projects/${encodeURIComponent(projectId)}`;
  return tab === "analysis" ? base : `${base}?tab=${tab}`;
}
