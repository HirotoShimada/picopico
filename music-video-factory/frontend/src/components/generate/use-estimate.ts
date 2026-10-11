"use client";

import * as React from "react";
import useSWR from "swr";

import { estimateBatch, type ApiError } from "@/lib/api";
import type { Estimate, GenerationSettingsInput } from "@/lib/types";

const DEBOUNCE_MS = 500;

function useDebouncedValue<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = React.useState(value);
  React.useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}

export interface EstimateState {
  data: Estimate | undefined;
  error: ApiError | undefined;
  /** The shown estimate does not match the current settings yet (typing / request in flight). */
  stale: boolean;
  /** `data` was computed for exactly the current settings. */
  current: boolean;
  retry: () => void;
}

/**
 * Live cost estimate (`POST /api/projects/{id}/estimate`), debounced while the
 * settings change. The previous result stays visible (marked stale) until the
 * new one arrives.
 */
export function useEstimate(projectId: string, settings: GenerationSettingsInput | null, enabled: boolean): EstimateState {
  const key = enabled && settings ? JSON.stringify(settings) : null;
  const debouncedKey = useDebouncedValue(key, DEBOUNCE_MS);
  const requestKey = debouncedKey !== null ? (["estimate", projectId, debouncedKey] as const) : null;

  const { data, error, isValidating, mutate } = useSWR<Estimate, ApiError, typeof requestKey>(
    requestKey,
    ([, id, body]) => estimateBatch(id, JSON.parse(body) as GenerationSettingsInput),
    {
      keepPreviousData: true,
      revalidateOnFocus: false,
      revalidateIfStale: false,
      shouldRetryOnError: false,
      dedupingInterval: 1_000,
    },
  );

  const settled = key !== null && key === debouncedKey && !isValidating;
  return {
    data: key ? data : undefined,
    error: key && settled ? error : undefined,
    stale: key !== null && !settled,
    current: settled && !error && data !== undefined,
    retry: () => void mutate(),
  };
}
