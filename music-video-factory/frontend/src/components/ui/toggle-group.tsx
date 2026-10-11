"use client";

import { ToggleGroup as ToggleGroupPrimitive } from "radix-ui";
import * as React from "react";

import { cn } from "@/lib/utils";

/** Segmented choice (e.g. 15 / 30 / 60 秒). Selected items use a neutral highlight. */
function ToggleGroup({ className, ...props }: React.ComponentProps<typeof ToggleGroupPrimitive.Root>) {
  return (
    <ToggleGroupPrimitive.Root
      data-slot="toggle-group"
      className={cn("inline-flex w-fit flex-wrap items-center gap-1 rounded-md border border-border bg-surface p-0.5", className)}
      {...props}
    />
  );
}

function ToggleGroupItem({ className, ...props }: React.ComponentProps<typeof ToggleGroupPrimitive.Item>) {
  return (
    <ToggleGroupPrimitive.Item
      data-slot="toggle-group-item"
      className={cn(
        "focus-ring-custom inline-flex h-8 min-w-10 cursor-pointer items-center justify-center gap-1.5 rounded px-3 text-sm font-medium text-muted-foreground outline-none transition-colors",
        "hover:bg-surface-raised hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/40",
        "data-[state=on]:bg-zinc-200 data-[state=on]:text-zinc-900",
        "disabled:pointer-events-none disabled:opacity-45 [&_svg]:size-4 [&_svg]:shrink-0",
        className,
      )}
      {...props}
    />
  );
}

export { ToggleGroup, ToggleGroupItem };
