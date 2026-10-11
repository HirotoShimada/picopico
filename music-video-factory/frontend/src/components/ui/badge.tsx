import { cva, type VariantProps } from "class-variance-authority";
import { Slot } from "radix-ui";
import * as React from "react";

import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex w-fit shrink-0 items-center gap-1 whitespace-nowrap rounded border px-1.5 py-px text-2xs font-medium leading-4 [&_svg]:pointer-events-none [&_svg:not([class*='size-'])]:size-3",
  {
    variants: {
      tone: {
        neutral: "border-border bg-surface-raised text-muted-foreground",
        outline: "border-border-strong bg-transparent text-foreground",
        /** Work in progress — the only blue badge. */
        progress: "border-primary/40 bg-primary-soft text-blue-300",
        success: "border-success/35 bg-success-soft text-success-foreground",
        warning: "border-warning/35 bg-warning-soft text-warning-foreground",
        danger: "border-danger/40 bg-danger-soft text-danger-foreground",
      },
    },
    defaultVariants: { tone: "neutral" },
  },
);

export type BadgeTone = NonNullable<VariantProps<typeof badgeVariants>["tone"]>;

function Badge({
  className,
  tone,
  asChild = false,
  ...props
}: React.ComponentProps<"span"> & VariantProps<typeof badgeVariants> & { asChild?: boolean }) {
  const Comp = asChild ? Slot.Root : "span";
  return <Comp data-slot="badge" className={cn(badgeVariants({ tone }), className)} {...props} />;
}

export { Badge, badgeVariants };
