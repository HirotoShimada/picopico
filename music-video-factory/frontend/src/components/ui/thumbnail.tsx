import { ImageOff } from "lucide-react";
import * as React from "react";

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

function Thumbnail({ src, alt, ratio = "9/16", fallback, overlay, className, imgClassName, ...props }: ThumbnailProps) {
  return (
    <div
      data-slot="thumbnail"
      className={cn("relative overflow-hidden rounded-md border border-border bg-surface-raised", RATIO_CLASSES[ratio], className)}
      {...props}
    >
      {src ? (
        // eslint-disable-next-line @next/next/no-img-element -- signed, short-lived backend URLs
        <img src={src} alt={alt} loading="lazy" decoding="async" className={cn("size-full object-cover", imgClassName)} />
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
