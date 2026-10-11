"use client";

import { Pencil } from "lucide-react";
import * as React from "react";
import { useSWRConfig } from "swr";

import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { toast } from "@/components/ui/toast";
import { usePresets } from "@/hooks/use-capabilities";
import { apiPaths, errorMessage, updateProject } from "@/lib/api";
import { formatDateTime, formatUsd } from "@/lib/format";
import type { ProjectOut, ProjectUpdate } from "@/lib/types";
import { formatAliases, parseAliases } from "@/lib/utils";

interface InfoValues {
  title: string;
  artist: string;
  aliases: string;
  mood: string;
  purpose: string;
  notes: string;
}

type InfoErrors = Partial<Record<keyof InfoValues, string>>;

function valuesOf(project: ProjectOut): InfoValues {
  return {
    title: project.title,
    artist: project.artist_name,
    aliases: formatAliases(project.artist_aliases),
    mood: project.mood ?? "",
    purpose: project.purpose ?? "",
    notes: project.notes ?? "",
  };
}

function validate(values: InfoValues): InfoErrors {
  const errors: InfoErrors = {};
  if (!values.title.trim()) errors.title = "楽曲名を入力してください";
  else if (values.title.trim().length > 200) errors.title = "楽曲名は200文字以内にしてください";
  if (!values.artist.trim()) errors.artist = "アーティスト名を入力してください";
  else if (values.artist.trim().length > 200) errors.artist = "アーティスト名は200文字以内にしてください";
  const aliases = parseAliases(values.aliases);
  if (aliases.length > 10) errors.aliases = "別表記は10件までにしてください";
  else if (aliases.some((alias) => alias.length > 100)) errors.aliases = "別表記は1件100文字以内にしてください";
  if (values.mood.length > 2000) errors.mood = "2000文字以内にしてください";
  if (values.purpose.length > 2000) errors.purpose = "2000文字以内にしてください";
  if (values.notes.length > 10_000) errors.notes = "10000文字以内にしてください";
  return errors;
}

function diff(project: ProjectOut, values: InfoValues): ProjectUpdate {
  const patch: ProjectUpdate = {};
  const optional = (text: string) => (text.trim() ? text.trim() : null);
  if (values.title.trim() !== project.title) patch.title = values.title.trim();
  if (values.artist.trim() !== project.artist_name) patch.artist_name = values.artist.trim();
  const aliases = parseAliases(values.aliases);
  if (formatAliases(aliases) !== formatAliases(project.artist_aliases)) patch.artist_aliases = aliases;
  if (optional(values.mood) !== (project.mood ?? null)) patch.mood = optional(values.mood);
  if (optional(values.purpose) !== (project.purpose ?? null)) patch.purpose = optional(values.purpose);
  if (optional(values.notes) !== (project.notes ?? null)) patch.notes = optional(values.notes);
  return patch;
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[6.5rem_minmax(0,1fr)] gap-3 py-2 text-sm first:pt-0 last:pb-0">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words whitespace-pre-wrap text-foreground">{children}</dd>
    </div>
  );
}

const Empty = () => <span className="text-subtle-foreground">未設定</span>;

/** 楽曲名・アーティスト・雰囲気・目的 (view / inline edit via `PATCH /api/projects/{id}`). */
export function ProjectInfoCard({ project }: { project: ProjectOut }) {
  const { mutate } = useSWRConfig();
  const { presets } = usePresets();
  const [values, setValues] = React.useState<InfoValues | null>(null);
  const [errors, setErrors] = React.useState<InfoErrors>({});
  const [saving, setSaving] = React.useState(false);
  const editing = values !== null;
  const preset = presets.find((item) => item.id === project.world_preset);

  function set<K extends keyof InfoValues>(key: K, value: string) {
    setValues((current) => (current ? { ...current, [key]: value } : current));
    if (errors[key]) setErrors((current) => ({ ...current, [key]: undefined }));
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (!values) return;
    const found = validate(values);
    setErrors(found);
    if (Object.keys(found).length > 0) return;
    const patch = diff(project, values);
    if (Object.keys(patch).length === 0) {
      setValues(null);
      return;
    }
    setSaving(true);
    try {
      const updated = await updateProject(project.id, patch);
      await mutate(apiPaths.project(project.id), updated, { revalidate: false });
      void mutate(apiPaths.projects);
      setValues(null);
      toast.success("楽曲情報を保存しました");
    } catch (error) {
      toast.error("楽曲情報を保存できませんでした", errorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>楽曲情報</CardTitle>
        <CardDescription>生成する映像の世界観やストーリーボードの参考になります。</CardDescription>
        {!editing ? (
          <CardAction>
            <Button type="button" variant="ghost" size="sm" onClick={() => setValues(valuesOf(project))}>
              <Pencil aria-hidden />
              編集
            </Button>
          </CardAction>
        ) : null}
      </CardHeader>
      <CardContent>
        {editing ? (
          <form className="flex flex-col gap-4" onSubmit={save} noValidate>
            <Field label="楽曲名" htmlFor="info-title" required error={errors.title}>
              <Input id="info-title" value={values.title} onChange={(e) => set("title", e.target.value)} aria-invalid={!!errors.title} />
            </Field>
            <Field label="アーティスト名" htmlFor="info-artist" required error={errors.artist}>
              <Input id="info-artist" value={values.artist} onChange={(e) => set("artist", e.target.value)} aria-invalid={!!errors.artist} />
            </Field>
            <Field
              label="アーティスト名の別表記"
              htmlFor="info-aliases"
              optional
              error={errors.aliases}
              description="ローマ字・英語表記など（カンマ区切り）。生成プロンプトから除外します。"
            >
              <Input id="info-aliases" value={values.aliases} onChange={(e) => set("aliases", e.target.value)} aria-invalid={!!errors.aliases} />
            </Field>
            <Field label="雰囲気" htmlFor="info-mood" optional error={errors.mood} description="例: 切ない、疾走感、夜明け">
              <Textarea id="info-mood" rows={2} value={values.mood} onChange={(e) => set("mood", e.target.value)} />
            </Field>
            <Field label="動画生成目的" htmlFor="info-purpose" optional error={errors.purpose} description="例: リリース告知のストーリーズ広告">
              <Textarea id="info-purpose" rows={2} value={values.purpose} onChange={(e) => set("purpose", e.target.value)} />
            </Field>
            <Field label="メモ" htmlFor="info-notes" optional error={errors.notes}>
              <Textarea id="info-notes" rows={2} value={values.notes} onChange={(e) => set("notes", e.target.value)} />
            </Field>
            <div className="flex justify-end gap-2">
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={saving}
                onClick={() => {
                  setValues(null);
                  setErrors({});
                }}
              >
                キャンセル
              </Button>
              <Button type="submit" variant="secondary" size="sm" loading={saving}>
                保存
              </Button>
            </div>
          </form>
        ) : (
          <dl className="divide-y divide-border">
            <Row label="楽曲名">{project.title}</Row>
            <Row label="アーティスト">{project.artist_name}</Row>
            {project.artist_aliases?.length ? <Row label="別表記">{formatAliases(project.artist_aliases)}</Row> : null}
            <Row label="雰囲気">{project.mood || <Empty />}</Row>
            <Row label="動画生成目的">{project.purpose || <Empty />}</Row>
            {project.notes ? <Row label="メモ">{project.notes}</Row> : null}
            <Row label="世界観">{preset?.name_ja ?? project.world_preset}</Row>
            <Row label="予算上限">{project.budget_limit_usd !== null ? formatUsd(project.budget_limit_usd) : "既定値"}</Row>
            <Row label="作成日時">{formatDateTime(project.created_at)}</Row>
          </dl>
        )}
      </CardContent>
    </Card>
  );
}
