"use client";

import { FileAudio, Music, Replace, X } from "lucide-react";
import * as React from "react";
import { useSWRConfig } from "swr";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { FileDropzone } from "@/components/ui/file-dropzone";
import { Progress } from "@/components/ui/progress";
import { toast } from "@/components/ui/toast";
import { useLimits } from "@/hooks/use-capabilities";
import { apiPaths, errorMessage, isAbortError, uploadAudio } from "@/lib/api";
import { AUDIO_EXTENSIONS, AUDIO_MAX_SECONDS, AUDIO_MIN_SECONDS } from "@/lib/constants";
import { formatBytes, formatDateTime, formatDuration, formatPercent } from "@/lib/format";
import type { ProjectOut } from "@/lib/types";

/** Duration from the file's metadata (best effort; `null` when the browser cannot decode it). */
function readLocalDuration(file: File): Promise<number | null> {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const audio = new Audio();
    audio.preload = "metadata";
    let timer = 0;
    const finish = (value: number | null) => {
      window.clearTimeout(timer);
      URL.revokeObjectURL(url);
      resolve(value);
    };
    timer = window.setTimeout(() => finish(null), 8000);
    audio.onloadedmetadata = () => finish(Number.isFinite(audio.duration) ? audio.duration : null);
    audio.onerror = () => finish(null);
    audio.src = url;
  });
}

function durationProblem(duration: number | null): string | null {
  if (duration === null) return null;
  if (duration < AUDIO_MIN_SECONDS) return `楽曲が短すぎます（${formatDuration(duration)}）。${AUDIO_MIN_SECONDS}秒以上の音源を選択してください。`;
  if (duration > AUDIO_MAX_SECONDS) return `楽曲が長すぎます（${formatDuration(duration)}）。15分以内の音源を選択してください。`;
  return null;
}

interface UploadState {
  name: string;
  size: number;
  fraction: number;
}

/** Upload the song, or show the current file with a "差し替え" flow. */
export function AudioUploadCard({ project }: { project: ProjectOut }) {
  const { mutate } = useSWRConfig();
  const limits = useLimits();
  const [replacing, setReplacing] = React.useState(false);
  const [upload, setUpload] = React.useState<UploadState | null>(null);
  const [problems, setProblems] = React.useState<string[]>([]);
  const controller = React.useRef<AbortController | null>(null);

  const audio = project.audio;
  const showDropzone = !audio || replacing;

  async function start(file: File) {
    setProblems([]);
    const problem = durationProblem(await readLocalDuration(file));
    if (problem) {
      setProblems([problem]);
      return;
    }
    const abort = new AbortController();
    controller.current = abort;
    setUpload({ name: file.name, size: file.size, fraction: 0 });
    try {
      const updated = await uploadAudio(project.id, file, {
        signal: abort.signal,
        onProgress: ({ fraction }) => setUpload((current) => (current ? { ...current, fraction } : current)),
      });
      await mutate(apiPaths.project(project.id), updated, { revalidate: false });
      void mutate(apiPaths.analysis(project.id));
      void mutate(apiPaths.projects);
      setReplacing(false);
      toast.success(audio ? "音源を差し替えました" : "音源をアップロードしました", "楽曲解析を開始しました。");
    } catch (error) {
      if (isAbortError(error)) toast({ title: "アップロードを中止しました" });
      else setProblems([errorMessage(error)]);
    } finally {
      controller.current = null;
      setUpload(null);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <Music className="size-4 text-muted-foreground" aria-hidden />
          楽曲ファイル
        </CardTitle>
        <CardDescription>
          {audio ? "解析と動画の音声に使う音源です。" : "音源をアップロードすると、BPM・構成・サビ候補を自動で解析します。"}
        </CardDescription>
        {audio && !replacing && !upload ? (
          <CardAction>
            <Button type="button" variant="secondary" size="sm" onClick={() => setReplacing(true)}>
              <Replace aria-hidden />
              差し替え
            </Button>
          </CardAction>
        ) : null}
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {audio ? (
          <div className="flex items-start gap-3 rounded-md border border-border bg-surface-raised p-3">
            <div className="flex size-9 shrink-0 items-center justify-center rounded-md border border-border bg-surface">
              <FileAudio className="size-4 text-muted-foreground" aria-hidden />
            </div>
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium text-foreground" title={audio.filename}>
                {audio.filename}
              </p>
              <p className="text-xs text-muted-foreground tabular">
                {formatDuration(audio.duration)} ・ {formatBytes(audio.size_bytes)} ・ {audio.content_type}
              </p>
              <p className="text-2xs text-subtle-foreground">アップロード: {formatDateTime(audio.created_at)}</p>
            </div>
          </div>
        ) : null}

        {upload ? (
          <div className="flex flex-col gap-2 rounded-md border border-border bg-surface-raised p-3" aria-live="polite">
            <div className="flex items-center justify-between gap-3 text-xs">
              <span className="min-w-0 truncate text-foreground">{upload.name}</span>
              <span className="shrink-0 text-muted-foreground tabular">
                {upload.fraction >= 1 ? "サーバーで検証中…" : `${formatPercent(upload.fraction)} ・ ${formatBytes(upload.size)}`}
              </span>
            </div>
            <Progress value={upload.fraction >= 1 ? null : upload.fraction * 100} aria-label="アップロードの進捗" />
            <div className="flex justify-end">
              <Button type="button" variant="ghost" size="xs" onClick={() => controller.current?.abort()} disabled={upload.fraction >= 1}>
                <X aria-hidden />
                中止
              </Button>
            </div>
          </div>
        ) : showDropzone ? (
          <>
            {audio ? (
              <Alert tone="warning">
                差し替えると楽曲解析をやり直します。生成中のジョブがある間は差し替えできません。歌詞と素材はそのまま残ります。
              </Alert>
            ) : null}
            <FileDropzone
              accept={AUDIO_EXTENSIONS}
              maxSizeBytes={limits.max_audio_mb * 1024 * 1024}
              onFiles={(files) => void start(files[0])}
              onReject={setProblems}
              title={audio ? "新しい音源をドロップ" : "楽曲ファイルをドロップ"}
              description={`${AUDIO_EXTENSIONS.map((ext) => ext.toUpperCase()).join(" / ")} ・ ${limits.max_audio_mb} MB まで ・ ${AUDIO_MIN_SECONDS}秒〜15分`}
              buttonLabel="ファイルを選択"
              invalid={problems.length > 0}
            />
            {audio ? (
              <div className="flex justify-end">
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setReplacing(false);
                    setProblems([]);
                  }}
                >
                  キャンセル
                </Button>
              </div>
            ) : null}
          </>
        ) : null}

        {problems.length > 0 ? (
          <Alert tone="danger" title="アップロードできませんでした">
            <ul className="flex flex-col gap-0.5">
              {problems.map((problem) => (
                <li key={problem}>{problem}</li>
              ))}
            </ul>
          </Alert>
        ) : null}
      </CardContent>
    </Card>
  );
}
