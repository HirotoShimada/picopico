"use client";

import * as React from "react";
import { useSWRConfig } from "swr";

import { toast } from "@/components/ui/toast";
import {
  apiPaths,
  downloadVideo,
  downloadVideosZip,
  downloadBatchZip,
  errorMessage,
  regenerateVideo,
  retryVideo,
  updateVideo,
} from "@/lib/api";
import type { VideoOut } from "@/lib/types";

type VideoRef = Pick<VideoOut, "id" | "project_id" | "batch_id">;

/** Display title with a fallback (`動画 #3`). */
export function videoTitle(video: Pick<VideoOut, "title" | "index">): string {
  return video.title?.trim() || `動画 #${video.index + 1}`;
}

/**
 * Mutations on videos with Japanese toasts. Every action revalidates the SWR
 * keys that show the video: its detail, the project's video lists (all
 * filters), its batch and the dashboard.
 */
export function useVideoActions() {
  const { mutate } = useSWRConfig();
  const [pending, setPending] = React.useState<Record<string, string>>({});

  const revalidate = React.useCallback(
    (video: VideoRef) => {
      const listPrefix = apiPaths.projectVideos(video.project_id);
      return mutate(
        (key) =>
          typeof key === "string" &&
          (key === apiPaths.video(video.id) ||
            key === listPrefix ||
            key.startsWith(`${listPrefix}?`) ||
            key === apiPaths.batch(video.batch_id) ||
            key === apiPaths.project(video.project_id) ||
            key === apiPaths.dashboard),
      );
    },
    [mutate],
  );

  const run = React.useCallback(
    async <T,>(video: VideoRef, action: string, fn: () => Promise<T>, success?: string, failure = "操作に失敗しました"): Promise<T | undefined> => {
      setPending((current) => ({ ...current, [video.id]: action }));
      try {
        const result = await fn();
        if (success) toast.success(success);
        await revalidate(video);
        return result;
      } catch (error) {
        toast.error(failure, errorMessage(error));
        return undefined;
      } finally {
        setPending((current) => {
          const next = { ...current };
          delete next[video.id];
          return next;
        });
      }
    },
    [revalidate],
  );

  return {
    /** Action name currently running for a video id (`retry`, `regenerate`, `favorite`, `download`). */
    pending,
    revalidate,
    retry: (video: VideoRef) =>
      run(video, "retry", () => retryVideo(video.id), "再レンダリングを開始しました", "再試行できませんでした"),
    regenerate: (video: VideoRef, newClips: boolean) =>
      run(
        video,
        "regenerate",
        () => regenerateVideo(video.id, { new_clips: newClips }),
        newClips ? "新しい素材で再生成を開始しました" : "別バリエーションで再生成を開始しました",
        "再生成できませんでした",
      ),
    setFavorite: (video: VideoRef, favorite: boolean) =>
      run(video, "favorite", () => updateVideo(video.id, { favorite }), undefined, "お気に入りを更新できませんでした"),
    download: (video: VideoRef) => run(video, "download", () => downloadVideo(video.id), undefined, "ダウンロードできませんでした"),
  };
}

/** ZIP downloads (selection or whole batch) with a busy flag and error toast. */
export function useZipDownload() {
  const [busy, setBusy] = React.useState(false);

  async function wrap(fn: () => Promise<void>) {
    setBusy(true);
    try {
      await fn();
    } catch (error) {
      toast.error("ZIP をダウンロードできませんでした", errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  return {
    busy,
    downloadSelection: (ids: string[], filename?: string) => wrap(() => downloadVideosZip(ids, filename)),
    downloadBatch: (batchId: string, filename?: string) => wrap(() => downloadBatchZip(batchId, filename)),
  };
}
