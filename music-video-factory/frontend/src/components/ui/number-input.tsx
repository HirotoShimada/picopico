"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

import { Input } from "./input";

export interface NumberInputProps
  extends Omit<React.ComponentProps<"input">, "value" | "defaultValue" | "onChange" | "type" | "min" | "max" | "step" | "prefix"> {
  /** `null` = empty. */
  value: number | null;
  onValueChange: (value: number | null) => void;
  min?: number;
  max?: number;
  step?: number;
  /** Round committed values to this many decimals. */
  decimals?: number;
  /** Unit or currency shown inside the field (e.g. `秒`, `$`). */
  prefix?: React.ReactNode;
  suffix?: React.ReactNode;
  inputClassName?: string;
}

function format(value: number | null, decimals: number | undefined): string {
  if (value === null || !Number.isFinite(value)) return "";
  return decimals === undefined ? String(value) : String(Number(value.toFixed(decimals)));
}

/**
 * Numeric field that lets the user type freely (including an empty field) and
 * reports parsed values; out-of-range numbers are reported as typed so the
 * caller can show a validation message, and are clamped only on blur.
 */
function NumberInput({
  value,
  onValueChange,
  min,
  max,
  step,
  decimals,
  prefix,
  suffix,
  className,
  inputClassName,
  onBlur,
  ...props
}: NumberInputProps) {
  const [text, setText] = React.useState(() => format(value, decimals));
  const [lastValue, setLastValue] = React.useState(value);

  // Adopt values changed from outside (presets, resets) without clobbering typing.
  if (value !== lastValue) {
    setLastValue(value);
    const parsed = text.trim() === "" ? null : Number(text);
    if (parsed !== value) setText(format(value, decimals));
  }

  function commit(raw: string) {
    setText(raw);
    const trimmed = raw.trim();
    if (trimmed === "") {
      setLastValue(null);
      onValueChange(null);
      return;
    }
    const parsed = Number(trimmed);
    if (!Number.isFinite(parsed)) return;
    setLastValue(parsed);
    onValueChange(parsed);
  }

  function handleBlur(event: React.FocusEvent<HTMLInputElement>) {
    onBlur?.(event);
    if (value === null) return;
    let next = value;
    if (min !== undefined) next = Math.max(min, next);
    if (max !== undefined) next = Math.min(max, next);
    if (decimals !== undefined) next = Number(next.toFixed(decimals));
    setText(format(next, decimals));
    if (next !== value) {
      setLastValue(next);
      onValueChange(next);
    }
  }

  return (
    <div className={cn("relative flex items-center", className)}>
      {prefix ? (
        <span className="pointer-events-none absolute left-3 text-sm text-subtle-foreground" aria-hidden>
          {prefix}
        </span>
      ) : null}
      <Input
        type="number"
        inputMode="decimal"
        value={text}
        min={min}
        max={max}
        step={step}
        onChange={(event) => commit(event.target.value)}
        onBlur={handleBlur}
        className={cn(prefix ? "pl-7" : undefined, suffix ? "pr-10" : undefined, inputClassName)}
        {...props}
      />
      {suffix ? (
        <span className="pointer-events-none absolute right-3 text-xs text-subtle-foreground" aria-hidden>
          {suffix}
        </span>
      ) : null}
    </div>
  );
}

export { NumberInput };
