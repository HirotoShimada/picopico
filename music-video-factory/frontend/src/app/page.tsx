import { FolderPlus } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";

import { DashboardView } from "@/components/dashboard/dashboard-view";
import { PageHeader } from "@/components/layout/page-header";
import { Button } from "@/components/ui/button";

export const metadata: Metadata = {
  title: "ダッシュボード",
};

export default function DashboardPage() {
  return (
    <>
      <PageHeader
        title="ダッシュボード"
        description="プロジェクト、生成ジョブの進捗、API 利用額をまとめて確認できます。"
        actions={
          <Button variant="primary" asChild>
            <Link href="/projects/new">
              <FolderPlus aria-hidden />
              新規プロジェクト
            </Link>
          </Button>
        }
      />
      <DashboardView />
    </>
  );
}
