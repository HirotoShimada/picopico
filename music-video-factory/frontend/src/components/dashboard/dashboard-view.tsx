"use client";

import * as React from "react";

import { ErrorState } from "@/components/ui/error-state";
import { useDashboard } from "@/hooks/use-dashboard";
import { useProjects } from "@/hooks/use-projects";
import { isApiError, isBackendUnreachable } from "@/lib/api";

import { ActiveJobs } from "./active-jobs";
import { KpiRow } from "./kpi-row";
import { ProjectTable } from "./project-table";
import { RecentVideos } from "./recent-videos";
import { UsagePanel } from "./usage-panel";

export function DashboardView() {
  const dashboard = useDashboard();
  const projects = useProjects();
  const [retrying, setRetrying] = React.useState(false);

  const retryAll = React.useCallback(async () => {
    setRetrying(true);
    try {
      await Promise.all([dashboard.mutate(), projects.mutate()]);
    } finally {
      setRetrying(false);
    }
  }, [dashboard, projects]);

  // Unreachable backend / missing token affects every card: show one clear message instead.
  const firstError = dashboard.error ?? projects.error;
  const globalFailure = isBackendUnreachable(firstError) || (isApiError(firstError) && firstError.status === 401);
  if (!dashboard.data && !projects.data && globalFailure) {
    return <ErrorState error={dashboard.error ?? projects.error} onRetry={() => void retryAll()} retrying={retrying} />;
  }

  const dashboardLoading = dashboard.isLoading && !dashboard.data;

  return (
    <div className="flex flex-col gap-6">
      {dashboard.error && !dashboard.data ? (
        <ErrorState error={dashboard.error} title="集計データを読み込めませんでした" size="sm" onRetry={() => void dashboard.mutate()} />
      ) : (
        <KpiRow data={dashboard.data} loading={dashboardLoading} />
      )}

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-3">
        <div className="min-w-0 xl:col-span-2">
          <ProjectTable
            projects={projects.data}
            error={projects.error}
            loading={projects.isLoading && !projects.data}
            onRetry={() => void projects.mutate()}
          />
        </div>
        <div className="flex min-w-0 flex-col gap-6">
          {dashboard.data || dashboardLoading ? (
            <>
              <ActiveJobs jobs={dashboard.data?.active_jobs} loading={dashboardLoading} />
              <UsagePanel usage={dashboard.data?.usage} loading={dashboardLoading} />
            </>
          ) : null}
        </div>
      </div>

      {dashboard.data || dashboardLoading ? <RecentVideos videos={dashboard.data?.recent_videos} loading={dashboardLoading} /> : null}
    </div>
  );
}
