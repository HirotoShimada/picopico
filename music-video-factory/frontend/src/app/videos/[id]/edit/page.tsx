import type { Metadata } from "next";
import { Suspense } from "react";

import { EditorSkeleton, EditorView } from "@/components/editor/editor-view";

export const metadata: Metadata = {
  title: "動画の編集",
};

/**
 * `/videos/[id]/edit` — simple editor: player of the current render, lyric
 * timing nudges, lyric/text style, colour grade, start position and clip swaps;
 * 再レンダリング sends `PUT /api/videos/{id}/edit` and polls until done.
 */
export default function EditVideoPage({ params }: { params: Promise<{ id: string }> }) {
  return (
    <Suspense fallback={<EditorSkeleton />}>
      {params.then(({ id }) => (
        <EditorView videoId={id} />
      ))}
    </Suspense>
  );
}
