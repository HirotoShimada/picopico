import * as React from "react";

import { Skeleton } from "@/components/ui/skeleton";

import { Sidebar } from "./sidebar";
import { TopBar } from "./top-bar";

function PageFallback() {
  return (
    <div className="flex flex-col gap-6" aria-busy aria-label="読み込み中">
      <div className="flex flex-col gap-2">
        <Skeleton className="h-7 w-56" />
        <Skeleton className="h-4 w-80" />
      </div>
      <Skeleton className="h-40 w-full" />
      <Skeleton className="h-64 w-full" />
    </div>
  );
}

/** Left sidebar + top bar + scrolling content column. */
export function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar />
        <main id="main" className="mx-auto w-full max-w-[1600px] flex-1 px-8 pt-6 pb-16">
          {/* Safety net for routes that read URL data while prerendering (Cache Components). */}
          <React.Suspense fallback={<PageFallback />}>{children}</React.Suspense>
        </main>
      </div>
    </div>
  );
}
