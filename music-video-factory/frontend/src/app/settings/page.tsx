import type { Metadata } from "next";

import { PageHeader } from "@/components/layout/page-header";

import { SettingsView } from "./settings-view";

export const metadata: Metadata = {
  title: "設定",
};

export default function SettingsPage() {
  return (
    <>
      <PageHeader
        title="設定"
        description="バックエンドとの接続、API トークン、動画生成プロバイダーの設定状況と料金を確認します。API キーはサーバー側の環境変数で設定し、画面には表示されません。"
      />
      <SettingsView />
    </>
  );
}
