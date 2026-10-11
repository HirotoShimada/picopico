import type { Metadata } from "next";
import { Suspense } from "react";

import { ProjectWorkspace } from "@/components/project/project-workspace";
import { ProjectWorkspaceSkeleton } from "@/components/project/workspace-skeleton";

export const metadata: Metadata = {
  title: "プロジェクト",
};

/**
 * `/projects/[id]` — tabs 楽曲解析 / 歌詞 / 素材 / 権利確認 (`?tab=analysis|lyrics|materials|rights`).
 * Data is fetched in the browser (SWR → FastAPI); the params are runtime data under
 * Cache Components, so they are read inside a Suspense boundary.
 */
export default function ProjectPage({ params }: { params: Promise<{ id: string }> }) {
  return (
    <Suspense fallback={<ProjectWorkspaceSkeleton />}>
      {params.then(({ id }) => (
        <ProjectWorkspace projectId={id} />
      ))}
    </Suspense>
  );
}
