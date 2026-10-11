"use client";

import { Eye, EyeOff, KeyRound, ShieldCheck } from "lucide-react";
import * as React from "react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { toast } from "@/components/ui/toast";
import { useApiToken } from "@/hooks/use-api-token";
import { errorMessage, isApiError, listProjects } from "@/lib/api";

import { SettingsSection } from "./section";

type TestResult = { tone: "success" | "danger"; message: string } | null;

function maskToken(token: string): string {
  return token.length <= 4 ? "••••" : `••••••••${token.slice(-4)}`;
}

export function TokenSection() {
  const [token, setToken] = useApiToken();
  const [draft, setDraft] = React.useState("");
  const [visible, setVisible] = React.useState(false);
  const [testing, setTesting] = React.useState(false);
  const [result, setResult] = React.useState<TestResult>(null);

  function save(event: React.FormEvent) {
    event.preventDefault();
    if (!draft.trim()) return;
    setToken(draft.trim());
    setDraft("");
    setVisible(false);
    setResult(null);
    toast.success("API トークンを保存しました", "このブラウザにのみ保存されます。");
  }

  function clear() {
    setToken(null);
    setResult(null);
    toast({ title: "API トークンを削除しました" });
  }

  async function testConnection() {
    setTesting(true);
    setResult(null);
    try {
      const projects = await listProjects();
      setResult({ tone: "success", message: `API にアクセスできました（プロジェクト ${projects.length} 件）。` });
    } catch (error) {
      const message =
        isApiError(error) && error.status === 401
          ? token
            ? "トークンが無効です。正しいトークンを入力してください。"
            : "バックエンドはトークン認証モードです。トークンを設定してください。"
          : errorMessage(error);
      setResult({ tone: "danger", message });
    } finally {
      setTesting(false);
    }
  }

  return (
    <SettingsSection
      id="token"
      title="API トークン"
      description={
        <>
          バックエンドが <code className="font-mono">AUTH_MODE=token</code> で動作している場合のみ必要です（既定の dev モードでは不要）。
          トークンはこのブラウザの localStorage に保存され、<code className="font-mono">Authorization: Bearer</code> ヘッダーで送信されます。
        </>
      }
    >
      <div className="flex flex-col gap-4 px-5 pb-5">
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-border bg-surface-raised px-3 py-2.5">
          <div className="flex items-center gap-2.5">
            {token ? <ShieldCheck className="size-4 text-success" aria-hidden /> : <KeyRound className="size-4 text-subtle-foreground" aria-hidden />}
            <div className="flex flex-col">
              <span className="text-sm text-foreground">{token ? "トークン設定済み" : "トークン未設定"}</span>
              {token ? <span className="font-mono text-xs text-muted-foreground">{maskToken(token)}</span> : null}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="secondary" size="sm" onClick={() => void testConnection()} loading={testing}>
              接続テスト
            </Button>
            {token ? (
              <Button variant="destructive-outline" size="sm" onClick={clear}>
                削除
              </Button>
            ) : null}
          </div>
        </div>

        <form onSubmit={save} className="flex flex-col gap-3">
          <Field label={token ? "新しいトークン" : "トークン"} htmlFor="api-token" description="バックエンドの API_TOKENS に設定した name:token の token 部分を入力します。">
            <div className="flex gap-2">
              <div className="relative flex-1">
                <Input
                  id="api-token"
                  type={visible ? "text" : "password"}
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  placeholder="トークンを貼り付け"
                  autoComplete="off"
                  spellCheck={false}
                  className="pr-10 font-mono"
                />
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  className="absolute top-1/2 right-1.5 -translate-y-1/2"
                  onClick={() => setVisible((v) => !v)}
                  aria-label={visible ? "トークンを隠す" : "トークンを表示"}
                  aria-pressed={visible}
                >
                  {visible ? <EyeOff aria-hidden /> : <Eye aria-hidden />}
                </Button>
              </div>
              <Button type="submit" variant="primary" disabled={!draft.trim()}>
                保存
              </Button>
            </div>
          </Field>
        </form>

        {result ? <Alert tone={result.tone}>{result.message}</Alert> : null}
      </div>
    </SettingsSection>
  );
}
