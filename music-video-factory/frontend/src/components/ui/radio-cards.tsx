"use client";

import { Check } from "lucide-react";
import { RadioGroup } from "radix-ui";
import * as React from "react";

import { cn } from "@/lib/utils";

export interface RadioCardOption<T extends string> {
  value: T;
  title: React.ReactNode;
  description?: React.ReactNode;
  /** Badges next to the title (status, price …). */
  meta?: React.ReactNode;
  /** Extra content under the description (e.g. a visual sample). */
  media?: React.ReactNode;
  disabled?: boolean;
  /** Why the option is unavailable (shown instead of hiding the option). */
  disabledReason?: React.ReactNode;
}

export interface RadioCardsProps<T extends string> {
  options: readonly RadioCardOption<T>[];
  value: T;
  onValueChange: (value: T) => void;
  disabled?: boolean;
  className?: string;
  /** Put the visual `media` above the text instead of below it. */
  mediaFirst?: boolean;
  "aria-label"?: string;
  "aria-labelledby"?: string;
}

/**
 * Radio group rendered as option cards (title, description, badges, optional
 * media). Selection uses a neutral highlight; blue stays reserved for primary
 * actions. Disabled options remain visible with their reason.
 */
export function RadioCards<T extends string>({
  options,
  value,
  onValueChange,
  disabled,
  className,
  mediaFirst = false,
  ...aria
}: RadioCardsProps<T>) {
  return (
    <RadioGroup.Root
      value={value}
      onValueChange={(next) => onValueChange(next as T)}
      disabled={disabled}
      className={cn("grid grid-cols-1 gap-2.5 sm:grid-cols-2", className)}
      {...aria}
    >
      {options.map((option) => {
        const selected = option.value === value;
        return (
          <RadioGroup.Item
            key={option.value}
            value={option.value}
            disabled={option.disabled}
            className={cn(
              "focus-ring-custom group relative flex min-w-0 cursor-pointer flex-col gap-2 rounded-lg border p-3 text-left outline-none transition-colors",
              "focus-visible:ring-2 focus-visible:ring-ring/40 disabled:cursor-not-allowed",
              selected
                ? "border-zinc-300 bg-surface-hover"
                : "border-border bg-surface hover:border-border-strong hover:bg-surface-raised disabled:hover:border-border disabled:hover:bg-surface",
            )}
          >
            {mediaFirst && option.media ? option.media : null}
            <span className="flex items-start justify-between gap-2">
              <span className={cn("flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1", option.disabled && "opacity-60")}>
                <span className="text-sm font-medium text-foreground">{option.title}</span>
                {option.meta}
              </span>
              <span
                className={cn(
                  "mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-full border",
                  selected ? "border-zinc-200 bg-zinc-200 text-zinc-900" : "border-zinc-600",
                )}
                aria-hidden
              >
                <RadioGroup.Indicator>
                  <Check className="size-3" strokeWidth={3} />
                </RadioGroup.Indicator>
              </span>
            </span>
            {option.description ? (
              <span className={cn("block text-xs leading-5 text-muted-foreground", option.disabled && "opacity-60")}>{option.description}</span>
            ) : null}
            {!mediaFirst && option.media ? option.media : null}
            {option.disabled && option.disabledReason ? (
              <span className="block text-xs leading-5 text-subtle-foreground">{option.disabledReason}</span>
            ) : null}
          </RadioGroup.Item>
        );
      })}
    </RadioGroup.Root>
  );
}
