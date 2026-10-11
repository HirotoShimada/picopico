"use client";

import { RotateCcw } from "lucide-react";
import * as React from "react";

import { Button } from "@/components/ui/button";
import { ColorInput } from "@/components/ui/color-input";
import { Label } from "@/components/ui/label";
import { Slider } from "@/components/ui/slider";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import type { LyricStyleId, TextPosition, TextStyle } from "@/lib/types";
import { cn } from "@/lib/utils";

import {
  compactTextStyle,
  FONT_SIZE_RANGE,
  OUTLINE_WIDTH_RANGE,
  POSITION_LABELS,
  textStyleDefault,
  type EditableTextStyleKey,
} from "./text-style";

export interface TextStyleControlsProps {
  styleId: LyricStyleId;
  /** Overrides only; unset fields use the lyric style's defaults. */
  value: TextStyle | null;
  onValueChange: (value: TextStyle | null) => void;
  disabled?: boolean;
  /** Prefix for element ids (two instances can coexist on a page). */
  idPrefix: string;
  className?: string;
}

function ResetButton({ visible, onClick, label }: { visible: boolean; onClick: () => void; label: string }) {
  if (!visible) return null;
  return (
    <Button type="button" variant="ghost" size="icon-xs" onClick={onClick} aria-label={`${label}を既定に戻す`} title="既定に戻す">
      <RotateCcw aria-hidden />
    </Button>
  );
}

function ControlRow({
  label,
  htmlFor,
  value,
  overridden,
  onReset,
  hint,
  children,
}: {
  label: string;
  htmlFor?: string;
  value?: React.ReactNode;
  overridden: boolean;
  onReset: () => void;
  hint?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <div className="flex h-7 items-center justify-between gap-2">
        <Label htmlFor={htmlFor} className="text-xs font-normal text-muted-foreground">
          {label}
          {overridden ? <span className="text-2xs text-foreground/80">（変更済み）</span> : null}
        </Label>
        <div className="flex items-center gap-1">
          {value !== undefined ? <span className="font-mono text-xs text-foreground tabular">{value}</span> : null}
          <ResetButton visible={overridden} onClick={onReset} label={label} />
        </div>
      </div>
      {children}
      {hint ? <p className="text-2xs leading-4 text-subtle-foreground">{hint}</p> : null}
    </div>
  );
}

/** Lyric text overrides: size, colours, outline, position and background box opacity. */
export function TextStyleControls({ styleId, value, onValueChange, disabled, idPrefix, className }: TextStyleControlsProps) {
  const current = value ?? {};

  function set<K extends EditableTextStyleKey>(key: K, next: TextStyle[K] | null) {
    onValueChange(compactTextStyle({ ...current, [key]: next }));
  }

  const fontSize = current.font_size ?? (textStyleDefault(styleId, "font_size") as number);
  const outlineWidth = current.outline_width ?? (textStyleDefault(styleId, "outline_width") as number);
  const bgOpacity = current.bg_opacity ?? (textStyleDefault(styleId, "bg_opacity") as number);
  const defaultPosition = textStyleDefault(styleId, "position") as TextPosition;
  const anyOverride = compactTextStyle(value) !== null;

  return (
    <div className={cn("flex flex-col gap-4", className)}>
      <div className="grid grid-cols-1 gap-x-6 gap-y-4 md:grid-cols-2">
        <ControlRow
          label="文字サイズ"
          value={fontSize}
          overridden={current.font_size != null}
          onReset={() => set("font_size", null)}
        >
          <Slider
            min={FONT_SIZE_RANGE.min}
            max={FONT_SIZE_RANGE.max}
            step={2}
            value={[fontSize]}
            onValueChange={([next]) => set("font_size", next)}
            disabled={disabled}
            aria-label="文字サイズ"
          />
        </ControlRow>

        <ControlRow label="表示位置" overridden={current.position != null} onReset={() => set("position", null)}>
          <ToggleGroup
            type="single"
            value={current.position ?? "default"}
            onValueChange={(next) => next && set("position", next === "default" ? null : (next as TextPosition))}
            disabled={disabled}
            aria-label="表示位置"
          >
            <ToggleGroupItem value="default" className="h-7 px-2.5 text-xs">
              既定（{POSITION_LABELS[defaultPosition]}）
            </ToggleGroupItem>
            {(["top", "center", "bottom"] as const).map((position) => (
              <ToggleGroupItem key={position} value={position} className="h-7 px-2.5 text-xs">
                {POSITION_LABELS[position]}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        </ControlRow>

        <ControlRow
          label="文字色"
          htmlFor={`${idPrefix}-primary`}
          overridden={current.primary_color != null}
          onReset={() => set("primary_color", null)}
        >
          <ColorInput
            id={`${idPrefix}-primary`}
            value={current.primary_color ?? null}
            placeholderColor={textStyleDefault(styleId, "primary_color") as string}
            onValueChange={(next) => set("primary_color", next)}
            disabled={disabled}
            aria-label="文字色"
          />
        </ControlRow>

        <ControlRow
          label="ハイライト色"
          htmlFor={`${idPrefix}-highlight`}
          overridden={current.highlight_color != null}
          onReset={() => set("highlight_color", null)}
          hint="カラオケスタイルで歌に合わせて塗られる色です。"
        >
          <ColorInput
            id={`${idPrefix}-highlight`}
            value={current.highlight_color ?? null}
            placeholderColor={textStyleDefault(styleId, "highlight_color") as string}
            onValueChange={(next) => set("highlight_color", next)}
            disabled={disabled}
            aria-label="ハイライト色"
          />
        </ControlRow>

        <ControlRow
          label="縁取りの色"
          htmlFor={`${idPrefix}-outline`}
          overridden={current.outline_color != null}
          onReset={() => set("outline_color", null)}
        >
          <ColorInput
            id={`${idPrefix}-outline`}
            value={current.outline_color ?? null}
            placeholderColor={textStyleDefault(styleId, "outline_color") as string}
            onValueChange={(next) => set("outline_color", next)}
            disabled={disabled}
            aria-label="縁取りの色"
          />
        </ControlRow>

        <ControlRow
          label="縁取りの太さ"
          value={`${outlineWidth}px`}
          overridden={current.outline_width != null}
          onReset={() => set("outline_width", null)}
        >
          <Slider
            min={OUTLINE_WIDTH_RANGE.min}
            max={OUTLINE_WIDTH_RANGE.max}
            step={0.5}
            value={[outlineWidth]}
            onValueChange={([next]) => set("outline_width", next)}
            disabled={disabled}
            aria-label="縁取りの太さ"
          />
        </ControlRow>

        <ControlRow
          label="背景の濃さ"
          value={`${Math.round(bgOpacity * 100)}%`}
          overridden={current.bg_opacity != null}
          onReset={() => set("bg_opacity", null)}
          hint="歌詞の後ろに敷く半透明の帯。0% で帯なし。"
        >
          <Slider
            min={0}
            max={1}
            step={0.05}
            value={[bgOpacity]}
            onValueChange={([next]) => set("bg_opacity", Number(next.toFixed(2)))}
            disabled={disabled}
            aria-label="背景の濃さ"
          />
        </ControlRow>
      </div>
      {anyOverride ? (
        <Button type="button" variant="ghost" size="xs" className="w-fit" onClick={() => onValueChange(null)} disabled={disabled}>
          <RotateCcw aria-hidden />
          文字の設定をすべて既定に戻す
        </Button>
      ) : null}
    </div>
  );
}
