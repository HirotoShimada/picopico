import * as React from "react";

import { cn } from "@/lib/utils";

import { Skeleton } from "./skeleton";

export interface StatCardProps extends Omit<React.ComponentProps<"div">, "title"> {
  label: React.ReactNode;
  value: React.ReactNode;
  /** Secondary line under the value (context, comparison). */
  hint?: React.ReactNode;
  icon?: React.ComponentType<{ className?: string }>;
  loading?: boolean;
  /** Optional element under the hint (e.g. a progress bar). */
  footer?: React.ReactNode;
}

/** KPI tile: label, large value, hint. Text stays neutral; color is carried by `footer` marks only. */
function StatCard({ label, value, hint, icon: Icon, loading = false, footer, className, ...props }: StatCardProps) {
  return (
    <div
      data-slot="stat-card"
      className={cn("flex min-w-0 flex-col gap-1.5 rounded-lg border border-border bg-surface px-4 py-3.5", className)}
      {...props}
    >
      <div className="flex items-center justify-between gap-2">
        <p className="truncate text-xs font-medium text-muted-foreground">{label}</p>
        {Icon ? <Icon className="size-4 shrink-0 text-subtle-foreground" aria-hidden /> : null}
      </div>
      {loading ? (
        <>
          <Skeleton className="h-7 w-20" />
          <Skeleton className="h-4 w-28" />
        </>
      ) : (
        <>
          <p className="truncate text-2xl font-semibold leading-8 tracking-tight text-foreground">{value}</p>
          {hint ? <p className="truncate text-xs leading-5 text-muted-foreground">{hint}</p> : null}
          {footer}
        </>
      )}
    </div>
  );
}

export { StatCard };
