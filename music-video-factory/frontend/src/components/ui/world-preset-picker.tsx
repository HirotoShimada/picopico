"use client";

import { Check } from "lucide-react";
import { RadioGroup } from "radix-ui";
import * as React from "react";

import type { WorldPresetInfo } from "@/lib/types";
import { cn } from "@/lib/utils";

export interface WorldPresetPickerProps {
  presets: readonly WorldPresetInfo[];
  value: string;
  onValueChange: (presetId: string) => void;
  disabled?: boolean;
  className?: string;
  /** Accessible name of the radio group. */
  "aria-label"?: string;
  "aria-labelledby"?: string;
}

/** Radio-card grid of world presets (palette swatches + Japanese description). */
export function WorldPresetPicker({ presets, value, onValueChange, disabled, className, ...aria }: WorldPresetPickerProps) {
  return (
    <RadioGroup.Root
      value={value}
      onValueChange={onValueChange}
      disabled={disabled}
      className={cn("grid grid-cols-1 gap-2.5 sm:grid-cols-2 xl:grid-cols-3", className)}
      {...aria}
    >
      {presets.map((preset) => {
        const selected = preset.id === value;
        return (
          <RadioGroup.Item
            key={preset.id}
            value={preset.id}
            className={cn(
              "focus-ring-custom group relative flex cursor-pointer flex-col gap-2 rounded-lg border p-3 text-left outline-none transition-colors",
              "focus-visible:ring-2 focus-visible:ring-ring/40 disabled:cursor-not-allowed disabled:opacity-50",
              selected ? "border-zinc-300 bg-surface-hover" : "border-border bg-surface hover:border-border-strong hover:bg-surface-raised",
            )}
          >
            <div className="flex items-center justify-between gap-2">
              <div className="flex gap-1" aria-hidden>
                {(preset.palette.length > 0 ? preset.palette.slice(0, 3) : ["#3f3f46", "#52525b", "#71717a"]).map((color, i) => (
                  <span key={`${color}-${i}`} className="h-4 w-6 rounded-sm border border-white/10" style={{ backgroundColor: color }} />
                ))}
              </div>
              <span
                className={cn(
                  "flex size-4 items-center justify-center rounded-full border",
                  selected ? "border-zinc-200 bg-zinc-200 text-zinc-900" : "border-zinc-600",
                )}
                aria-hidden
              >
                <RadioGroup.Indicator>
                  <Check className="size-3" strokeWidth={3} />
                </RadioGroup.Indicator>
              </span>
            </div>
            <div className="flex items-baseline gap-2">
              <span className="text-sm font-medium text-foreground">{preset.name_ja}</span>
              {preset.name_en ? <span className="text-2xs text-subtle-foreground">{preset.name_en}</span> : null}
            </div>
            {preset.description_ja ? <p className="text-xs leading-5 text-muted-foreground">{preset.description_ja}</p> : null}
          </RadioGroup.Item>
        );
      })}
    </RadioGroup.Root>
  );
}
