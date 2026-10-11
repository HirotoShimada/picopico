import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import * as React from "react";

import { cn } from "@/lib/utils";

export interface PageHeaderProps extends Omit<React.ComponentProps<"div">, "title"> {
  title: React.ReactNode;
  description?: React.ReactNode;
  /** Buttons on the right (put at most one `primary` button here). */
  actions?: React.ReactNode;
  /** Small back link above the title. */
  back?: { href: string; label: string };
  /** Inline badges next to the title. */
  meta?: React.ReactNode;
}

export function PageHeader({ title, description, actions, back, meta, className, ...props }: PageHeaderProps) {
  return (
    <div className={cn("mb-6 flex flex-col gap-3", className)} {...props}>
      {back ? (
        <Link
          href={back.href}
          className="focus-ring-custom inline-flex w-fit items-center gap-1 rounded text-xs text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/40"
        >
          <ArrowLeft className="size-3.5" aria-hidden />
          {back.label}
        </Link>
      ) : null}
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex min-w-0 flex-col gap-1">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <h1 className="truncate text-xl font-semibold tracking-tight text-foreground">{title}</h1>
            {meta}
          </div>
          {description ? <p className="max-w-3xl text-sm leading-6 text-muted-foreground">{description}</p> : null}
        </div>
        {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
    </div>
  );
}
