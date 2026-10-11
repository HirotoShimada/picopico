"use client";

import { useSyncExternalStore } from "react";

const TICK_MS = 30_000;

let now = 0;
let timer: ReturnType<typeof setInterval> | null = null;
const listeners = new Set<() => void>();

function emit(): void {
  now = Date.now();
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (timer === null) {
    timer = setInterval(emit, TICK_MS);
    // Resolve the first value right after hydration.
    queueMicrotask(emit);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
}

const getSnapshot = (): number => now;
const getServerSnapshot = (): number => 0;

/**
 * Current time in ms, refreshed every 30 s, for relative timestamps. Returns
 * `0` during prerender/hydration: render an absolute date in that case.
 */
export function useNow(): number {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}
