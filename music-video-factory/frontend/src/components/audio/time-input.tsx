"use client";

import * as React from "react";

import { fieldControlClasses } from "@/components/ui/input";
import { formatTimePrecise } from "@/lib/format";
import { cn } from "@/lib/utils";

import { parseTimeInput, roundTime } from "./timeline";

export interface TimeInputProps {
  value: number | null;
  /** Called with the parsed value (or `null` when cleared and `allowEmpty`). */
  onCommit: (value: number | null) => void;
  min?: number;
  max?: number;
  /** Arrow-key step in seconds (Shift = ×10). */
  step?: number;
  allowEmpty?: boolean;
  /** Shown when the value is empty (e.g. a derived value). */
  placeholder?: string;
  invalid?: boolean;
  disabled?: boolean;
  className?: string;
  "aria-label": string;
  id?: string;
}

/**
 * Time field showing `m:ss.cc`; accepts `75.5`, `1:15.5` or `1:15`. Commits on
 * Enter/blur, Escape reverts, ↑/↓ nudge by `step` (Shift: ×10).
 */
export function TimeInput({
  value,
  onCommit,
  min = 0,
  max,
  step = 0.1,
  allowEmpty = false,
  placeholder,
  invalid = false,
  disabled = false,
  className,
  id,
  ...aria
}: TimeInputProps) {
  // `null` while not editing: the field shows the formatted prop value.
  const [draft, setDraft] = React.useState<string | null>(null);
  const [error, setError] = React.useState(false);
  // Enter/Escape already handled the value; the synchronous blur that follows must not commit again.
  const skipBlur = React.useRef(false);

  const display = value === null ? "" : formatTimePrecise(value);

  function clamp(seconds: number): number {
    let result = Math.max(min, seconds);
    if (max !== undefined) result = Math.min(max, result);
    return roundTime(result);
  }

  function commit(text: string): boolean {
    const parsed = parseTimeInput(text);
    if (parsed === null) {
      if (!allowEmpty) return false;
      if (value !== null) onCommit(null);
      return true;
    }
    if (Number.isNaN(parsed)) return false;
    const next = clamp(parsed);
    if (value === null || Math.abs(next - value) > 0.0005) onCommit(next);
    return true;
  }

  return (
    <input
      id={id}
      type="text"
      inputMode="decimal"
      autoComplete="off"
      spellCheck={false}
      disabled={disabled}
      aria-label={aria["aria-label"]}
      aria-invalid={invalid || error || undefined}
      placeholder={placeholder}
      value={draft ?? display}
      onFocus={(event) => {
        setDraft(display);
        event.currentTarget.select();
      }}
      onChange={(event) => {
        setDraft(event.target.value);
        setError(false);
      }}
      onBlur={() => {
        if (skipBlur.current) {
          skipBlur.current = false;
          return;
        }
        if (draft !== null && draft !== display && !commit(draft)) {
          setError(true);
          return;
        }
        setDraft(null);
        setError(false);
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          if (draft !== null && draft !== display && !commit(draft)) {
            setError(true);
            return;
          }
          setDraft(null);
          setError(false);
          skipBlur.current = true;
          event.currentTarget.blur();
        } else if (event.key === "Escape") {
          event.preventDefault();
          setDraft(null);
          setError(false);
          skipBlur.current = true;
          event.currentTarget.blur();
        } else if (event.key === "ArrowUp" || event.key === "ArrowDown") {
          event.preventDefault();
          const base = (draft !== null ? parseTimeInput(draft) : value) ?? value ?? 0;
          if (Number.isNaN(base)) return;
          const delta = (event.key === "ArrowUp" ? 1 : -1) * step * (event.shiftKey ? 10 : 1);
          const next = clamp(base + delta);
          onCommit(next);
          setDraft(formatTimePrecise(next));
          setError(false);
        }
      }}
      className={cn(fieldControlClasses, "h-8 w-[5.75rem] px-2 font-mono text-xs tabular", className)}
    />
  );
}
