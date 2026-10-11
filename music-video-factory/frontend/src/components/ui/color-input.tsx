"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

import { Input } from "./input";

const HEX_RE = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;

/** `#abc` → `#aabbcc` (upper case); `null` when not a valid hex colour. */
export function normalizeHex(value: string): string | null {
  const trimmed = value.trim();
  const withHash = trimmed.startsWith("#") ? trimmed : `#${trimmed}`;
  if (!HEX_RE.test(withHash)) return null;
  const digits = withHash.slice(1);
  const full = digits.length === 3 ? digits.replace(/./g, (c) => c + c) : digits;
  return `#${full.toUpperCase()}`;
}

export interface ColorInputProps {
  id?: string;
  /** Hex colour override, or `null` to use `placeholderColor`. */
  value: string | null;
  onValueChange: (value: string | null) => void;
  /** Effective colour shown while `value` is `null` (e.g. the style default). */
  placeholderColor: string;
  disabled?: boolean;
  className?: string;
  "aria-label"?: string;
}

/** Native colour swatch + hex text field. Clearing the text resets to the default. */
function ColorInput({ id, value, onValueChange, placeholderColor, disabled, className, ...aria }: ColorInputProps) {
  const effective = normalizeHex(value ?? "") ?? normalizeHex(placeholderColor) ?? "#FFFFFF";
  const [text, setText] = React.useState(value ?? "");
  const [lastValue, setLastValue] = React.useState(value);

  if (value !== lastValue) {
    setLastValue(value);
    setText(value ?? "");
  }

  function commitText(raw: string) {
    setText(raw);
    if (raw.trim() === "") {
      setLastValue(null);
      onValueChange(null);
      return;
    }
    const hex = normalizeHex(raw);
    if (hex) {
      setLastValue(hex);
      onValueChange(hex);
    }
  }

  return (
    <div className={cn("flex items-center gap-2", className)}>
      <input
        type="color"
        value={effective.toLowerCase()}
        disabled={disabled}
        onChange={(event) => commitText(event.target.value.toUpperCase())}
        aria-label={aria["aria-label"] ? `${aria["aria-label"]}（カラーピッカー）` : "カラーピッカー"}
        className="focus-ring-custom size-9 shrink-0 cursor-pointer rounded-md border border-border bg-surface p-1 outline-none focus-visible:ring-2 focus-visible:ring-ring/40 disabled:cursor-not-allowed disabled:opacity-50 [&::-webkit-color-swatch]:rounded-sm [&::-webkit-color-swatch]:border-0 [&::-webkit-color-swatch-wrapper]:p-0"
      />
      <Input
        id={id}
        value={text}
        disabled={disabled}
        placeholder={placeholderColor.toUpperCase()}
        maxLength={7}
        spellCheck={false}
        autoComplete="off"
        aria-invalid={text.trim() !== "" && !normalizeHex(text) ? true : undefined}
        onChange={(event) => commitText(event.target.value)}
        onBlur={() => setText(value ?? "")}
        className="w-28 font-mono text-xs uppercase"
        {...aria}
      />
    </div>
  );
}

export { ColorInput };
