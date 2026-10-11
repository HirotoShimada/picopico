"use client";

import * as React from "react";

import {
  CANVAS_HEIGHT,
  lyricBlockStyle,
  lyricBoxStyle,
  lyricTextStyle,
  SAFE_BOTTOM_PX,
  SAFE_TOP_PX,
} from "@/components/lyrics/lyric-styles";
import type { LyricStyleId, TextStyle } from "@/lib/types";
import { cn } from "@/lib/utils";

import { effectiveLyricSpec } from "./text-style";
import { useElementWidth } from "./use-element-width";

export const SAMPLE_LYRIC = "夜明けの街を走り出す";

/** Design width (canvas px) that a strip sample shows, i.e. a zoomed-in crop of the frame. */
const STRIP_DESIGN_WIDTH = 620;

export interface LyricStyleSampleProps {
  styleId: LyricStyleId;
  textStyle?: TextStyle | null;
  text?: string;
  /** Background gradient colours (e.g. the world preset palette). */
  palette?: readonly string[];
  /** `strip`: zoomed crop for pickers; `frame`: full 9:16 frame honouring the position. */
  variant?: "strip" | "frame";
  showSafeArea?: boolean;
  className?: string;
}

function backgroundFor(palette: readonly string[] | undefined): string {
  const colors = palette && palette.length >= 2 ? palette : ["#3f3f46", "#18181b", "#0a0a0a"];
  const [a, b, c = b] = colors;
  return `linear-gradient(rgba(0,0,0,0.35), rgba(0,0,0,0.35)), linear-gradient(160deg, ${a} 0%, ${b} 55%, ${c} 100%)`;
}

/**
 * Static CSS approximation of a burned-in lyric style (no animation). Karaoke
 * shows the sweep half-way; fade/minimal show the settled state.
 */
export function LyricStyleSample({
  styleId,
  textStyle,
  text = SAMPLE_LYRIC,
  palette,
  variant = "strip",
  showSafeArea = false,
  className,
}: LyricStyleSampleProps) {
  const [ref, width] = useElementWidth<HTMLSpanElement>();
  const spec = effectiveLyricSpec(styleId, textStyle);
  const scale = width > 0 ? (variant === "strip" ? width / STRIP_DESIGN_WIDTH : width / 1080) : 0;
  const box = lyricBoxStyle(spec, scale);

  const content =
    styleId === "karaoke" ? (
      <>
        <span style={{ color: spec.highlightColor }}>{text.slice(0, Math.ceil(text.length / 2))}</span>
        <span>{text.slice(Math.ceil(text.length / 2))}</span>
      </>
    ) : (
      text
    );

  const paragraph =
    scale > 0 ? (
      <span style={{ ...lyricTextStyle(spec, scale), display: "block", margin: 0 }}>
        <span style={box}>{content}</span>
      </span>
    ) : null;

  const pct = (px: number) => `${(px / CANVAS_HEIGHT) * 100}%`;

  return (
    <span
      ref={ref}
      aria-hidden
      className={cn(
        "relative block w-full overflow-hidden rounded-md border border-white/5",
        variant === "strip" ? "aspect-[16/7]" : "aspect-[9/16]",
        className,
      )}
      style={{ backgroundImage: backgroundFor(palette) }}
    >
      {variant === "frame" && showSafeArea ? (
        <>
          <span
            className="absolute inset-x-0 top-0 border-b border-dashed border-white/30 bg-white/[0.04]"
            style={{ height: pct(SAFE_TOP_PX) }}
          />
          <span
            className="absolute inset-x-0 bottom-0 border-t border-dashed border-white/30 bg-white/[0.04]"
            style={{ height: pct(SAFE_BOTTOM_PX) }}
          />
        </>
      ) : null}
      {variant === "strip" ? (
        <span className="absolute inset-0 flex items-center justify-center px-2">{paragraph}</span>
      ) : paragraph ? (
        <span style={lyricBlockStyle(spec, spec.position, scale)}>{paragraph}</span>
      ) : null}
    </span>
  );
}
