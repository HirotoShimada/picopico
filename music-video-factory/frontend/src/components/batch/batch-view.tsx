"use client";

import { Ban, Download, Film } from "lucide-react";
import Link from "next/link";
import * as React from "react";
import { useSWRConfig } from "swr";

import { galleryHref, generateHref } from "@/components/generate/links";
import { ProjectHeader } from "@/components/layout/project-header";
import { useZipDownload } from "@/components/videos/video-actions";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/ui/status-badge";
import { toast } from "@/components/ui/toast";
import { apiPaths, cancelBatch, errorMessage } from "@/lib/api";
import { formatCount, formatDateTime } from "@/lib/format";
import { isBatchActive } from "@/lib/status";
import type { BatchDetailOut } from "@/lib/types";

import { BatchProgress } from "./batch-progress";
import { BatchClipsCard, BatchSettingsCard, BatchStoryboardCard } from "./batch-side-panels";
import { BatchVideoTable } from "./batch-video-table";
import { useBatchDetail } from "./use-batch-detail";

/**
 * The project header (job/video counts, "実行中" badge) and the gallery read other SWR
 * keys that only poll on their own schedule. Refresh them as soon as this batch changes
 * status or finishes a video, so the header never claims a job is still running.
 */
function useRefreshProjectOnProgress(batch: BatchDetailOut | undefined) {
  const { mutate } = useSWRConfig();
  const signature = batch ? `${batch.status}:${batch.counts.done}:${batch.counts.failed}` : null;
  const previous = React.useRef<string | null>(null);
  const projectId = batch?.project_id;
  React.useEffect(() => {
    if (!projectId || signature === null) return;
    const changed = previous.current !== null && previous.current !== signature;
    previous.current = signature;
    if (!changed) return;
    const videosPrefix = apiPaths.projectVideos(projectId);
    void mutate(apiPaths.projectBatches(projectId));
    void mutate(apiPaths.project(projectId));
    void mutate(apiPaths.dashboard);
    void mutate((key) => typeof key === "string" && key.startsWith(videosPrefix));
  }, [mutate, projectId, signature]);
}

export function BatchSkeleton() {
  return (
    <div className="flex flex-col gap-6" aria-busy aria-label="読み込み中">
      <div className="flex flex-col gap-3">
        <Skeleton className="h-3.5 w-24" />
        <Skeleton className="h-7 w-72" />
      </div>
      <Skeleton className="h-40 w-full" />
      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1fr)_340px]">
        <Skeleton className="h-96 w-full" />
        <Skeleton className="h-80 w-full" />
      </div>
    </div>
  );
}

function StatusCallout({ batch }: { batch: BatchDetailOut }) {
  const galleryLink = (
    <Link href={galleryHref(batch.project_id, batch.id)} className="font-medium text-foreground underline underline-offset-2">
      動画一覧を開く
    </Link>
  );
  switch (batch.status) {
    case "budget_exceeded":
      return (
        <Alert tone="warning" title="予算上限に達したため停止しました" className="mb-6">
          {batch.error ?? "これ以上の素材生成は予算を超えるため中止しました。"} 完成した動画はそのまま利用できます。予算や本数を見直して再度生成してください。
        </Alert>
      );
    case "failed":
      return (
        <Alert tone="danger" title="生成に失敗しました" className="mb-6">
          {batch.error ?? "原因不明のエラーで停止しました。"}{" "}
          <Link href={generateHref(batch.project_id)} className="font-medium text-foreground underline underline-offset-2">
            生成設定に戻る
          </Link>
        </Alert>
      );
    case "partial":
      return (
        <Alert tone="warning" title={`${formatCount(batch.counts.failed)} 本の動画が失敗しました`} className="mb-6">
          失敗した動画は下の一覧から個別に再試行できます。完成した {formatCount(batch.counts.done)} 本はダウンロードできます。 {galleryLink}
        </Alert>
      );
    case "cancelled":
      return (
        <Alert tone="neutral" title="この生成はキャンセルされました" className="mb-6">
          キャンセル前に完成した動画はそのまま利用できます。
        </Alert>
      );
    case "completed":
      return (
        <Alert tone="success" title={`${formatCount(batch.counts.done)} 本の動画が完成しました`} className="mb-6">
          動画一覧でプレビュー・ダウンロード・お気に入り登録ができます。 {galleryLink}
        </Alert>
      );
    default:
      return batch.error ? (
        <Alert tone="warning" className="mb-6">
          {batch.error}
        </Alert>
      ) : null;
  }
}

/** `/batches/[id]`: live progress of one generation run, per-video status and actions. */
export function BatchView({ batchId }: { batchId: string }) {
  const { data: batch, error, mutate } = useBatchDetail(batchId);
  const { mutate: globalMutate } = useSWRConfig();
  const zip = useZipDownload();
  const [cancelOpen, setCancelOpen] = React.useState(false);
  useRefreshProjectOnProgress(batch);

  if (error && !batch) {
    return (
      <ErrorState
        error={error}
        title={error.isNotFound ? "生成ジョブが見つかりません" : undefined}
        onRetry={error.isNotFound ? undefined : () => void mutate()}
      />
    );
  }
  if (!batch) return <BatchSkeleton />;

  const active = isBatchActive(batch.status);
  const finished = !active;
  const hasDone = batch.counts.done > 0;

  async function handleCancel() {
    try {
      const updated = await cancelBatch(batchId);
      await mutate((current) => (current ? { ...current, ...updated } : current), { revalidate: true });
      void globalMutate(apiPaths.projectBatches(updated.project_id));
      void globalMutate(apiPaths.dashboard);
      toast.success("生成をキャンセルしました", "実行中の処理は区切りのよいところで停止します。");
    } catch (err) {
      toast.error("キャンセルできませんでした", errorMessage(err));
      throw err;
    }
  }

  return (
    <>
      <ProjectHeader
        projectId={batch.project_id}
        active="jobs"
        batchId={batch.id}
        actions={
          <>
            {active ? (
              <Button variant="destructive-outline" onClick={() => setCancelOpen(true)}>
                <Ban aria-hidden />
                生成をキャンセル
              </Button>
            ) : null}
            {hasDone ? (
              <Button
                variant="secondary"
                onClick={() => void zip.downloadBatch(batch.id, `batch_${batch.id.slice(0, 8)}.zip`)}
                loading={zip.busy}
              >
                {zip.busy ? null : <Download aria-hidden />}
                完成分を ZIP で保存
              </Button>
            ) : null}
            <Button variant={finished && hasDone ? "primary" : "secondary"} asChild>
              <Link href={galleryHref(batch.project_id, batch.id)}>
                <Film aria-hidden />
                動画一覧へ
              </Link>
            </Button>
          </>
        }
      />

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <h2 className="text-base font-semibold text-foreground">生成ジョブ</h2>
        <StatusBadge kind="batch" status={batch.status} />
        <span className="font-mono text-2xs text-subtle-foreground">{batch.id}</span>
        <span className="text-xs text-muted-foreground">作成 {formatDateTime(batch.created_at)}</span>
        {active ? <span className="text-xs text-muted-foreground">・ 自動で更新しています</span> : null}
      </div>

      <StatusCallout batch={batch} />
      <BatchProgress batch={batch} />

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1fr)_340px]">
        <BatchVideoTable videos={batch.videos ?? []} />
        <aside className="flex min-w-0 flex-col gap-4">
          <BatchSettingsCard batch={batch} />
          <BatchClipsCard batch={batch} />
          <BatchStoryboardCard batch={batch} />
        </aside>
      </div>

      <ConfirmDialog
        open={cancelOpen}
        onOpenChange={setCancelOpen}
        destructive
        title="生成をキャンセルしますか？"
        description="まだ始まっていない動画と素材の生成を中止します。完成済みの動画と生成済みの素材は残ります。実行中の外部 API の生成は途中で止められない場合があり、その分の費用が発生することがあります。"
        confirmLabel="キャンセルする"
        cancelLabel="続行する"
        onConfirm={handleCancel}
      />
    </>
  );
}
