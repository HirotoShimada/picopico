import { providerLabel } from "@/lib/constants";
import type { ClipOut } from "@/lib/types";

export type ClipOrigin = "upload" | "generated";

export function clipOrigin(clip: ClipOut): ClipOrigin {
  return clip.source === "upload" ? "upload" : "generated";
}

/** Short label for the clip's origin, e.g. 「アップロード」 or 「Kling」. */
export function clipSourceLabel(clip: ClipOut): string {
  if (clip.source === "upload") return "アップロード";
  if (clip.source === "mock") return "モック生成";
  return providerLabel(clip.provider || clip.source);
}

export function isVertical(clip: ClipOut): boolean | null {
  if (!clip.width || !clip.height) return null;
  return clip.height > clip.width;
}
