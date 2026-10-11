"use client";

import * as React from "react";

import { ProjectHeader } from "@/components/layout/project-header";
import { Skeleton } from "@/components/ui/skeleton";
import { useProjectBatches } from "@/hooks/use-batches";
import { useProject } from "@/hooks/use-projects";

import { GenerateForm } from "./generate-form";

export function GenerateSkeleton() {
  return (
    <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1fr)_380px]" aria-busy aria-label="読み込み中">
      <div className="flex flex-col gap-6">
        <Skeleton className="h-72 w-full" />
        <Skeleton className="h-56 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
      <div className="flex flex-col gap-4">
        <Skeleton className="h-80 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    </div>
  );
}

/** `/projects/[id]/generate`: project header + the generation settings form. */
export function GenerateView({ projectId }: { projectId: string }) {
  const { data: project, error: projectError } = useProject(projectId);
  const { data: batches, error: batchesError } = useProjectBatches(projectId);

  const previous = React.useMemo(() => {
    if (!batches || batches.length === 0) return null;
    return [...batches].sort((a, b) => b.created_at.localeCompare(a.created_at))[0].settings;
  }, [batches]);

  // Wait for the batch list (best effort) so the form can start from the last settings.
  const ready = project !== undefined && (batches !== undefined || batchesError !== undefined);

  return (
    <>
      <ProjectHeader projectId={projectId} active="generate" />
      {projectError && !project ? null : ready ? (
        <GenerateForm key={project.id} project={project} previous={previous} />
      ) : (
        <GenerateSkeleton />
      )}
    </>
  );
}
