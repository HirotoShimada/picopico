import type { Metadata } from "next";
import { Suspense } from "react";

import { BatchSkeleton, BatchView } from "@/components/batch/batch-view";

export const metadata: Metadata = {
  title: "生成ジョブ",
};

/**
 * `/batches/[id]` — live progress of one generation run (polled while active),
 * per-video status with retry, cancel, cost so far and links to the gallery.
 */
export default function BatchPage({ params }: { params: Promise<{ id: string }> }) {
  return (
    <Suspense fallback={<BatchSkeleton />}>
      {params.then(({ id }) => (
        <BatchView batchId={id} />
      ))}
    </Suspense>
  );
}
