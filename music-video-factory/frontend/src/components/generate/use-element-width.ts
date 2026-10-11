"use client";

import * as React from "react";

/** Content-box width of an element (0 until measured), tracked with ResizeObserver. */
export function useElementWidth<T extends HTMLElement>(): [React.RefCallback<T>, number] {
  const [width, setWidth] = React.useState(0);
  const ref = React.useCallback((element: T | null) => {
    if (!element) return;
    const observer = new ResizeObserver((entries) => setWidth(entries[0]?.contentRect.width ?? 0));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}
