"use client";

import { useCallback, useSyncExternalStore } from "react";

import { getApiToken, setApiToken, subscribeApiToken } from "@/lib/api";

const getServerSnapshot = (): string | null => null;

/**
 * The Bearer token stored on /settings (`null` when unset or during SSR) and a
 * setter that persists it and notifies every tab.
 */
export function useApiToken(): [string | null, (token: string | null) => void] {
  const token = useSyncExternalStore(subscribeApiToken, getApiToken, getServerSnapshot);
  const update = useCallback((value: string | null) => setApiToken(value), []);
  return [token, update];
}
