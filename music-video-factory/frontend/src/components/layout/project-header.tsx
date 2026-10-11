"use client";

import * as React from "react";

import { Badge } from "@/components/ui/badge";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/ui/status-badge";
import { useProject } from "@/hooks/use-projects";

import { PageHeader } from "./page-header";
import { ProjectNav, type ProjectNavKey } from "./project-nav";

export interface ProjectHeaderProps {
  projectId: string;
  active: ProjectNavKey;
  /** See `ProjectNav.batchId`. */
  batchId?: string;
  /** Page-specific buttons on the right. */
  actions?: React.ReactNode;
}

/**
 * Project title (楽曲名 / アーティスト), status badges and the project sub-nav.
 * Handles its own loading and error (incl. 404) states.
 */
export function ProjectHeader({ projectId, active, batchId, actions }: ProjectHeaderProps) {
  const { data: project, error, isLoading, mutate } = useProject(projectId);

  if (error && !project) {
    return (
      <div className="mb-6">
        <ErrorState
          error={error}
          title={error.isNotFound ? "プロジェクトが見つかりません" : undefined}
          onRetry={error.isNotFound ? undefined : () => void mutate()}
        />
      </div>
    );
  }

  return (
    <>
      {isLoading || !project ? (
        <div className="mb-6 flex flex-col gap-3">
          <Skeleton className="h-3.5 w-24" />
          <Skeleton className="h-7 w-72" />
          <Skeleton className="h-4 w-40" />
        </div>
      ) : (
        <PageHeader
          className="mb-4"
          back={{ href: "/", label: "ダッシュボード" }}
          title={project.title}
          description={project.artist_name}
          meta={
            <>
              <StatusBadge kind="analysis" status={project.analysis_status} />
              {project.rights_confirmed_at ? <Badge tone="success">権利確認済み</Badge> : <Badge>権利未確認</Badge>}
            </>
          }
          actions={actions}
        />
      )}
      <ProjectNav projectId={projectId} active={active} batchId={batchId} />
    </>
  );
}
