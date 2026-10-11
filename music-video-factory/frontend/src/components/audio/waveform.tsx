"use client";

import * as React from "react";

import { formatTimePrecise } from "@/lib/format";
import type { AudioAnalysis } from "@/lib/types";
import { cn } from "@/lib/utils";

import { useAudioPlayer, useAudioTimeListener } from "./audio-player";
import { drawWaveform, waveformLayout } from "./waveform-draw";
import {
  applyRangeDrag,
  clampTime,
  makeSnapper,
  MIN_SEGMENT_SECONDS,
  nextGridPoint,
  previousGridPoint,
  roundTime,
  type RangeDragMode,
  type TimeRange,
} from "./timeline";

export interface WaveformProps {
  analysis: AudioAnalysis;
  /** Current (draft) selection. */
  selection: TimeRange | null;
  /** Saved selection, drawn dashed while the draft differs. */
  savedSelection?: TimeRange | null;
  /** Enables selection editing (drag handles, drag to move, drag on empty area to create). */
  onSelectionChange?: (range: TimeRange) => void;
  /** Snap selection edges to these times (e.g. downbeats); `null` = 10 ms resolution. */
  snapGrid?: readonly number[] | null;
  minLength?: number;
  height?: number;
  className?: string;
}

/** Pixels the pointer must travel before a press becomes a drag (otherwise it is a click-to-seek). */
const DRAG_THRESHOLD_PX = 4;

interface DragState {
  pointerId: number;
  startX: number;
  anchor: number;
  origin: TimeRange | null;
  /** `pending-*` until the pointer moved past the threshold. */
  mode: RangeDragMode | "pending-move" | "pending-create";
}

/**
 * Canvas waveform with section bands, beat/downbeat ticks, chorus/highlight
 * markers, a playhead synced to the shared player, click-to-seek and a
 * draggable selection.
 */
export function Waveform({
  analysis,
  selection,
  savedSelection = null,
  onSelectionChange,
  snapGrid = null,
  minLength = MIN_SEGMENT_SECONDS,
  height = 200,
  className,
}: WaveformProps) {
  const player = useAudioPlayer();
  const containerRef = React.useRef<HTMLDivElement>(null);
  const canvasRef = React.useRef<HTMLCanvasElement>(null);
  const playheadRef = React.useRef<HTMLDivElement>(null);
  const dragRef = React.useRef<DragState | null>(null);
  const [width, setWidth] = React.useState(0);
  const [hover, setHover] = React.useState<{ x: number; time: number } | null>(null);
  const [dragging, setDragging] = React.useState<RangeDragMode | null>(null);

  const duration = analysis.duration;
  const layout = waveformLayout(height);
  const editable = !!onSelectionChange;
  const snap = React.useMemo(() => makeSnapper(snapGrid), [snapGrid]);

  React.useEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    const observer = new ResizeObserver((entries) => {
      const next = Math.floor(entries[0]?.contentRect.width ?? 0);
      setWidth((current) => (current === next ? current : next));
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  React.useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || width <= 0) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    drawWaveform(ctx, {
      width,
      height,
      devicePixelRatio: dpr,
      duration,
      peaks: analysis.waveform.peaks,
      sections: analysis.sections,
      beats: analysis.beats,
      downbeats: analysis.downbeats,
      chorusCandidates: analysis.chorus_candidates,
      highlights: analysis.highlights,
      selection,
      savedSelection,
    });
  }, [analysis, duration, height, selection, savedSelection, width]);

  useAudioTimeListener((time) => {
    const element = playheadRef.current;
    if (!element || duration <= 0) return;
    element.style.left = `${(clampTime(time, duration) / duration) * 100}%`;
  });

  function timeAtClientX(clientX: number): number {
    const rect = containerRef.current?.getBoundingClientRect();
    if (!rect || rect.width <= 0) return 0;
    return clampTime(((clientX - rect.left) / rect.width) * duration, duration);
  }

  function handlePointerDown(event: React.PointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    const time = timeAtClientX(event.clientX);
    const handle = (event.target as HTMLElement).closest<HTMLElement>("[data-handle]")?.dataset.handle;
    let mode: DragState["mode"];
    if (editable && selection && (handle === "start" || handle === "end")) mode = handle;
    else if (editable && selection && time >= selection.start && time <= selection.end) mode = "pending-move";
    else mode = "pending-create";
    dragRef.current = { pointerId: event.pointerId, startX: event.clientX, anchor: time, origin: selection, mode };
    event.currentTarget.setPointerCapture(event.pointerId);
    if (mode === "start" || mode === "end") setDragging(mode);
  }

  function handlePointerMove(event: React.PointerEvent<HTMLDivElement>) {
    const rect = containerRef.current?.getBoundingClientRect();
    if (rect) setHover({ x: event.clientX - rect.left, time: timeAtClientX(event.clientX) });

    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    if (drag.mode === "pending-move" || drag.mode === "pending-create") {
      if (!editable || Math.abs(event.clientX - drag.startX) < DRAG_THRESHOLD_PX) return;
      drag.mode = drag.mode === "pending-move" ? "move" : "create";
      setDragging(drag.mode);
    }
    const range = applyRangeDrag(drag.mode, drag.origin, drag.anchor, timeAtClientX(event.clientX), {
      duration,
      minLength,
      snap,
    });
    onSelectionChange?.(range);
  }

  function handlePointerUp(event: React.PointerEvent<HTMLDivElement>) {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    dragRef.current = null;
    setDragging(null);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    if (drag.mode === "pending-move" || drag.mode === "pending-create") player.seek(timeAtClientX(event.clientX));
  }

  function handleKeyDown(edge: "start" | "end", event: React.KeyboardEvent<HTMLDivElement>) {
    if (!selection || !onSelectionChange) return;
    const value = selection[edge];
    let next: number | null = null;
    const coarse = event.shiftKey || event.key === "PageUp" || event.key === "PageDown";
    if (event.key === "ArrowLeft" || event.key === "ArrowDown" || event.key === "PageDown") {
      next = snapGrid && snapGrid.length > 0 && !coarse ? previousGridPoint(snapGrid, value) : value - (coarse ? 1 : 0.1);
    } else if (event.key === "ArrowRight" || event.key === "ArrowUp" || event.key === "PageUp") {
      next = snapGrid && snapGrid.length > 0 && !coarse ? nextGridPoint(snapGrid, value) : value + (coarse ? 1 : 0.1);
    } else if (event.key === "Home") {
      next = 0;
    } else if (event.key === "End") {
      next = duration;
    } else {
      return;
    }
    event.preventDefault();
    if (next === null) return;
    onSelectionChange(applyRangeDrag(edge, selection, 0, next, { duration, minLength, snap: roundTime }));
  }

  const cursor =
    dragging === "move"
      ? "cursor-grabbing"
      : dragging
        ? "cursor-ew-resize"
        : editable && selection && hover && hover.time >= selection.start && hover.time <= selection.end
          ? "cursor-grab"
          : "cursor-pointer";

  const handleTop = layout.sectionsTop;
  const handleHeight = layout.markersTop - layout.sectionsTop;
  const pct = (seconds: number) => `${(seconds / duration) * 100}%`;

  return (
    <div
      ref={containerRef}
      role="group"
      aria-label="波形（クリックで再生位置を移動、ドラッグで区間を選択）"
      className={cn("relative w-full touch-none select-none", cursor, className)}
      style={{ height }}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
      onPointerCancel={handlePointerUp}
      onPointerLeave={() => setHover(null)}
    >
      <canvas ref={canvasRef} className="pointer-events-none absolute inset-0 size-full" aria-hidden />

      {hover && !dragging ? (
        <div className="pointer-events-none absolute" style={{ left: hover.x, top: layout.waveTop, height: layout.waveHeight }} aria-hidden>
          <div className="h-full w-px bg-zinc-500/70" />
          <span className="absolute top-1 left-1.5 rounded bg-zinc-900/90 px-1 py-px font-mono text-2xs whitespace-nowrap text-zinc-300 tabular">
            {formatTimePrecise(hover.time)}
          </span>
        </div>
      ) : null}

      {editable && selection
        ? (["start", "end"] as const).map((edge) => (
            <div
              key={edge}
              data-handle={edge}
              role="slider"
              tabIndex={0}
              aria-label={edge === "start" ? "区間の開始位置" : "区間の終了位置"}
              aria-valuemin={0}
              aria-valuemax={Math.round(duration * 100) / 100}
              aria-valuenow={selection[edge]}
              aria-valuetext={formatTimePrecise(selection[edge])}
              onKeyDown={(event) => handleKeyDown(edge, event)}
              className="focus-ring-custom group absolute z-10 flex w-4 -translate-x-1/2 cursor-ew-resize items-center justify-center outline-none"
              style={{ left: pct(selection[edge]), top: handleTop, height: handleHeight }}
            >
              <span className="h-8 w-2.5 rounded-sm border border-zinc-900 bg-zinc-100 shadow group-hover:bg-white group-focus-visible:ring-2 group-focus-visible:ring-ring/70" />
            </div>
          ))
        : null}

      <div
        ref={playheadRef}
        className="pointer-events-none absolute z-20 -translate-x-1/2"
        style={{ left: 0, top: layout.sectionsTop, height: layout.markersTop + layout.markersHeight - layout.sectionsTop }}
        aria-hidden
      >
        <div className="mx-auto h-full w-0.5 bg-white shadow-[0_0_0_1px_rgba(0,0,0,0.6)]" />
        <div className="absolute -top-1 left-1/2 size-0 -translate-x-1/2 border-x-[5px] border-t-[6px] border-x-transparent border-t-white" />
      </div>
    </div>
  );
}
