import { FolderPlus, LayoutDashboard, Settings } from "lucide-react";
import type { ComponentType } from "react";

export interface NavItem {
  href: "/" | "/projects/new" | "/settings";
  label: string;
  icon: ComponentType<{ className?: string }>;
}

export const MAIN_NAV: readonly NavItem[] = [
  { href: "/", label: "ダッシュボード", icon: LayoutDashboard },
  { href: "/projects/new", label: "プロジェクト作成", icon: FolderPlus },
  { href: "/settings", label: "設定", icon: Settings },
];

export function isNavItemActive(href: string, pathname: string | null): boolean {
  if (!pathname) return false;
  return href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);
}

/** Section label for the top bar. */
export function sectionLabel(pathname: string | null): string {
  if (!pathname || pathname === "/") return "ダッシュボード";
  if (pathname === "/projects/new") return "プロジェクト作成";
  if (pathname.startsWith("/settings")) return "設定";
  if (/^\/projects\/[^/]+\/generate/.test(pathname)) return "生成設定";
  if (/^\/projects\/[^/]+\/videos/.test(pathname)) return "動画一覧";
  if (pathname.startsWith("/projects/")) return "プロジェクト";
  if (pathname.startsWith("/batches/")) return "生成ジョブ";
  if (pathname.startsWith("/videos/")) return "動画エディター";
  return "";
}
