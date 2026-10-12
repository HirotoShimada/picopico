import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { providerLabel } from "@/lib/constants";
import { formatCount, formatUsd } from "@/lib/format";
import { isOpenRouter } from "@/lib/llm";
import type { UsageOut } from "@/lib/types";

/** API spend by provider: a single-series magnitude, so one neutral hue and values as text. */
export function UsagePanel({ usage, loading }: { usage: UsageOut | undefined; loading: boolean }) {
  const rows = usage ? [...usage.by_provider].sort((a, b) => b.amount_usd - a.amount_usd) : [];
  const max = rows.reduce((m, row) => Math.max(m, row.amount_usd), 0);

  return (
    <Card>
      <CardHeader>
        <CardTitle>API 利用状況</CardTitle>
        <CardDescription>動画生成・LLM の実費（キャッシュ再利用分は 0 円）</CardDescription>
      </CardHeader>
      <div className="flex flex-col gap-4 px-5 pb-5">
        {loading || !usage ? (
          <>
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-24 w-full" />
          </>
        ) : (
          <>
            <dl className="grid grid-cols-2 gap-3">
              <div className="rounded-md border border-border bg-surface-raised px-3 py-2">
                <dt className="text-2xs text-muted-foreground">今月</dt>
                <dd className="text-lg font-semibold">{formatUsd(usage.month_usd)}</dd>
              </div>
              <div className="rounded-md border border-border bg-surface-raised px-3 py-2">
                <dt className="text-2xs text-muted-foreground">累計</dt>
                <dd className="text-lg font-semibold">{formatUsd(usage.total_usd)}</dd>
              </div>
            </dl>

            {rows.length === 0 ? (
              <p className="text-sm leading-6 text-muted-foreground">
                まだ有料 API の利用はありません。モックとアップロード素材は無料です。
              </p>
            ) : (
              <table className="w-full text-sm">
                <caption className="sr-only">プロバイダー別の利用額</caption>
                <thead>
                  <tr className="text-2xs text-subtle-foreground">
                    <th className="pb-1.5 text-left font-medium">プロバイダー</th>
                    <th className="pb-1.5 text-right font-medium">クリップ</th>
                    <th className="pb-1.5 text-right font-medium">金額</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.provider} className="align-top">
                      <td className="py-1.5 pr-3">
                        <span className="block truncate">{providerLabel(row.provider)}</span>
                        <span className="mt-1 block h-1.5 w-full rounded-full bg-zinc-800" aria-hidden>
                          <span
                            className="block h-full rounded-full bg-zinc-400"
                            style={{ width: max > 0 ? `${Math.max(2, (row.amount_usd / max) * 100)}%` : "0%" }}
                          />
                        </span>
                      </td>
                      <td className="py-1.5 text-right text-muted-foreground tabular">{formatCount(row.clips)}</td>
                      <td className="py-1.5 pl-3 text-right font-medium tabular">{formatUsd(row.amount_usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {rows.some((row) => isOpenRouter(row.provider)) ? (
              <p className="text-2xs leading-4 text-subtle-foreground">
                OpenRouter の金額には、動画生成に加えて OpenRouter 経由の Claude（絵コンテ）の費用も含みます。クリップ数は動画生成の分だけです。
              </p>
            ) : null}

            <dl className="grid grid-cols-3 gap-2 border-t border-border pt-3 text-center">
              <div>
                <dt className="text-2xs text-muted-foreground">生成クリップ</dt>
                <dd className="text-sm font-medium tabular">{formatCount(usage.clips_generated)}</dd>
              </div>
              <div>
                <dt className="text-2xs text-muted-foreground">再利用</dt>
                <dd className="text-sm font-medium tabular">{formatCount(usage.clips_reused)}</dd>
              </div>
              <div>
                <dt className="text-2xs text-muted-foreground">節約額</dt>
                <dd className="text-sm font-medium tabular">{formatUsd(usage.saved_usd)}</dd>
              </div>
            </dl>
          </>
        )}
      </div>
    </Card>
  );
}
