"use client";

import { usePathname } from "next/navigation";
import * as React from "react";

import { useHydrated } from "@/hooks/use-hydrated";
import { API_BASE_URL } from "@/lib/api";

import { BackendStatus } from "./backend-status";
import { sectionLabel } from "./nav-items";

/** Rendered after hydration only: the static 404 page is prerendered with a different pathname. */
function SectionTitle() {
  const pathname = usePathname();
  const hydrated = useHydrated();
  return <span>{hydrated ? sectionLabel(pathname) : null}</span>;
}

export function TopBar() {
  return (
    <header className="sticky top-0 z-30 flex h-14 shrink-0 items-center justify-between gap-4 border-b border-border bg-background/95 px-8 backdrop-blur supports-[backdrop-filter]:bg-background/80">
      <div className="flex min-w-0 items-center gap-2 text-sm font-medium text-muted-foreground">
        <React.Suspense fallback={<span />}>
          <SectionTitle />
        </React.Suspense>
      </div>
      <div className="flex items-center gap-3">
        <span className="hidden font-mono text-2xs text-subtle-foreground lg:inline" title="NEXT_PUBLIC_API_BASE_URL">
          {API_BASE_URL}
        </span>
        <BackendStatus />
      </div>
    </header>
  );
}
