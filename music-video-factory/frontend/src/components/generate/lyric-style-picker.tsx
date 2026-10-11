"use client";

import * as React from "react";

import { isLyricStyleId, LYRIC_STYLES_FALLBACK } from "@/components/lyrics/lyric-styles";
import { RadioCards, type RadioCardOption } from "@/components/ui/radio-cards";
import { useCapabilities } from "@/hooks/use-capabilities";
import type { LyricStyleChoice, LyricStyleId, LyricStyleInfo, TextStyle } from "@/lib/types";
import { cn } from "@/lib/utils";

import { LyricStyleSample } from "./lyric-style-sample";
import { AUTO_LYRIC_STYLE_LABEL, lyricStyleChoiceName } from "./text-style";

/** Lyric styles from capabilities (only ids the frontend can preview), else the built-in list. */
export function useLyricStyles(): readonly LyricStyleInfo[] {
  const { data } = useCapabilities();
  return React.useMemo(() => {
    const fromServer = data?.lyric_styles.filter((style) => isLyricStyleId(style.id)) ?? [];
    return fromServer.length > 0 ? fromServer : LYRIC_STYLES_FALLBACK;
  }, [data]);
}

export interface LyricStylePickerProps<T extends LyricStyleChoice> {
  value: T;
  onValueChange: (value: T) => void;
  /**
   * Batch settings only (`T` must allow `"auto"`): the styles `auto` rotates per video
   * (the world preset's `lyric_styles`). When given, an 「自動」 card is offered first.
   */
  autoStyles?: "auto" extends T ? readonly LyricStyleId[] : never;
  /** Overrides applied to the selected style's sample. */
  textStyle?: TextStyle | null;
  palette?: readonly string[];
  disabled?: boolean;
  className?: string;
}

/** The five lyric styles (plus 「自動」 for a batch) as cards with a small visual sample each. */
export function LyricStylePicker<T extends LyricStyleChoice>({
  value,
  onValueChange,
  autoStyles,
  textStyle,
  palette,
  disabled,
  className,
}: LyricStylePickerProps<T>) {
  const styles = useLyricStyles();
  const options: RadioCardOption<LyricStyleChoice>[] = styles.map((style) => {
    const id = style.id as LyricStyleId;
    return {
      value: id,
      title: style.name_ja,
      description: style.description,
      media: <LyricStyleSample styleId={id} textStyle={id === value ? textStyle : null} palette={palette} />,
    };
  });
  if (autoStyles && autoStyles.length > 0) {
    const names = autoStyles.map((id) => lyricStyleChoiceName(id, styles));
    options.unshift({
      value: "auto",
      title: AUTO_LYRIC_STYLE_LABEL,
      description: `世界観のおすすめ（${names.join("・")}）を動画ごとに順番に使います。`,
      media: <LyricStyleSample styleId={autoStyles[0]} textStyle={value === "auto" ? textStyle : null} palette={palette} />,
    });
  }
  return (
    <RadioCards<LyricStyleChoice>
      options={options}
      value={value}
      onValueChange={(next) => onValueChange(next as T)}
      disabled={disabled}
      mediaFirst
      aria-label="歌詞スタイル"
      className={cn("grid-cols-2 sm:grid-cols-3", options.length === 5 && "2xl:grid-cols-5", className)}
    />
  );
}
