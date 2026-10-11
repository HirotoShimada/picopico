"use client";

import { Clapperboard, Download, Film, FilterX, ListChecks, X } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import * as React from "react";
import { useSWRConfig } from "swr";

import { batchHref, generateHref } from "@/components/generate/links";
import { ProjectHeader } from "@/components/layout/project-header";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Select, SelectContent, SelectItem, SelectSeparator, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/ui/status-badge";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { useProjectBatches } from "@/hooks/use-batches";
import { useVideos } from "@/hooks/use-videos";
import { apiPaths } from "@/lib/api";
import { formatCount, formatDateTime } from "@/lib/format";
import { statusMeta } from "@/lib/status";
import type { BatchOut, VideoOut } from "@/lib/types";

import { useVideoActions, useZipDownload } from "./video-actions";
import { VideoCard } from "./video-card";
import { VideoPreviewDialog } from "./video-preview-dialog";

const ALL = "all";
const GRID_CLASSES = "grid grid-cols-2 gap-x-4 gap-y-6 md:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5";

export function GallerySkeleton() {
  return (
    <div className="flex flex-col gap-5" aria-busy aria-label="読み込み中">
      <Skeleton className="h-9 w-96" />
      <ul className={GRID_CLASSES}>
        {Array.from({ length: 8 }, (_, i) => (
          <li key={i} className="flex flex-col gap-2">
            <Skeleton className="aspect-[9/16] w-full" />
            <Skeleton className="h-4 w-2/3" />
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Latest batch first, then by index inside a batch. */
function sortVideos(videos: readonly VideoOut[], batches: readonly BatchOut[] | undefined): VideoOut[] {
  const batchCreated = new Map((batches ?? []).map((batch) => [batch.id, batch.created_at]));
  return [...videos].sort((a, b) => {
    const left = batchCreated.get(a.batch_id) ?? a.created_at;
    const right = batchCreated.get(b.batch_id) ?? b.created_at;
    if (left !== right) return right.localeCompare(left);
    if (a.batch_id !== b.batch_id) return a.batch_id.localeCompare(b.batch_id);
    return a.index - b.index;
  });
}

interface VideoGroup {
  batchId: string;
  batch: BatchOut | undefined;
  videos: VideoOut[];
}

/** Consecutive videos of the same batch (input is already sorted batch by batch). */
function groupByBatch(videos: readonly VideoOut[], batches: readonly BatchOut[]): VideoGroup[] {
  const byId = new Map(batches.map((batch) => [batch.id, batch]));
  const groups: VideoGroup[] = [];
  for (const video of videos) {
    const last = groups[groups.length - 1];
    if (last && last.batchId === video.batch_id) last.videos.push(video);
    else groups.push({ batchId: video.batch_id, batch: byId.get(video.batch_id), videos: [video] });
  }
  return groups;
}

function batchOptionLabel(batch: BatchOut): string {
  return `${formatDateTime(batch.created_at)} ・ ${formatCount(batch.counts.done)}/${formatCount(batch.counts.total)} 本 ・ ${statusMeta("batch", batch.status).label}`;
}

/** `/projects/[id]/videos`: filterable 9:16 grid, preview dialog, favourites and ZIP download. */
export function GalleryView({ projectId }: { projectId: string }) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const batchFilter = searchParams.get("batch");
  const favoritesOnly = searchParams.get("fav") === "1";

  const { data: batches } = useProjectBatches(projectId);
  const { data: videos, error, isLoading, mutate } = useVideos(projectId, {
    batch_id: batchFilter ?? undefined,
    favorite: favoritesOnly ? true : undefined,
  });
  const { mutate: globalMutate } = useSWRConfig();
  const actions = useVideoActions();
  const zip = useZipDownload();

  const [previewId, setPreviewId] = React.useState<string | null>(null);
  const [selected, setSelected] = React.useState<ReadonlySet<string>>(() => new Set());

  // Close the player when the route is hidden (Cache Components keep it mounted).
  React.useLayoutEffect(() => () => setPreviewId(null), []);

  const sorted = React.useMemo(() => sortVideos(videos ?? [], batches), [videos, batches]);
  const doneIds = React.useMemo(() => sorted.filter((video) => video.status === "done").map((video) => video.id), [sorted]);
  const selectedIds = doneIds.filter((id) => selected.has(id));
  const sortedBatches = React.useMemo(
    () => [...(batches ?? [])].sort((a, b) => b.created_at.localeCompare(a.created_at)),
    [batches],
  );
  const filterBatch = sortedBatches.find((batch) => batch.id === batchFilter);
  const groups = React.useMemo(() => groupByBatch(sorted, sortedBatches), [sorted, sortedBatches]);

  function setFilters(next: { batch?: string | null; fav?: boolean }) {
    const params = new URLSearchParams(searchParams.toString());
    const batch = next.batch === undefined ? batchFilter : next.batch;
    const fav = next.fav === undefined ? favoritesOnly : next.fav;
    if (batch) params.set("batch", batch);
    else params.delete("batch");
    if (fav) params.set("fav", "1");
    else params.delete("fav");
    const query = params.toString();
    setSelected(new Set());
    router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
  }

  function toggleSelected(id: string, value: boolean) {
    setSelected((current) => {
      const next = new Set(current);
      if (value) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  async function toggleFavorite(video: VideoOut) {
    const favorite = !video.favorite;
    // Optimistic: flip the star in the visible list right away.
    void mutate((current) => current?.map((item) => (item.id === video.id ? { ...item, favorite } : item)), { revalidate: false });
    const result = await actions.setFavorite(video, favorite);
    if (result === undefined) void mutate();
  }

  const reloadUrls = () => {
    void mutate();
    void globalMutate(apiPaths.projectVideos(projectId));
  };

  const filtered = batchFilter !== null || favoritesOnly;
  const zipName = filterBatch ? `batch_${filterBatch.id.slice(0, 8)}.zip` : "videos.zip";

  return (
    <>
      <ProjectHeader
        projectId={projectId}
        active="videos"
        actions={
          <Button variant="secondary" asChild>
            <Link href={generateHref(projectId)}>
              <Clapperboard aria-hidden />
              新しく生成
            </Link>
          </Button>
        }
      />

      <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <Select value={batchFilter ?? ALL} onValueChange={(value) => setFilters({ batch: value === ALL ? null : value })}>
            <SelectTrigger className="w-80" aria-label="生成ジョブで絞り込み">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>すべての生成ジョブ</SelectItem>
              {sortedBatches.length > 0 ? <SelectSeparator /> : null}
              {sortedBatches.map((batch) => (
                <SelectItem key={batch.id} value={batch.id}>
                  {batchOptionLabel(batch)}
                </SelectItem>
              ))}
              {batchFilter && !filterBatch ? <SelectItem value={batchFilter}>{batchFilter}</SelectItem> : null}
            </SelectContent>
          </Select>
          <ToggleGroup
            type="single"
            value={favoritesOnly ? "fav" : ALL}
            onValueChange={(value) => value && setFilters({ fav: value === "fav" })}
            aria-label="お気に入りで絞り込み"
          >
            <ToggleGroupItem value={ALL}>すべて</ToggleGroupItem>
            <ToggleGroupItem value="fav">お気に入り</ToggleGroupItem>
          </ToggleGroup>
          {filtered ? (
            <Button variant="ghost" size="sm" onClick={() => setFilters({ batch: null, fav: false })}>
              <FilterX aria-hidden />
              絞り込みを解除
            </Button>
          ) : null}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {videos ? (
            <span className="text-xs text-muted-foreground tabular">
              {formatCount(sorted.length)} 本{selectedIds.length > 0 ? ` ・ ${formatCount(selectedIds.length)} 本選択中` : ""}
            </span>
          ) : null}
          {doneIds.length > 0 ? (
            selectedIds.length === doneIds.length ? (
              <Button variant="ghost" size="sm" onClick={() => setSelected(new Set())}>
                <X aria-hidden />
                選択を解除
              </Button>
            ) : (
              <Button variant="ghost" size="sm" onClick={() => setSelected(new Set(doneIds))}>
                <ListChecks aria-hidden />
                完成分をすべて選択
              </Button>
            )
          ) : null}
          {selectedIds.length > 0 ? (
            <Button variant="primary" size="sm" loading={zip.busy} onClick={() => void zip.downloadSelection(selectedIds, zipName)}>
              {zip.busy ? null : <Download aria-hidden />}
              選択した {formatCount(selectedIds.length)} 本を ZIP で保存
            </Button>
          ) : filterBatch && filterBatch.counts.done > 0 ? (
            <Button variant="secondary" size="sm" loading={zip.busy} onClick={() => void zip.downloadBatch(filterBatch.id, zipName)}>
              {zip.busy ? null : <Download aria-hidden />}
              このジョブの完成分を ZIP で保存
            </Button>
          ) : null}
        </div>
      </div>

      {error && !videos ? (
        <ErrorState error={error} onRetry={() => void mutate()} />
      ) : isLoading && !videos ? (
        <GallerySkeleton />
      ) : sorted.length === 0 ? (
        filtered ? (
          <EmptyState
            icon={FilterX}
            title="条件に一致する動画はありません"
            description={favoritesOnly ? "お気に入りに追加した動画がここに表示されます。" : "別の生成ジョブを選ぶか、絞り込みを解除してください。"}
            action={
              <Button variant="secondary" size="sm" onClick={() => setFilters({ batch: null, fav: false })}>
                絞り込みを解除
              </Button>
            }
          />
        ) : (
          <EmptyState
            icon={Film}
            title="まだ動画がありません"
            description="生成設定で世界観と本数を選んで生成を開始すると、完成した動画がここに並びます。"
            action={
              <Button variant="primary" size="sm" asChild>
                <Link href={generateHref(projectId)}>
                  <Clapperboard aria-hidden />
                  生成設定へ
                </Link>
              </Button>
            }
          />
        )
      ) : (
        <div className="flex flex-col gap-8">
          {groups.map((group) => (
            <section key={group.batchId} className="flex flex-col gap-3" aria-label={group.batch ? `${formatDateTime(group.batch.created_at)} の生成` : "生成ジョブ"}>
              {batchFilter === null ? (
                <div className="flex flex-wrap items-center gap-2 border-b border-border pb-2">
                  <h2 className="text-sm font-medium text-foreground">
                    {group.batch ? `${formatDateTime(group.batch.created_at)} の生成` : "生成ジョブ"}
                  </h2>
                  {group.batch ? <StatusBadge kind="batch" status={group.batch.status} /> : null}
                  <span className="text-xs text-muted-foreground tabular">{formatCount(group.videos.length)} 本</span>
                  <div className="ml-auto flex items-center gap-1">
                    <Button variant="ghost" size="xs" onClick={() => setFilters({ batch: group.batchId })}>
                      このジョブだけ表示
                    </Button>
                    <Button variant="ghost" size="xs" asChild>
                      <Link href={batchHref(group.batchId)}>ジョブの詳細</Link>
                    </Button>
                  </div>
                </div>
              ) : null}
              <ul className={GRID_CLASSES}>
                {group.videos.map((video) => (
                  <VideoCard
                    key={video.id}
                    video={video}
                    selected={selected.has(video.id)}
                    selecting={selectedIds.length > 0}
                    onSelectedChange={(value) => toggleSelected(video.id, value)}
                    onOpen={() => setPreviewId(video.id)}
                    onToggleFavorite={() => void toggleFavorite(video)}
                    favoritePending={actions.pending[video.id] === "favorite"}
                  />
                ))}
              </ul>
            </section>
          ))}
        </div>
      )}

      <VideoPreviewDialog videos={sorted} videoId={previewId} onVideoIdChange={setPreviewId} onReload={reloadUrls} />
    </>
  );
}
