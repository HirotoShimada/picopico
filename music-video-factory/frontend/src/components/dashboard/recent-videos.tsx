import { Film } from "lucide-react";
import Link from "next/link";

import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/ui/status-badge";
import { Thumbnail } from "@/components/ui/thumbnail";
import { formatTime } from "@/lib/format";
import type { VideoOut } from "@/lib/types";

const MAX_VIDEOS = 12;

export function RecentVideos({ videos, loading }: { videos: VideoOut[] | undefined; loading: boolean }) {
  const items = (videos ?? []).slice(0, MAX_VIDEOS);
  return (
    <Card>
      <CardHeader>
        <CardTitle>最近の動画</CardTitle>
        <CardDescription>クリックするとエディターで開きます</CardDescription>
      </CardHeader>
      <div className="px-5 pb-5">
        {loading ? (
          <div className="grid grid-cols-3 gap-4 md:grid-cols-4 xl:grid-cols-6">
            {Array.from({ length: 6 }, (_, i) => (
              <Skeleton key={i} className="aspect-[9/16] w-full" />
            ))}
          </div>
        ) : items.length === 0 ? (
          <EmptyState size="sm" icon={Film} title="まだ動画はありません" description="生成が完了した動画がここに表示されます。" />
        ) : (
          <ul className="grid grid-cols-3 gap-4 md:grid-cols-4 xl:grid-cols-6">
            {items.map((video) => {
              const rendering = video.status === "rendering";
              return (
                <li key={video.id} className="min-w-0">
                  <Link
                    href={`/videos/${video.id}/edit`}
                    className="group focus-ring-custom flex flex-col gap-2 rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring/40"
                  >
                    <Thumbnail
                      src={video.thumbnail_url}
                      alt={`${video.title} のサムネイル`}
                      className="transition-colors group-hover:border-border-strong"
                      fallback={<Film className="size-6" aria-hidden />}
                      overlay={
                        <>
                          {video.status !== "done" ? (
                            <div className="absolute top-2 left-2">
                              <StatusBadge kind="video" status={video.status} />
                            </div>
                          ) : null}
                          {video.duration && !rendering ? (
                            <span className="absolute right-2 bottom-2 rounded bg-black/70 px-1.5 py-px font-mono text-2xs text-white">
                              {formatTime(video.duration)}
                            </span>
                          ) : null}
                          {rendering ? (
                            <div className="absolute inset-x-2 bottom-2">
                              <Progress value={video.progress * 100} size="sm" aria-label="レンダリング進捗" />
                            </div>
                          ) : null}
                        </>
                      }
                    />
                    <div className="flex min-w-0 flex-col">
                      <span className="truncate text-sm text-foreground group-hover:underline">{video.title || `動画 #${video.index + 1}`}</span>
                      <span className="text-2xs text-subtle-foreground tabular">
                        #{video.index + 1}
                        {video.render_version > 1 ? ` ・ v${video.render_version}` : ""}
                      </span>
                    </div>
                  </Link>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </Card>
  );
}
