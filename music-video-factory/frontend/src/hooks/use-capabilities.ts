"use client";

import * as React from "react";
import useSWR from "swr";

import { apiPaths, swrFetcher, type ApiError } from "@/lib/api";
import { DEFAULT_LIMITS, WORLD_PRESETS_FALLBACK } from "@/lib/constants";
import type { CapabilitiesOut, LimitsInfo, WorldPresetInfo } from "@/lib/types";

/** `GET /api/system/capabilities` — changes rarely, so it is not polled. */
export function useCapabilities() {
  return useSWR<CapabilitiesOut, ApiError>(apiPaths.capabilities, swrFetcher, {
    revalidateOnFocus: false,
    dedupingInterval: 60_000,
  });
}

/**
 * World presets from the server, falling back to the built-in Japanese list
 * while loading or when capabilities are unavailable.
 */
export function usePresets(): { presets: readonly WorldPresetInfo[]; fromServer: boolean } {
  const { data } = useCapabilities();
  return React.useMemo(() => {
    if (!data || data.presets.length === 0) return { presets: WORLD_PRESETS_FALLBACK, fromServer: false };
    const fallbackById = new Map(WORLD_PRESETS_FALLBACK.map((preset) => [preset.id, preset]));
    const presets = data.presets.map((preset) => {
      const fallback = fallbackById.get(preset.id);
      return {
        ...fallback,
        ...preset,
        description_ja: preset.description_ja || fallback?.description_ja || "",
        palette: preset.palette.length > 0 ? preset.palette : (fallback?.palette ?? []),
      };
    });
    return { presets, fromServer: true };
  }, [data]);
}

/** Upload / generation limits (server values when loaded, else §3 defaults). */
export function useLimits(): LimitsInfo {
  const { data } = useCapabilities();
  return data?.limits ?? DEFAULT_LIMITS;
}
