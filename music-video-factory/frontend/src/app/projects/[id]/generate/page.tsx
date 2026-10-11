import type { Metadata } from "next";
import { Suspense } from "react";

import { GenerateSkeleton, GenerateView } from "@/components/generate/generate-view";

export const metadata: Metadata = {
  title: "生成設定",
};

/**
 * `/projects/[id]/generate` — world preset, provider/model, length/count, lyric
 * style, live cost estimate and storyboard preview; 生成開始 → `/batches/[id]`.
 * The params are runtime data under Cache Components, so they are read inside Suspense.
 */
export default function GeneratePage({ params }: { params: Promise<{ id: string }> }) {
  return (
    <Suspense fallback={<GenerateSkeleton />}>
      {params.then(({ id }) => (
        <GenerateView projectId={id} />
      ))}
    </Suspense>
  );
}
