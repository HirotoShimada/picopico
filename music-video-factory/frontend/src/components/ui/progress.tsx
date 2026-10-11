"use client";

import { Progress as ProgressPrimitive } from "radix-ui";
import * as React from "react";

import { cn } from "@/lib/utils";

const INDICATOR_TONES = {
  primary: "bg-primary",
  success: "bg-success",
  danger: "bg-danger",
  neutral: "bg-zinc-400",
} as const;

export interface ProgressProps extends Omit<React.ComponentProps<typeof ProgressPrimitive.Root>, "value"> {
  /** 0..100; `null` renders an indeterminate (striped, static) bar. */
  value: number | null;
  tone?: keyof typeof INDICATOR_TONES;
  size?: "sm" | "md";
}

function Progress({ className, value, tone = "primary", size = "md", ...props }: ProgressProps) {
  const clamped = value === null ? null : Math.min(100, Math.max(0, value));
  return (
    <ProgressPrimitive.Root
      data-slot="progress"
      value={clamped}
      className={cn(
        "relative w-full overflow-hidden rounded-full bg-zinc-800",
        size === "sm" ? "h-1" : "h-1.5",
        className,
      )}
      {...props}
    >
      <ProgressPrimitive.Indicator
        data-slot="progress-indicator"
        className={cn(
          "h-full rounded-full transition-[width] duration-300",
          INDICATOR_TONES[tone],
          clamped === null &&
            "w-full bg-[repeating-linear-gradient(-45deg,var(--color-border-strong)_0_6px,transparent_6px_12px)]",
        )}
        style={clamped === null ? undefined : { width: `${clamped}%` }}
      />
    </ProgressPrimitive.Root>
  );
}

export { Progress };
