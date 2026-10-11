import { CircleDollarSign, Film, FolderOpen, Hourglass, ListChecks, Recycle } from "lucide-react";

import { Progress } from "@/components/ui/progress";
import { StatCard } from "@/components/ui/stat-card";
import { formatCount, formatUsd } from "@/lib/format";
import type { DashboardOut } from "@/lib/types";

/** Headline numbers of the dashboard. */
export function KpiRow({ data, loading }: { data: DashboardOut | undefined; loading: boolean }) {
  const activeJobs = data?.active_jobs ?? [];
  const remaining = activeJobs.reduce((sum, job) => sum + Math.max(0, job.total - job.done - job.failed), 0);
  const doneRatio = data && data.videos_total > 0 ? (data.videos_done / data.videos_total) * 100 : 0;

  return (
    <section aria-label="概要" className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
      <StatCard label="プロジェクト" icon={FolderOpen} loading={loading} value={formatCount(data?.projects)} hint="作成済みの楽曲" />
      <StatCard
        label="完成した動画"
        icon={Film}
        loading={loading}
        value={formatCount(data?.videos_done)}
        hint={data ? `全 ${formatCount(data.videos_total)} 本中` : undefined}
        footer={data && data.videos_total > 0 ? <Progress value={doneRatio} tone="neutral" size="sm" className="mt-1" aria-label="完成率" /> : null}
      />
      <StatCard
        label="実行中のジョブ"
        icon={ListChecks}
        loading={loading}
        value={formatCount(activeJobs.length)}
        hint={activeJobs.length > 0 ? `残り ${formatCount(remaining)} 本` : "なし"}
      />
      <StatCard
        label="今月の API 利用額"
        icon={CircleDollarSign}
        loading={loading}
        value={formatUsd(data?.usage.month_usd)}
        hint={data ? `累計 ${formatUsd(data.usage.total_usd)}` : undefined}
      />
      <StatCard
        label="未消化の見積額"
        icon={Hourglass}
        loading={loading}
        value={formatUsd(data?.pending_estimate_usd)}
        hint="実行中ジョブの残り"
      />
      <StatCard
        label="素材の再利用"
        icon={Recycle}
        loading={loading}
        value={data ? `${formatCount(data.usage.clips_reused)} 本` : "—"}
        hint={data ? `節約額 ${formatUsd(data.usage.saved_usd)}` : undefined}
      />
    </section>
  );
}
