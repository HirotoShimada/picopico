import type { Metadata } from "next";

import { PageHeader } from "@/components/layout/page-header";

import { NewProjectForm } from "./new-project-form";

export const metadata: Metadata = {
  title: "プロジェクト作成",
};

export default function NewProjectPage() {
  return (
    <>
      <PageHeader
        back={{ href: "/", label: "ダッシュボード" }}
        title="プロジェクト作成"
        description="楽曲の基本情報と世界観を設定し、音源をアップロードします。作成後に楽曲解析が自動で始まります。"
      />
      <NewProjectForm />
    </>
  );
}
