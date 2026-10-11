import { ListChecks } from "lucide-react";
import Link from "next/link";

import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/ui/status-badge";
import { formatCount } from "@/lib/format";
import type { ActiveJob } from "@/lib/types";

export function ActiveJobs({ jobs, loading }: { jobs: ActiveJob[] | undefined; loading: boolean }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>実行中のジョブ</CardTitle>
        <CardDescription>進捗は自動で更新されます</CardDescription>
      </CardHeader>
      <div className="px-5 pb-5">
        {loading ? (
          <div className="flex flex-col gap-3">
            <Skeleton className="h-12 w-full" />
            <Skeleton className="h-12 w-full" />
          </div>
        ) : !jobs || jobs.length === 0 ? (
          <EmptyState size="sm" icon={ListChecks} title="実行中のジョブはありません" description="生成を開始すると、ここに進捗が表示されます。" />
        ) : (
          <ul className="flex flex-col divide-y divide-border">
            {jobs.map((job) => {
              const finished = job.done + job.failed;
              const percent = job.total > 0 ? (finished / job.total) * 100 : null;
              return (
                <li key={job.batch_id} className="py-3 first:pt-0 last:pb-0">
                  <div className="mb-2 flex items-center justify-between gap-3">
                    <Link
                      href={`/batches/${job.batch_id}`}
                      className="focus-ring-custom min-w-0 truncate text-sm font-medium text-foreground outline-none hover:underline focus-visible:underline"
                    >
                      {job.project_title}
                    </Link>
                    <StatusBadge kind="batch" status={job.status} />
                  </div>
                  <Progress value={percent} aria-label={`${job.project_title} の進捗`} />
                  <p className="mt-1.5 flex justify-between text-xs text-muted-foreground tabular">
                    <span>
                      {formatCount(job.done)} / {formatCount(job.total)} 本完了
                    </span>
                    {job.failed > 0 ? <span className="text-danger-foreground">失敗 {formatCount(job.failed)} 本</span> : null}
                  </p>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </Card>
  );
}
