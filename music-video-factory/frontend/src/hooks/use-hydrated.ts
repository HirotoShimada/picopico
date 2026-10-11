"use client";

import { useSyncExternalStore } from "react";

const subscribe = () => () => {};

/**
 * `false` on the server and during hydration, `true` afterwards. Use it for
 * values that can differ between the prerendered HTML and the browser (e.g.
 * the pathname on the static 404 page).
 */
export function useHydrated(): boolean {
  return useSyncExternalStore(
    subscribe,
    () => true,
    () => false,
  );
}
