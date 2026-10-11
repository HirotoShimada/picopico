"use client";

import { ShieldCheck, UserX } from "lucide-react";
import * as React from "react";
import { useSWRConfig } from "swr";

import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { toast } from "@/components/ui/toast";
import { apiPaths, errorMessage, updateRights } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import type { ProjectOut, RightsKey } from "@/lib/types";
import { cn } from "@/lib/utils";

import { LIKENESS_POLICY, RIGHTS_CHECKLIST } from "./rights-items";

type RightsState = Record<RightsKey, boolean>;

function fromProject(project: ProjectOut): RightsState {
  return Object.fromEntries(RIGHTS_CHECKLIST.map((item) => [item.key, project.rights[item.key] === true])) as RightsState;
}

function sameRights(a: RightsState, b: RightsState): boolean {
  return RIGHTS_CHECKLIST.every((item) => a[item.key] === b[item.key]);
}

/** Tab 「権利確認」: the five §9 confirmations (`PUT /rights`), status and the likeness policy. */
export function RightsPanel({ project }: { project: ProjectOut }) {
  const { mutate } = useSWRConfig();
  const saved = React.useMemo(() => fromProject(project), [project]);
  const [draft, setDraft] = React.useState<RightsState | null>(null);
  const [saving, setSaving] = React.useState(false);

  const values = draft ?? saved;
  const dirty = draft !== null && !sameRights(draft, saved);
  const checkedCount = RIGHTS_CHECKLIST.filter((item) => values[item.key]).length;
  const savedCount = RIGHTS_CHECKLIST.filter((item) => saved[item.key]).length;
  const allSaved = project.rights_confirmed_at !== null && savedCount === RIGHTS_CHECKLIST.length;

  async function save() {
    setSaving(true);
    try {
      const updated = await updateRights(project.id, values);
      await mutate(apiPaths.project(project.id), updated, { revalidate: false });
      void mutate(apiPaths.projects);
      setDraft(null);
      if (updated.rights_confirmed_at) toast.success("権利確認を保存しました", "動画生成を開始できます。");
      else toast.success("権利確認を保存しました", "すべての項目を確認すると動画生成を開始できます。");
    } catch (error) {
      toast.error("権利確認を保存できませんでした", errorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(320px,400px)]">
      <div className="flex min-w-0 flex-col gap-6">
        {allSaved ? (
          <Alert tone="success" title="権利確認が完了しています">
            {formatDateTime(project.rights_confirmed_at)} に5項目すべての確認が記録されました。項目のチェックを外すと、動画生成は再びできなくなります。
          </Alert>
        ) : (
          <Alert tone="warning" title="権利確認が完了するまで、動画生成は開始できません">
            下の5項目をすべて確認して保存してください（現在 {savedCount} / {RIGHTS_CHECKLIST.length} 項目が確認済み）。チェックリストは自己申告で、システムが権利の有無を検証するものではありません。
          </Alert>
        )}

        <Card>
          <CardHeader>
            <CardTitle>
              <ShieldCheck className="size-4 text-muted-foreground" aria-hidden />
              権利確認チェックリスト
              <Badge tone={checkedCount === RIGHTS_CHECKLIST.length ? "success" : "neutral"}>
                {checkedCount} / {RIGHTS_CHECKLIST.length}
              </Badge>
              {dirty ? <Badge tone="warning">未保存</Badge> : null}
            </CardTitle>
            <CardDescription>各項目の内容を確認し、当てはまる場合にチェックしてください。確認した日時が記録されます。</CardDescription>
          </CardHeader>
          <CardContent className="p-0">
            <ul className="divide-y divide-border border-t border-border">
              {RIGHTS_CHECKLIST.map((item, index) => {
                const id = `rights-${item.key}`;
                const checked = values[item.key];
                return (
                  <li key={item.key} className={cn("flex gap-3 px-5 py-4", checked ? "bg-transparent" : "bg-surface-raised/30")}>
                    <Checkbox
                      id={id}
                      checked={checked}
                      onCheckedChange={(next) => setDraft({ ...values, [item.key]: next === true })}
                      aria-describedby={`${id}-description`}
                      className="mt-0.5"
                    />
                    <div className="flex min-w-0 flex-col gap-1">
                      <label htmlFor={id} className="cursor-pointer text-sm font-medium leading-5 text-foreground">
                        <span className="mr-1.5 font-mono text-xs text-subtle-foreground">{index + 1}.</span>
                        {item.label}
                      </label>
                      <p id={`${id}-description`} className="text-xs leading-5 text-muted-foreground">
                        {item.explanation}
                      </p>
                    </div>
                  </li>
                );
              })}
            </ul>
          </CardContent>
          <CardFooter className="justify-between">
            <span className="text-xs text-muted-foreground">
              {project.rights_confirmed_at ? `最終確認: ${formatDateTime(project.rights_confirmed_at)}` : "まだ確認は記録されていません"}
            </span>
            <div className="flex items-center gap-2">
              <Button type="button" variant="ghost" size="sm" onClick={() => setDraft(null)} disabled={!dirty || saving}>
                元に戻す
              </Button>
              <Button type="button" variant="primary" size="sm" onClick={() => void save()} disabled={!dirty} loading={saving}>
                確認内容を保存
              </Button>
            </div>
          </CardFooter>
        </Card>
      </div>

      <aside className="flex min-w-0 flex-col gap-6">
        <Card>
          <CardHeader>
            <CardTitle>
              <UserX className="size-4 text-muted-foreground" aria-hidden />
              肖像（ライクネス）ポリシー
            </CardTitle>
            <CardDescription>実在の人物の肖像を守るため、生成時に次のルールを適用します。</CardDescription>
          </CardHeader>
          <CardContent>
            <ul className="flex list-disc flex-col gap-2 pl-4 text-sm leading-6 text-foreground/90 marker:text-subtle-foreground">
              {LIKENESS_POLICY.map((rule) => (
                <li key={rule}>{rule}</li>
              ))}
            </ul>
          </CardContent>
        </Card>
      </aside>
    </div>
  );
}
