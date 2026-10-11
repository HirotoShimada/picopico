import { cva, type VariantProps } from "class-variance-authority";
import { CircleAlert, CircleCheck, Info, TriangleAlert } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/utils";

const alertVariants = cva("flex gap-3 rounded-md border px-3.5 py-3 text-sm leading-6", {
  variants: {
    tone: {
      neutral: "border-border bg-surface-raised text-foreground [&>svg]:text-muted-foreground",
      success: "border-success/30 bg-success-soft text-foreground [&>svg]:text-success",
      warning: "border-warning/30 bg-warning-soft text-foreground [&>svg]:text-warning",
      danger: "border-danger/35 bg-danger-soft text-foreground [&>svg]:text-danger",
    },
  },
  defaultVariants: { tone: "neutral" },
});

const ICONS = { neutral: Info, success: CircleCheck, warning: TriangleAlert, danger: CircleAlert } as const;

export interface AlertProps extends Omit<React.ComponentProps<"div">, "title">, VariantProps<typeof alertVariants> {
  title?: React.ReactNode;
  icon?: React.ComponentType<{ className?: string }> | null;
  action?: React.ReactNode;
}

/** Inline callout for notes, warnings and errors (always icon + text, never color alone). */
function Alert({ className, tone = "neutral", title, icon, action, children, ...props }: AlertProps) {
  const Icon = icon === null ? null : (icon ?? ICONS[tone ?? "neutral"]);
  return (
    <div
      data-slot="alert"
      role={tone === "danger" || tone === "warning" ? "alert" : "status"}
      className={cn(alertVariants({ tone }), className)}
      {...props}
    >
      {Icon ? <Icon className="mt-1 size-4 shrink-0" aria-hidden /> : null}
      <div className="min-w-0 flex-1">
        {title ? <p className="font-medium text-foreground">{title}</p> : null}
        {children ? <div className={cn("text-muted-foreground", !title && "text-foreground/90")}>{children}</div> : null}
      </div>
      {action ? <div className="shrink-0 self-center">{action}</div> : null}
    </div>
  );
}

export { Alert, alertVariants };
