"use client";

import * as React from "react";

import { useAudioTime } from "@/components/audio/audio-player";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { useCapabilities } from "@/hooks/use-capabilities";
import { formatTimePrecise } from "@/lib/format";
import type { LyricLine, LyricStyleId, TextPosition } from "@/lib/types";
import { cn } from "@/lib/utils";

import {
  CANVAS_HEIGHT,
  isLyricStyleId,
  LYRIC_STYLE_SPECS,
  LYRIC_STYLES_FALLBACK,
  lyricBlockStyle,
  lyricBoxStyle,
  lyricFade,
  lyricTextStyle,
  SAFE_BOTTOM_PX,
  SAFE_TOP_PX,
  type LyricStyleSpec,
} from "./lyric-styles";
import { activeLineIndex, effectiveEnd, karaokeSpans } from "./timing";

const POSITION_OPTIONS: { value: "default" | TextPosition; label: string }[] = [
  { value: "default", label: "既定" },
  { value: "top", label: "上" },
  { value: "center", label: "中央" },
  { value: "bottom", label: "下" },
];

function KaraokeText({ line, end, time, spec }: { line: LyricLine; end: number; time: number; spec: LyricStyleSpec }) {
  const spans = karaokeSpans(line, end);
  if (spans.length === 0) return <>{line.text}</>;
  const parts: React.ReactNode[] = [];
  let cursor = 0;
  spans.forEach((span, index) => {
    if (span.from > cursor) parts.push(<span key={`g${index}`}>{line.text.slice(cursor, span.from)}</span>);
    const progress = span.end > span.start ? Math.min(1, Math.max(0, (time - span.start) / (span.end - span.start))) : time >= span.end ? 1 : 0;
    const text = line.text.slice(span.from, span.to);
    if (progress <= 0 || progress >= 1) {
      parts.push(
        <span key={`w${index}`} style={progress >= 1 ? { color: spec.highlightColor } : undefined}>
          {text}
        </span>,
      );
    } else {
      // Partially sung: a highlight-coloured copy clipped from the right, with the same outline.
      parts.push(
        <span key={`w${index}`} className="relative inline-block">
          {text}
          <span
            aria-hidden
            className="absolute inset-0"
            style={{ color: spec.highlightColor, clipPath: `inset(-20% ${(1 - progress) * 100}% -20% -20%)` }}
          >
            {text}
          </span>
        </span>,
      );
    }
    cursor = span.to;
  });
  if (cursor < line.text.length) parts.push(<span key="tail">{line.text.slice(cursor)}</span>);
  return <>{parts}</>;
}

interface FrameProps {
  lines: readonly LyricLine[];
  duration: number | null;
  styleId: LyricStyleId;
  position: TextPosition;
  showSafeArea: boolean;
  backgroundUrl: string | null;
}

/** The 9:16 frame; re-renders every animation frame while playing (kept small on purpose). */
function PreviewFrame({ lines, duration, styleId, position, showSafeArea, backgroundUrl }: FrameProps) {
  const time = useAudioTime();
  const frameRef = React.useRef<HTMLDivElement>(null);
  const [width, setWidth] = React.useState(0);

  React.useEffect(() => {
    const element = frameRef.current;
    if (!element) return;
    const observer = new ResizeObserver((entries) => setWidth(entries[0]?.contentRect.width ?? 0));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const scale = width / 1080;
  const spec = LYRIC_STYLE_SPECS[styleId];
  const index = activeLineIndex(lines, time, duration);
  const line = index >= 0 ? lines[index] : null;
  const end = index >= 0 ? effectiveEnd(lines, index, duration) : null;
  const fade = line && line.start !== null && end !== null ? lyricFade(spec, time, line.start, end) : { opacity: 1, rise: 0 };
  const box = lyricBoxStyle(spec, scale);
  const pct = (px: number) => `${(px / CANVAS_HEIGHT) * 100}%`;

  return (
    <div
      ref={frameRef}
      className="relative aspect-[9/16] w-full overflow-hidden rounded-md border border-border bg-black"
      role="img"
      aria-label={line ? `プレビュー: ${line.text}` : "プレビュー（この位置に歌詞はありません）"}
    >
      {backgroundUrl ? (
        // eslint-disable-next-line @next/next/no-img-element -- signed, short-lived backend URL
        <img src={backgroundUrl} alt="" className="absolute inset-0 size-full object-cover opacity-70" />
      ) : (
        <div className="absolute inset-0 bg-[radial-gradient(120%_80%_at_50%_20%,#27272a_0%,#0a0a0a_70%)]" />
      )}

      {showSafeArea ? (
        <>
          <div
            className="absolute inset-x-0 top-0 border-b border-dashed border-white/40 bg-[repeating-linear-gradient(-45deg,rgba(255,255,255,0.07)_0_6px,transparent_6px_12px)]"
            style={{ height: pct(SAFE_TOP_PX) }}
          >
            <span className="absolute bottom-1 left-1.5 text-[10px] leading-3 text-white/70">上部 14%：プロフィール表示</span>
          </div>
          <div
            className="absolute inset-x-0 bottom-0 border-t border-dashed border-white/40 bg-[repeating-linear-gradient(-45deg,rgba(255,255,255,0.07)_0_6px,transparent_6px_12px)]"
            style={{ height: pct(SAFE_BOTTOM_PX) }}
          >
            <span className="absolute top-1 left-1.5 text-[10px] leading-3 text-white/70">下部 約20%：返信欄</span>
          </div>
        </>
      ) : null}

      {line && width > 0 ? (
        <div style={{ ...lyricBlockStyle(spec, position, scale), opacity: fade.opacity }}>
          <p style={{ ...lyricTextStyle(spec, scale), transform: fade.rise ? `translateY(${fade.rise * scale}px)` : undefined }}>
            <span style={box}>
              {styleId === "karaoke" && end !== null ? <KaraokeText line={line} end={end} time={time} spec={spec} /> : line.text}
            </span>
          </p>
        </div>
      ) : null}

      <div className="absolute top-1.5 right-1.5 rounded bg-black/60 px-1.5 py-0.5 font-mono text-[10px] text-white/80 tabular">
        {formatTimePrecise(time)}
      </div>
    </div>
  );
}

export interface LyricPreviewProps {
  lines: readonly LyricLine[];
  duration: number | null;
  /** Optional background image (e.g. a ready material's thumbnail). */
  backgroundUrl?: string | null;
  className?: string;
}

/** 9:16 preview of the current lyric line with style approximations and IG safe-area guides. */
export function LyricPreview({ lines, duration, backgroundUrl = null, className }: LyricPreviewProps) {
  const { data: capabilities } = useCapabilities();
  const [styleId, setStyleId] = React.useState<LyricStyleId>("simple");
  const [position, setPosition] = React.useState<"default" | TextPosition>("default");
  const [showSafeArea, setShowSafeArea] = React.useState(true);
  const [useBackground, setUseBackground] = React.useState(true);

  const styles = (capabilities?.lyric_styles.length ? capabilities.lyric_styles : LYRIC_STYLES_FALLBACK).filter((style) =>
    isLyricStyleId(style.id),
  );
  const current = styles.find((style) => style.id === styleId);
  const effectivePosition = position === "default" ? LYRIC_STYLE_SPECS[styleId].position : position;

  return (
    <div className={cn("flex flex-col gap-3", className)}>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="preview-style" className="text-xs font-normal text-muted-foreground">
          歌詞スタイル
        </Label>
        <Select value={styleId} onValueChange={(value) => isLyricStyleId(value) && setStyleId(value)}>
          <SelectTrigger id="preview-style" size="sm">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {styles.map((style) => (
              <SelectItem key={style.id} value={style.id} description={style.description}>
                {style.name_ja}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {current ? <p className="text-2xs leading-4 text-subtle-foreground">{current.description}</p> : null}
      </div>

      <PreviewFrame
        lines={lines}
        duration={duration}
        styleId={styleId}
        position={effectivePosition}
        showSafeArea={showSafeArea}
        backgroundUrl={useBackground ? backgroundUrl : null}
      />

      <div className="flex flex-col gap-2.5">
        <div className="flex items-center justify-between gap-2">
          <span className="text-xs text-muted-foreground">表示位置</span>
          <ToggleGroup
            type="single"
            value={position}
            onValueChange={(value) => value && setPosition(value as "default" | TextPosition)}
            aria-label="表示位置"
            className="p-0.5"
          >
            {POSITION_OPTIONS.map((option) => (
              <ToggleGroupItem key={option.value} value={option.value} className="h-7 min-w-0 px-2 text-xs">
                {option.label}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        </div>
        <div className="flex items-center justify-between gap-2">
          <Label htmlFor="preview-safe-area" className="text-xs font-normal text-muted-foreground">
            セーフエリアを表示
          </Label>
          <Switch id="preview-safe-area" checked={showSafeArea} onCheckedChange={setShowSafeArea} />
        </div>
        {backgroundUrl ? (
          <div className="flex items-center justify-between gap-2">
            <Label htmlFor="preview-background" className="text-xs font-normal text-muted-foreground">
              素材を背景に表示
            </Label>
            <Switch id="preview-background" checked={useBackground} onCheckedChange={setUseBackground} />
          </div>
        ) : null}
        <p className="text-2xs leading-4 text-subtle-foreground">
          ブラウザ上の近似表示です。実際の動画ではフォント・縁取り・改行位置がわずかに異なる場合があります。
        </p>
      </div>
    </div>
  );
}
