"use client";

import * as React from "react";

import { isLyricStyleId, LYRIC_STYLES_FALLBACK } from "@/components/lyrics/lyric-styles";
import { RadioCards, type RadioCardOption } from "@/components/ui/radio-cards";
import { useCapabilities } from "@/hooks/use-capabilities";
import type { LyricStyleId, LyricStyleInfo, TextStyle } from "@/lib/types";
import { cn } from "@/lib/utils";

import { LyricStyleSample } from "./lyric-style-sample";

/** Lyric styles from capabilities (only ids the frontend can preview), else the built-in list. */
export function useLyricStyles(): readonly LyricStyleInfo[] {
  const { data } = useCapabilities();
  return React.useMemo(() => {
    const fromServer = data?.lyric_styles.filter((style) => isLyricStyleId(style.id)) ?? [];
    return fromServer.length > 0 ? fromServer : LYRIC_STYLES_FALLBACK;
  }, [data]);
}

export interface LyricStylePickerProps {
  value: LyricStyleId;
  onValueChange: (value: LyricStyleId) => void;
  /** Overrides applied to the selected style's sample. */
  textStyle?: TextStyle | null;
  palette?: readonly string[];
  disabled?: boolean;
  className?: string;
}

/** Five lyric styles as cards with a small visual sample each. */
export function LyricStylePicker({ value, onValueChange, textStyle, palette, disabled, className }: LyricStylePickerProps) {
  const styles = useLyricStyles();
  const options: RadioCardOption<LyricStyleId>[] = styles.map((style) => {
    const id = style.id as LyricStyleId;
    return {
      value: id,
      title: style.name_ja,
      description: style.description,
      media: <LyricStyleSample styleId={id} textStyle={id === value ? textStyle : null} palette={palette} />,
    };
  });
  return (
    <RadioCards
      options={options}
      value={value}
      onValueChange={onValueChange}
      disabled={disabled}
      mediaFirst
      aria-label="歌詞スタイル"
      className={cn("grid-cols-2 sm:grid-cols-3 2xl:grid-cols-5", className)}
    />
  );
}
