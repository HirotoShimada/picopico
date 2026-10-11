import { Inbox } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/utils";

export interface EmptyStateProps extends Omit<React.ComponentProps<"div">, "title"> {
  icon?: React.ComponentType<{ className?: string }>;
  title: React.ReactNode;
  description?: React.ReactNode;
  /** Primary/secondary actions (buttons or links). */
  action?: React.ReactNode;
  size?: "sm" | "md";
}

function EmptyState({ icon: Icon = Inbox, title, description, action, size = "md", className, ...props }: EmptyStateProps) {
  return (
    <div
      data-slot="empty-state"
      className={cn(
        "flex flex-col items-center justify-center rounded-lg border border-dashed border-border text-center",
        size === "md" ? "gap-3 px-6 py-12" : "gap-2 px-4 py-8",
        className,
      )}
      {...props}
    >
      <div className="flex size-10 items-center justify-center rounded-full border border-border bg-surface-raised">
        <Icon className="size-5 text-muted-foreground" aria-hidden />
      </div>
      <div className="flex max-w-md flex-col gap-1">
        <p className="text-sm font-medium text-foreground">{title}</p>
        {description ? <p className="text-sm leading-6 text-balance text-muted-foreground">{description}</p> : null}
      </div>
      {action ? <div className="mt-1 flex flex-wrap items-center justify-center gap-2">{action}</div> : null}
    </div>
  );
}

export { EmptyState };
