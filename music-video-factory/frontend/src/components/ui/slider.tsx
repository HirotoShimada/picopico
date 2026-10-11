"use client";

import { Slider as SliderPrimitive } from "radix-ui";
import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Single value or range slider (pass two values for a range). `aria-label` /
 * `aria-labelledby` name the thumbs (the elements with role="slider"), not the root.
 */
function Slider({
  className,
  value,
  defaultValue,
  min = 0,
  max = 100,
  "aria-label": ariaLabel,
  "aria-labelledby": ariaLabelledBy,
  ...props
}: React.ComponentProps<typeof SliderPrimitive.Root>) {
  const thumbs = Array.isArray(value) ? value : Array.isArray(defaultValue) ? defaultValue : [min];
  return (
    <SliderPrimitive.Root
      data-slot="slider"
      value={value}
      defaultValue={defaultValue}
      min={min}
      max={max}
      className={cn(
        "relative flex w-full touch-none items-center select-none data-[disabled]:opacity-50",
        "data-[orientation=vertical]:h-full data-[orientation=vertical]:min-h-40 data-[orientation=vertical]:w-auto data-[orientation=vertical]:flex-col",
        className,
      )}
      {...props}
    >
      <SliderPrimitive.Track
        data-slot="slider-track"
        className="relative grow overflow-hidden rounded-full bg-zinc-800 data-[orientation=horizontal]:h-1.5 data-[orientation=horizontal]:w-full data-[orientation=vertical]:h-full data-[orientation=vertical]:w-1.5"
      >
        <SliderPrimitive.Range
          data-slot="slider-range"
          className="absolute bg-zinc-300 data-[orientation=horizontal]:h-full data-[orientation=vertical]:w-full"
        />
      </SliderPrimitive.Track>
      {thumbs.map((_, index) => (
        <SliderPrimitive.Thumb
          key={index}
          data-slot="slider-thumb"
          aria-label={ariaLabel && thumbs.length > 1 ? `${ariaLabel}（${index === 0 ? "開始" : "終了"}）` : ariaLabel}
          aria-labelledby={ariaLabelledBy}
          className="focus-ring-custom block size-4 cursor-grab rounded-full border-2 border-zinc-300 bg-background shadow transition-colors outline-none hover:border-white focus-visible:ring-2 focus-visible:ring-ring/40 active:cursor-grabbing disabled:pointer-events-none"
        />
      ))}
    </SliderPrimitive.Root>
  );
}

export { Slider };
