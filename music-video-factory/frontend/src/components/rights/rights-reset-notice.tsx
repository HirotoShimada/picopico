"use client";

import { ShieldAlert } from "lucide-react";
import Link from "next/link";
import * as React from "react";

import { projectTabHref } from "@/components/generate/links";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import type { ProjectOut, RightsKey } from "@/lib/types";

import { useRightsResets } from "./rights-reset";

export interface RightsResetNoticeProps {
  project: Pick<ProjectOut, "id" | "rights">;
  /** Items this place of the UI changes (e.g. `audio_rights` next to the song upload). */
  keys: readonly RightsKey[];
  className?: string;
}

/**
 * Inline notice where the song or the lyrics were changed: the change withdrew a rights confirmation,
 * so generation stays blocked until it is confirmed again on the 権利確認 tab.
 */
export function RightsResetNotice({ project, keys, className }: RightsResetNoticeProps) {
  const resets = useRightsResets(project).filter((reset) => keys.includes(reset.key));
  if (resets.length === 0) return null;
  return (
    <Alert tone="warning" icon={ShieldAlert} title="権利確認をやり直してください" className={className}>
      <ul className="flex flex-col gap-0.5">
        {resets.map((reset) => (
          <li key={reset.key}>{reset.message}</li>
        ))}
      </ul>
      {/* Below the text (not beside it): the notice also sits in narrow cards. */}
      <Button variant="secondary" size="sm" className="mt-2" asChild>
        <Link href={projectTabHref(project.id, "rights")}>権利確認へ</Link>
      </Button>
    </Alert>
  );
}
