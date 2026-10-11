"use client";

import { Clapperboard } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import * as React from "react";

import { Skeleton } from "@/components/ui/skeleton";
import { useProjects } from "@/hooks/use-projects";
import { cn } from "@/lib/utils";

import { isNavItemActive, MAIN_NAV } from "./nav-items";

const RECENT_LIMIT = 6;

function NavLinks({ pathname }: { pathname: string | null }) {
  return (
    <ul className="flex flex-col gap-0.5">
      {MAIN_NAV.map((item) => {
        const active = isNavItemActive(item.href, pathname);
        const Icon = item.icon;
        return (
          <li key={item.href}>
            <Link
              href={item.href}
              aria-current={active ? "page" : undefined}
              className={cn(
                "focus-ring-custom flex h-9 items-center gap-2.5 rounded-md px-2.5 text-sm outline-none transition-colors",
                "focus-visible:ring-2 focus-visible:ring-ring/40",
                active
                  ? "bg-surface-hover font-medium text-foreground"
                  : "text-muted-foreground hover:bg-surface-raised hover:text-foreground",
              )}
            >
              <Icon className={cn("size-4 shrink-0", active ? "text-foreground" : "text-subtle-foreground")} />
              {item.label}
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

function RecentProjects({ pathname }: { pathname: string | null }) {
  const { data, error, isLoading } = useProjects();
  const recent = React.useMemo(
    () => (data ? [...data].sort((a, b) => b.updated_at.localeCompare(a.updated_at)).slice(0, RECENT_LIMIT) : []),
    [data],
  );

  let body: React.ReactNode;
  if (isLoading) {
    body = (
      <div className="flex flex-col gap-2 px-2.5 py-1">
        <Skeleton className="h-4 w-36" />
        <Skeleton className="h-4 w-28" />
        <Skeleton className="h-4 w-32" />
      </div>
    );
  } else if (error) {
    body = <p className="px-2.5 text-xs text-subtle-foreground">取得できませんでした</p>;
  } else if (recent.length === 0) {
    body = <p className="px-2.5 text-xs text-subtle-foreground">まだプロジェクトはありません</p>;
  } else {
    body = (
      <ul className="flex flex-col gap-0.5">
        {recent.map((project) => {
          const href = `/projects/${project.id}`;
          const active = !!pathname && (pathname === href || pathname.startsWith(`${href}/`));
          return (
            <li key={project.id}>
              <Link
                href={href}
                aria-current={active ? "page" : undefined}
                title={`${project.title} — ${project.artist_name}`}
                className={cn(
                  "focus-ring-custom flex min-h-8 flex-col justify-center rounded-md px-2.5 py-1 outline-none transition-colors",
                  "focus-visible:ring-2 focus-visible:ring-ring/40",
                  active ? "bg-surface-hover text-foreground" : "text-muted-foreground hover:bg-surface-raised hover:text-foreground",
                )}
              >
                <span className="truncate text-[13px] leading-5">{project.title}</span>
                <span className="truncate text-2xs text-subtle-foreground">{project.artist_name}</span>
              </Link>
            </li>
          );
        })}
      </ul>
    );
  }

  return (
    <div className="flex min-h-0 flex-col gap-1.5">
      <p className="px-2.5 text-2xs font-medium tracking-wide text-subtle-foreground">最近のプロジェクト</p>
      {body}
    </div>
  );
}

function SidebarContent({ pathname }: { pathname: string | null }) {
  return (
    <>
      <NavLinks pathname={pathname} />
      <div className="mt-6 min-h-0 flex-1 overflow-y-auto">
        <RecentProjects pathname={pathname} />
      </div>
    </>
  );
}

/** Reads the pathname (suspends during prerender of dynamic routes: render inside `<Suspense>`). */
function SidebarWithPathname() {
  const pathname = usePathname();
  return <SidebarContent pathname={pathname} />;
}

export function Sidebar() {
  return (
    <aside className="sticky top-0 flex h-screen w-60 shrink-0 flex-col border-r border-border bg-surface">
      <Link
        href="/"
        className="focus-ring-custom flex h-14 shrink-0 items-center gap-2.5 border-b border-border px-4 outline-none focus-visible:ring-2 focus-visible:ring-ring/40"
      >
        <span className="flex size-7 items-center justify-center rounded-md bg-zinc-100 text-zinc-900">
          <Clapperboard className="size-4" aria-hidden />
        </span>
        <span className="flex min-w-0 flex-col leading-tight">
          <span className="truncate text-sm font-semibold tracking-tight text-foreground">Music Video Factory</span>
          <span className="truncate text-2xs text-subtle-foreground">AI ミュージックビデオ量産</span>
        </span>
      </Link>
      <nav aria-label="メイン" className="flex min-h-0 flex-1 flex-col px-2.5 py-3">
        <React.Suspense fallback={<SidebarContent pathname={null} />}>
          <SidebarWithPathname />
        </React.Suspense>
      </nav>
      <div className="shrink-0 border-t border-border px-4 py-3 text-2xs leading-4 text-subtle-foreground">
        9:16 Instagram Stories 向け
        <br />
        音源・歌詞の権利をお持ちの楽曲のみご利用ください
      </div>
    </aside>
  );
}
