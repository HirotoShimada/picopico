import * as React from "react";

import { Skeleton } from "@/components/ui/skeleton";

/** Tabs + content placeholder (no hooks: usable as a prerendered Suspense fallback). */
export function WorkspaceBodySkeleton() {
  return (
    <div className="flex flex-col gap-6" aria-busy aria-label="読み込み中">
      <Skeleton className="h-9 w-[30rem] max-w-full" />
      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(320px,360px)]">
        <Skeleton className="h-[420px] w-full" />
        <div className="flex flex-col gap-6">
          <Skeleton className="h-40 w-full" />
          <Skeleton className="h-56 w-full" />
        </div>
      </div>
    </div>
  );
}

/** Whole-page placeholder for `/projects/[id]` while the route params resolve. */
export function ProjectWorkspaceSkeleton() {
  return (
    <div className="flex flex-col" aria-busy aria-label="プロジェクトを読み込み中">
      <div className="mb-4 flex flex-col gap-3">
        <Skeleton className="h-3.5 w-24" />
        <Skeleton className="h-7 w-72" />
        <Skeleton className="h-4 w-40" />
      </div>
      <Skeleton className="mb-6 h-10 w-full" />
      <WorkspaceBodySkeleton />
    </div>
  );
}
