import { ExternalLink } from "lucide-react";

import { cn } from "@/lib/utils";

import { badgeVariants } from "./badge";

/** Runway's API terms ask for this mark, linking to runwayml.com (docs/VIDEO_PROVIDERS.md §4.3). */
export const POWERED_BY_RUNWAY = "Powered by Runway";
export const RUNWAY_URL = "https://runwayml.com";

/** True for the Runway provider id / clip source. */
export function isRunway(id: string | null | undefined): boolean {
  return id === "runway";
}

export interface PoweredByRunwayProps {
  /**
   * `false` renders the mark as plain text, for places that are already a control
   * (e.g. a provider radio card), where a nested link would be invalid. Show the
   * linked mark elsewhere on the same screen.
   */
  link?: boolean;
  className?: string;
}

/** The 「Powered by Runway」 attribution shown wherever Runway is selected or its footage is used. */
export function PoweredByRunway({ link = true, className }: PoweredByRunwayProps) {
  const classes = cn(badgeVariants({ tone: "outline" }), className);
  if (!link) {
    return (
      <span lang="en" data-slot="powered-by-runway" className={classes}>
        {POWERED_BY_RUNWAY}
      </span>
    );
  }
  return (
    <a
      href={RUNWAY_URL}
      target="_blank"
      rel="noreferrer noopener"
      lang="en"
      data-slot="powered-by-runway"
      title="runwayml.com を新しいタブで開きます"
      className={cn(classes, "transition-colors hover:border-zinc-400 hover:bg-surface-hover")}
    >
      {POWERED_BY_RUNWAY}
      <ExternalLink aria-hidden />
    </a>
  );
}
