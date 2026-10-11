"use client";

import { Pause, Play, RefreshCw, RotateCcw, RotateCw } from "lucide-react";
import * as React from "react";

import { Button } from "@/components/ui/button";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { SimpleTooltip } from "@/components/ui/tooltip";
import { formatTimePrecise } from "@/lib/format";
import { cn } from "@/lib/utils";

import { useAudioPlayer, useAudioReload, useAudioState, useAudioTime } from "./audio-player";

const RATES = [0.5, 0.75, 1] as const;

function TimeReadout({ duration }: { duration: number }) {
  const time = useAudioTime();
  return (
    <span className="font-mono text-xs tabular text-muted-foreground">
      <span className="text-foreground">{formatTimePrecise(time)}</span>
      <span className="mx-1 text-subtle-foreground">/</span>
      {formatTimePrecise(duration)}
    </span>
  );
}

export interface AudioTransportProps {
  /** Song length from the analysis; falls back to the element's duration. */
  duration?: number | null;
  showRate?: boolean;
  className?: string;
  /** Extra controls on the right. */
  children?: React.ReactNode;
}

/** Play/pause, ±5 s, time readout and playback rate for the shared player. */
export function AudioTransport({ duration, showRate = true, className, children }: AudioTransportProps) {
  const player = useAudioPlayer();
  const state = useAudioState();
  const reload = useAudioReload();
  const total = duration ?? state.duration;
  const disabled = !state.hasSource;

  return (
    <div className={cn("flex flex-wrap items-center gap-2", className)}>
      <SimpleTooltip content={state.playing ? "一時停止（Space）" : "再生（Space）"}>
        <Button
          type="button"
          variant="secondary"
          size="icon-sm"
          onClick={player.toggle}
          disabled={disabled}
          aria-label={state.playing ? "一時停止" : "再生"}
        >
          {state.playing ? <Pause aria-hidden /> : <Play aria-hidden />}
        </Button>
      </SimpleTooltip>
      <SimpleTooltip content="5秒戻る（←）">
        <Button type="button" variant="ghost" size="icon-sm" onClick={() => player.skip(-5)} disabled={disabled} aria-label="5秒戻る">
          <RotateCcw aria-hidden />
        </Button>
      </SimpleTooltip>
      <SimpleTooltip content="5秒進む（→）">
        <Button type="button" variant="ghost" size="icon-sm" onClick={() => player.skip(5)} disabled={disabled} aria-label="5秒進む">
          <RotateCw aria-hidden />
        </Button>
      </SimpleTooltip>
      <TimeReadout duration={total} />
      {state.error ? (
        <span className="flex items-center gap-2 text-xs text-danger-foreground" role="alert">
          {state.error}
          <Button type="button" variant="outline" size="xs" onClick={reload}>
            <RefreshCw aria-hidden />
            再読み込み
          </Button>
        </span>
      ) : null}
      <div className="ml-auto flex items-center gap-2">
        {children}
        {showRate ? (
          <ToggleGroup
            type="single"
            value={String(state.rate)}
            onValueChange={(value) => value && player.setRate(Number(value))}
            aria-label="再生速度"
            className="p-0.5"
          >
            {RATES.map((rate) => (
              <ToggleGroupItem key={rate} value={String(rate)} className="h-7 min-w-0 px-2 text-xs tabular" disabled={disabled}>
                {rate}×
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        ) : null}
      </div>
    </div>
  );
}
