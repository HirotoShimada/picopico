"use client";

import { ArrowRight, CircleCheck, FolderPlus, Plus } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import * as React from "react";
import { useSWRConfig } from "swr";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import { Textarea } from "@/components/ui/textarea";
import { toast } from "@/components/ui/toast";
import { WorldPresetPicker } from "@/components/ui/world-preset-picker";
import { useLimits, usePresets } from "@/hooks/use-capabilities";
import { apiPaths, createProject, errorMessage, isAbortError, uploadAudio } from "@/lib/api";
import { formatPercent } from "@/lib/format";
import type { PresetId, ProjectOut } from "@/lib/types";
import { cn } from "@/lib/utils";

import { audioDurationProblem, AudioFileSection, readAudioDuration, type SelectedAudio } from "./audio-file-card";

const MOOD_SUGGESTIONS = ["切ない", "疾走感", "爽やか", "エモーショナル", "ダーク", "幻想的", "ノスタルジック", "力強い"];
const PURPOSE_SUGGESTIONS = ["リリース告知", "ストーリーズ広告", "ティザー", "歌詞動画", "ライブ告知"];

type Phase = "idle" | "creating" | "uploading" | "processing" | "failed";

interface FormValues {
  title: string;
  artist: string;
  mood: string;
  purpose: string;
  notes: string;
  preset: PresetId;
  customWorld: string;
  budget: string;
}

type FieldErrors = Partial<Record<keyof FormValues, string>>;

/** DOM ids of the controls, in visual order (used to focus the first invalid field). */
const FIELD_IDS: Record<keyof FormValues, string> = {
  title: "title",
  artist: "artist",
  mood: "mood",
  purpose: "purpose",
  notes: "notes",
  preset: "preset-heading",
  customWorld: "customWorld",
  budget: "budget",
};

const INITIAL_VALUES: FormValues = {
  title: "",
  artist: "",
  mood: "",
  purpose: "",
  notes: "",
  preset: "cinematic",
  customWorld: "",
  budget: "",
};

function validate(values: FormValues): FieldErrors {
  const errors: FieldErrors = {};
  if (!values.title.trim()) errors.title = "楽曲名を入力してください";
  else if (values.title.trim().length > 200) errors.title = "楽曲名は200文字以内にしてください";
  if (!values.artist.trim()) errors.artist = "アーティスト名を入力してください";
  else if (values.artist.trim().length > 200) errors.artist = "アーティスト名は200文字以内にしてください";
  if (values.preset === "custom" && !values.customWorld.trim()) {
    errors.customWorld = "カスタムを選んだ場合は世界観を入力してください";
  }
  if (values.budget.trim()) {
    const budget = Number(values.budget);
    if (!Number.isFinite(budget) || budget < 0) errors.budget = "0以上の数値で入力してください";
    else if (budget > 100_000) errors.budget = "100,000 USD 以下にしてください";
  }
  return errors;
}

/** Appends a suggestion to a comma-separated free-text field (no duplicates). */
function appendTag(current: string, tag: string): string {
  const parts = current
    .split(/[、,]/)
    .map((part) => part.trim())
    .filter(Boolean);
  if (parts.includes(tag)) return current;
  return [...parts, tag].join("、");
}

function SuggestionChips({ items, onPick, disabled }: { items: string[]; onPick: (item: string) => void; disabled: boolean }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {items.map((item) => (
        <button
          key={item}
          type="button"
          disabled={disabled}
          onClick={() => onPick(item)}
          className="focus-ring-custom inline-flex h-6 cursor-pointer items-center gap-1 rounded-full border border-border px-2.5 text-xs text-muted-foreground outline-none transition-colors hover:border-border-strong hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/40 disabled:pointer-events-none disabled:opacity-50"
        >
          <Plus className="size-3" aria-hidden />
          {item}
        </button>
      ))}
    </div>
  );
}

export function NewProjectForm() {
  const router = useRouter();
  const { mutate } = useSWRConfig();
  const { presets } = usePresets();
  const limits = useLimits();

  const [values, setValues] = React.useState<FormValues>(INITIAL_VALUES);
  const [fieldErrors, setFieldErrors] = React.useState<FieldErrors>({});
  const [audio, setAudio] = React.useState<SelectedAudio | null>(null);
  const [rejectMessages, setRejectMessages] = React.useState<string[]>([]);
  const [phase, setPhase] = React.useState<Phase>("idle");
  const [progress, setProgress] = React.useState(0);
  const [formError, setFormError] = React.useState<string | null>(null);
  /** Set once the project exists, so a failed upload can be retried without creating a duplicate. */
  const [created, setCreated] = React.useState<ProjectOut | null>(null);

  const abortRef = React.useRef<AbortController | null>(null);
  const previewUrlRef = React.useRef<string | null>(null);

  // Leaving the page (unmount, or hidden by Activity on navigation) cancels a running upload.
  // The preview URL is kept so the page still works when the user comes back.
  React.useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  const busy = phase === "creating" || phase === "uploading" || phase === "processing";
  const locked = busy || created !== null;

  function setValue<K extends keyof FormValues>(key: K, value: FormValues[K]) {
    setValues((prev) => ({ ...prev, [key]: value }));
    if (fieldErrors[key]) setFieldErrors((prev) => ({ ...prev, [key]: undefined }));
  }

  function replacePreview(url: string | null) {
    if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
    previewUrlRef.current = url;
  }

  async function selectAudio(file: File) {
    setRejectMessages([]);
    const previewUrl = URL.createObjectURL(file);
    replacePreview(previewUrl);
    setAudio({ file, duration: null, previewUrl });
    const duration = await readAudioDuration(previewUrl);
    // Ignore results for a file the user has already replaced.
    setAudio((current) => (current && current.file === file ? { ...current, duration } : current));
  }

  function clearAudio() {
    replacePreview(null);
    setAudio(null);
    setRejectMessages([]);
  }

  function resetForm() {
    setValues(INITIAL_VALUES);
    setFieldErrors({});
    clearAudio();
    setPhase("idle");
    setProgress(0);
    setFormError(null);
    setCreated(null);
  }

  function finish(project: ProjectOut) {
    toast.success("プロジェクトを作成しました", audio ? "楽曲解析を開始しました。" : undefined);
    // Cache Components keeps this page mounted (hidden) after navigation; start fresh next time.
    resetForm();
    router.push(`/projects/${project.id}`);
  }

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setFormError(null);

    const errors = created ? {} : validate(values);
    setFieldErrors(errors);
    const firstInvalid = (Object.keys(INITIAL_VALUES) as (keyof FormValues)[]).find((key) => errors[key]);
    if (firstInvalid) {
      document.getElementById(FIELD_IDS[firstInvalid])?.focus();
      return;
    }
    if (audio && audioDurationProblem(audio.duration)) return;

    let project = created;
    try {
      if (!project) {
        setPhase("creating");
        project = await createProject({
          title: values.title.trim(),
          artist_name: values.artist.trim(),
          mood: values.mood.trim() || null,
          purpose: values.purpose.trim() || null,
          notes: values.notes.trim() || null,
          world_preset: values.preset,
          custom_world: values.preset === "custom" ? values.customWorld.trim() : null,
          budget_limit_usd: values.budget.trim() ? Number(values.budget) : null,
        });
        setCreated(project);
        void mutate(apiPaths.projects);
        void mutate(apiPaths.dashboard);
      }

      if (audio) {
        const controller = new AbortController();
        abortRef.current = controller;
        setProgress(0);
        setPhase("uploading");
        const updated = await uploadAudio(project.id, audio.file, {
          signal: controller.signal,
          onProgress: ({ fraction }) => {
            setProgress(fraction);
            if (fraction >= 1) setPhase("processing");
          },
        });
        abortRef.current = null;
        void mutate(apiPaths.project(project.id), updated, { revalidate: false });
        void mutate(apiPaths.projects);
      }
      finish(project);
    } catch (error) {
      abortRef.current = null;
      setPhase("failed");
      if (isAbortError(error)) {
        setFormError("アップロードを中止しました。");
      } else {
        setFormError(errorMessage(error));
      }
    }
  }

  const phaseLabel: Record<Phase, string> = {
    idle: "",
    creating: "プロジェクトを作成しています…",
    uploading: `音源をアップロード中… ${formatPercent(progress)}`,
    processing: "サーバーで音源を検証しています…",
    failed: "",
  };

  return (
    <form onSubmit={handleSubmit} noValidate className="grid grid-cols-1 items-start gap-6 xl:grid-cols-3">
      <div className="flex min-w-0 flex-col gap-6 xl:col-span-2">
        {created ? (
          <Alert tone="success" icon={CircleCheck} title="プロジェクトは作成済みです">
            「{created.title}」を作成しました。音源のアップロードを再試行するか、音源なしでプロジェクトを開けます。
          </Alert>
        ) : null}

        <Card>
          <CardHeader>
            <CardTitle>基本情報</CardTitle>
            <CardDescription>楽曲名とアーティスト名は歌詞動画のタイトルやファイル名に使われます。</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-5">
            <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
              <Field label="楽曲名" htmlFor="title" required error={fieldErrors.title}>
                <Input
                  id="title"
                  value={values.title}
                  onChange={(e) => setValue("title", e.target.value)}
                  placeholder="例：夜明けのシグナル"
                  maxLength={200}
                  autoComplete="off"
                  disabled={locked}
                  aria-invalid={!!fieldErrors.title || undefined}
                  aria-describedby={fieldErrors.title ? "title-error" : undefined}
                />
              </Field>
              <Field label="アーティスト名" htmlFor="artist" required error={fieldErrors.artist}>
                <Input
                  id="artist"
                  value={values.artist}
                  onChange={(e) => setValue("artist", e.target.value)}
                  placeholder="例：Hikari Lab"
                  maxLength={200}
                  autoComplete="off"
                  disabled={locked}
                  aria-invalid={!!fieldErrors.artist || undefined}
                  aria-describedby={fieldErrors.artist ? "artist-error" : undefined}
                />
              </Field>
            </div>

            <Field
              label="雰囲気"
              htmlFor="mood"
              optional
              description="映像のトーンやストーリーボード生成のヒントになります。アーティスト本人の容姿は指定しないでください。"
            >
              <Input
                id="mood"
                value={values.mood}
                onChange={(e) => setValue("mood", e.target.value)}
                placeholder="例：切ない、夜明け前の静けさ、少しずつ希望が見える"
                maxLength={2000}
                disabled={locked}
              />
              <SuggestionChips items={MOOD_SUGGESTIONS} disabled={locked} onPick={(tag) => setValue("mood", appendTag(values.mood, tag))} />
            </Field>

            <Field label="動画生成目的" htmlFor="purpose" optional>
              <Input
                id="purpose"
                value={values.purpose}
                onChange={(e) => setValue("purpose", e.target.value)}
                placeholder="例：新曲リリースの告知用ストーリーズ"
                maxLength={2000}
                disabled={locked}
              />
              <SuggestionChips
                items={PURPOSE_SUGGESTIONS}
                disabled={locked}
                onPick={(tag) => setValue("purpose", appendTag(values.purpose, tag))}
              />
            </Field>

            <Field label="メモ" htmlFor="notes" optional>
              <Textarea
                id="notes"
                value={values.notes}
                onChange={(e) => setValue("notes", e.target.value)}
                placeholder="チーム内の共有事項など（動画には使われません）"
                maxLength={10_000}
                rows={2}
                disabled={locked}
              />
            </Field>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle id="preset-heading">世界観プリセット</CardTitle>
            <CardDescription>生成される映像のルック（光・色・カメラ・場所）の基準です。生成設定で動画ごとに変更できます。</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <WorldPresetPicker
              aria-labelledby="preset-heading"
              presets={presets}
              value={values.preset}
              onValueChange={(id) => setValue("preset", id as PresetId)}
              disabled={locked}
            />
            {values.preset === "custom" ? (
              <Field
                label="カスタム世界観"
                htmlFor="customWorld"
                required
                error={fieldErrors.customWorld}
                description="場所・時間帯・天気・色・質感などを具体的に。実在の人物名やブランド名は使わないでください。"
              >
                <Textarea
                  id="customWorld"
                  value={values.customWorld}
                  onChange={(e) => setValue("customWorld", e.target.value)}
                  placeholder="例：雨上がりの港町。夕方のオレンジと青の街灯、霧がかった桟橋、フィルムの粒子感"
                  maxLength={4000}
                  rows={3}
                  disabled={locked}
                  aria-invalid={!!fieldErrors.customWorld || undefined}
                />
              </Field>
            ) : null}
          </CardContent>
        </Card>
      </div>

      <div className="flex min-w-0 flex-col gap-6 xl:sticky xl:top-20">
        <Card>
          <CardHeader>
            <CardTitle>音源</CardTitle>
            <CardDescription>作成後すぐにアップロードされ、BPM・サビ候補などの解析が始まります。後からでも追加できます。</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <AudioFileSection
              audio={audio}
              maxAudioMb={limits.max_audio_mb}
              disabled={busy}
              onSelect={(file) => void selectAudio(file)}
              onClear={clearAudio}
              rejectMessages={rejectMessages}
              onReject={setRejectMessages}
            />
            <p className="text-2xs leading-4 text-subtle-foreground">
              権利を保有している、または利用許諾を得ている音源のみアップロードしてください。ファイルは非公開で保存されます。
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>予算</CardTitle>
            <CardDescription>1回の生成ジョブで使える API 費用の上限です。空欄ならサーバーの既定値を使います。</CardDescription>
          </CardHeader>
          <CardContent>
            <Field label="予算上限（USD）" htmlFor="budget" optional error={fieldErrors.budget}>
              <div className="relative">
                <span className="pointer-events-none absolute top-1/2 left-3 -translate-y-1/2 text-sm text-subtle-foreground">$</span>
                <Input
                  id="budget"
                  type="number"
                  inputMode="decimal"
                  min={0}
                  step="0.01"
                  value={values.budget}
                  onChange={(e) => setValue("budget", e.target.value)}
                  placeholder="50"
                  className="pl-6"
                  disabled={locked}
                  aria-invalid={!!fieldErrors.budget || undefined}
                />
              </div>
            </Field>
          </CardContent>
        </Card>

        <div className="flex flex-col gap-3">
          {busy ? (
            <div className="flex flex-col gap-2 rounded-lg border border-border bg-surface px-4 py-3" aria-live="polite">
              <p className="text-sm text-foreground">{phaseLabel[phase]}</p>
              <Progress
                value={phase === "creating" || phase === "processing" ? null : progress * 100}
                aria-label="アップロードの進捗"
              />
            </div>
          ) : null}
          {formError ? <Alert tone="danger">{formError}</Alert> : null}

          <Button type="submit" variant="primary" size="lg" loading={busy} className="w-full">
            {busy ? null : created ? <ArrowRight aria-hidden /> : <FolderPlus aria-hidden />}
            {created
              ? audio
                ? "音源のアップロードを再試行"
                : "プロジェクトを開く"
              : audio
                ? "作成して音源をアップロード"
                : "プロジェクトを作成"}
          </Button>

          {phase === "uploading" ? (
            <Button type="button" variant="ghost" onClick={() => abortRef.current?.abort()}>
              アップロードを中止
            </Button>
          ) : created && audio ? (
            <Button
              type="button"
              variant="secondary"
              onClick={() => {
                const project = created;
                resetForm();
                router.push(`/projects/${project.id}`);
              }}
            >
              音源なしでプロジェクトを開く
            </Button>
          ) : (
            <Button type="button" variant="ghost" asChild className={cn(busy && "pointer-events-none opacity-45")}>
              <Link href="/">キャンセル</Link>
            </Button>
          )}
        </div>
      </div>
    </form>
  );
}
