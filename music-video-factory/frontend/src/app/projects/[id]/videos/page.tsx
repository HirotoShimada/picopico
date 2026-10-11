import type { Metadata } from "next";
import { Suspense } from "react";

import { GallerySkeleton, GalleryView } from "@/components/videos/gallery-view";

export const metadata: Metadata = {
  title: "動画一覧",
};

/**
 * `/projects/[id]/videos` — gallery of the project's videos (`?batch=<id>&fav=1`
 * filters), preview, favourites, regenerate, edit and ZIP download.
 */
export default function VideosPage({ params }: { params: Promise<{ id: string }> }) {
  return (
    <Suspense fallback={<GallerySkeleton />}>
      {params.then(({ id }) => (
        <GalleryView projectId={id} />
      ))}
    </Suspense>
  );
}
