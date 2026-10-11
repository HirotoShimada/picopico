"use client";

import { ImageOff } from "lucide-react";
import * as React from "react";
import { useSWRConfig } from "swr";

import { cn } from "@/lib/utils";

export interface ThumbnailProps extends React.ComponentProps<"div"> {
  /** Signed media URL. Plain `<img>`: signed URLs from the API are not optimizable by next/image. */
  src: string | null | undefined;
  alt: string;
  /** CSS aspect ratio; defaults to 9:16 (Instagram Stories). */
  ratio?: "9/16" | "16/9" | "1/1";
  /** Shown when there is no image. */
  fallback?: React.ReactNode;
  /** Absolutely positioned content over the image (badges, duration). */
  overlay?: React.ReactNode;
  imgClassName?: string;
}

const RATIO_CLASSES = { "9/16": "aspect-[9/16]", "16/9": "aspect-video", "1/1": "aspect-square" } as const;
/** At most one refetch of all API data per this many ms when images fail to load. */
const REFRESH_THROTTLE_MS = 30_000;
let lastRefresh = 0;

/**
 * Signed media URLs expire (`MEDIA_URL_TTL`). An image mounted from cached data after that
 * fails with 403: show the fallback and refetch the API data once (throttled), which hands
 * out fresh URLs; the new `src` then loads normally.
 */
function useExpiredImageRecovery(src: string | null | undefined) {
  const { mutate } = useSWRConfig();
  const [failedSrc, setFailedSrc] = React.useState<string | null>(null);
  const onError = React.useCallback(() => {
    if (!src) return;
    setFailedSrc(src);
    const now = Date.now();
    if (now - lastRefresh < REFRESH_THROTTLE_MS) return;
    lastRefresh = now;
    void mutate(() => true);
  }, [mutate, src]);
  return { failed: !!src && failedSrc === src, onError };
}

function Thumbnail({ src, alt, ratio = "9/16", fallback, overlay, className, imgClassName, ...props }: ThumbnailProps) {
  const { failed, onError } = useExpiredImageRecovery(src);
  return (
    <div
      data-slot="thumbnail"
      className={cn("relative overflow-hidden rounded-md border border-border bg-surface-raised", RATIO_CLASSES[ratio], className)}
      {...props}
    >
      {src && !failed ? (
        // eslint-disable-next-line @next/next/no-img-element -- signed, short-lived backend URLs
        <img src={src} alt={alt} loading="lazy" decoding="async" onError={onError} className={cn("size-full object-cover", imgClassName)} />
      ) : (
        <div className="flex size-full items-center justify-center text-subtle-foreground">
          {fallback ?? <ImageOff className="size-5" aria-label={alt} />}
        </div>
      )}
      {overlay}
    </div>
  );
}

export { Thumbnail };
