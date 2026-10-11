"use client";

import { Clapperboard, Film, Info, X } from "lucide-react";
import Link from "next/link";
import * as React from "react";
import { useSWRConfig } from "swr";

import { projectSectionHref } from "@/components/layout/project-nav";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { FileDropzone } from "@/components/ui/file-dropzone";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { toast } from "@/components/ui/toast";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { useLimits } from "@/hooks/use-capabilities";
import { useClips } from "@/hooks/use-clips";
import { apiPaths, deleteClip, errorMessage, isAbortError, isListKey, uploadClips } from "@/lib/api";
import { MAX_CLIP_FILES_PER_UPLOAD, VIDEO_EXTENSIONS } from "@/lib/constants";
import { formatBytes, formatCount, formatDuration, formatPercent } from "@/lib/format";
import { isClipActive } from "@/lib/status";
import type { ClipOut, ProjectOut } from "@/lib/types";

import { ClipCard } from "./clip-card";
import { clipOrigin, type ClipOrigin } from "./clip-labels";
import { ClipPreviewDialog } from "./clip-preview-dialog";

type Filter = "all" | ClipOrigin;

interface UploadState {
  count: number;
  bytes: number;
  fraction: number;
}

function UploadCard({ projectId }: { projectId: string }) {
  const { mutate } = useSWRConfig();
  const limits = useLimits();
  const [upload, setUpload] = React.useState<UploadState | null>(null);
  const [problems, setProblems] = React.useState<string[]>([]);
  const controller = React.useRef<AbortController | null>(null);

  async function start(files: File[]) {
    setProblems([]);
    const abort = new AbortController();
    controller.current = abort;
    setUpload({ count: files.length, bytes: files.reduce((sum, file) => sum + file.size, 0), fraction: 0 });
    try {
      const created = await uploadClips(projectId, files, {
        signal: abort.signal,
        onProgress: ({ fraction }) => setUpload((current) => (current ? { ...current, fraction } : current)),
      });
      await mutate<ClipOut[]>(isListKey(apiPaths.clips(projectId)), (current) => [...created, ...(current ?? [])], { revalidate: true });
      void mutate(apiPaths.project(projectId));
      toast.success(`${created.length}本の素材を追加しました`);
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
        <CardTitle>素材をアップロード</CardTitle>
        <CardDescription>
          撮影済みの映像やフリー素材を追加します。1回に{MAX_CLIP_FILES_PER_UPLOAD}本まで。1本でも不正なファイルがあると、まとめて取り消されます。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {upload ? (
          <div className="flex flex-col gap-2 rounded-md border border-border bg-surface-raised p-3" aria-live="polite">
            <div className="flex items-center justify-between gap-3 text-xs">
              <span className="text-foreground">
                {upload.count}本（{formatBytes(upload.bytes)}）
              </span>
              <span className="text-muted-foreground tabular">
                {upload.fraction >= 1 ? "サーバーで検証・サムネイル作成中…" : formatPercent(upload.fraction)}
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
        ) : (
          <FileDropzone
            accept={VIDEO_EXTENSIONS}
            multiple
            maxFiles={MAX_CLIP_FILES_PER_UPLOAD}
            maxSizeBytes={limits.max_video_mb * 1024 * 1024}
            onFiles={(files) => void start(files)}
            onReject={setProblems}
            icon={Film}
            title="動画ファイルをドロップ"
            description={
              <>
                {VIDEO_EXTENSIONS.map((ext) => ext.toUpperCase()).join(" / ")}
                <br />1本 {limits.max_video_mb} MB まで ・ 1秒〜10分
                <br />縦長（9:16）推奨
              </>
            }
            buttonLabel="ファイルを選択"
            invalid={problems.length > 0}
          />
        )}
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

/**
 * Tab 「素材」: video materials (uploaded and generated) with upload, preview and delete. Cancelled clips
 * (generation stopped) are hidden unless 「キャンセル済みを表示」 is on.
 */
export function MaterialsPanel({ project }: { project: ProjectOut }) {
  const { mutate } = useSWRConfig();
  const [showCancelled, setShowCancelled] = React.useState(false);
  const { data: clips, error, isLoading, mutate: reload } = useClips(project.id, { include_cancelled: showCancelled ? undefined : false });
  const [filter, setFilter] = React.useState<Filter>("all");
  const [previewId, setPreviewId] = React.useState<string | null>(null);
  const [deleting, setDeleting] = React.useState<ClipOut | null>(null);

  const counts = React.useMemo(() => {
    const list = clips ?? [];
    const uploaded = list.filter((clip) => clipOrigin(clip) === "upload");
    return {
      all: list.length,
      upload: uploaded.length,
      generated: list.length - uploaded.length,
      readyUploads: uploaded.filter((clip) => clip.status === "ready").length,
      seconds: list.reduce((sum, clip) => sum + (clip.status === "ready" ? (clip.duration ?? 0) : 0), 0),
    };
  }, [clips]);

  const visible = React.useMemo(() => {
    const list = [...(clips ?? [])].sort((a, b) => b.created_at.localeCompare(a.created_at));
    return filter === "all" ? list : list.filter((clip) => clipOrigin(clip) === filter);
  }, [clips, filter]);

  const preview = previewId ? (clips?.find((clip) => clip.id === previewId) ?? null) : null;
  // Cancelled clips left out of the list (the project counts every clip; skipped while clips are in flux).
  const hiddenCancelled =
    !showCancelled && clips && !clips.some((clip) => isClipActive(clip.status)) ? Math.max(0, project.counts.clips - clips.length) : 0;

  async function confirmDelete() {
    if (!deleting) return;
    const target = deleting;
    try {
      await deleteClip(target.id);
      await mutate<ClipOut[]>(isListKey(apiPaths.clips(project.id)), (current) => current?.filter((clip) => clip.id !== target.id), {
        revalidate: false,
      });
      void mutate(apiPaths.project(project.id));
      if (previewId === target.id) setPreviewId(null);
      toast.success("素材を削除しました");
    } catch (deleteError) {
      toast.error("素材を削除できませんでした", errorMessage(deleteError));
      throw deleteError;
    }
  }

  let body: React.ReactNode;
  if (error && !clips) {
    body = <ErrorState error={error} onRetry={() => void reload()} />;
  } else if (isLoading || !clips) {
    body = (
      <div className="grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-4" aria-busy aria-label="素材を読み込み中">
        {Array.from({ length: 6 }, (_, index) => (
          <Skeleton key={index} className="aspect-[9/16] w-full" />
        ))}
      </div>
    );
  } else if (clips.length === 0 && hiddenCancelled > 0) {
    body = (
      <EmptyState
        title="表示できる素材はありません"
        description={`生成がキャンセルされた素材 ${formatCount(hiddenCancelled)} 本は非表示にしています。`}
        action={
          <Button variant="secondary" size="sm" onClick={() => setShowCancelled(true)}>
            キャンセル済みを表示
          </Button>
        }
      />
    );
  } else if (clips.length === 0) {
    body = (
      <EmptyState
        icon={Film}
        title="素材はまだありません"
        description="動画ファイルをアップロードするか、生成設定から AI で映像を生成すると、ここに一覧表示されます。"
        action={
          <Button variant="secondary" size="sm" asChild>
            <Link href={projectSectionHref(project.id, "generate")}>
              <Clapperboard aria-hidden />
              生成設定へ
            </Link>
          </Button>
        }
      />
    );
  } else if (visible.length === 0) {
    body = (
      <EmptyState
        size="sm"
        title={filter === "upload" ? "アップロードした素材はありません" : "生成されたクリップはありません"}
        description="フィルターを「すべて」に切り替えると、他の素材を表示します。"
      />
    );
  } else {
    body = (
      <div className="grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-x-4 gap-y-5">
        {visible.map((clip) => (
          <ClipCard key={clip.id} clip={clip} onOpen={(item) => setPreviewId(item.id)} onDelete={setDeleting} />
        ))}
      </div>
    );
  }

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(320px,360px)]">
      <div className="flex min-w-0 flex-col gap-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <ToggleGroup type="single" value={filter} onValueChange={(value) => value && setFilter(value as Filter)} aria-label="表示する素材">
            <ToggleGroupItem value="all" className="h-7 px-2.5 text-xs">
              すべて {counts.all}
            </ToggleGroupItem>
            <ToggleGroupItem value="upload" className="h-7 px-2.5 text-xs">
              アップロード {counts.upload}
            </ToggleGroupItem>
            <ToggleGroupItem value="generated" className="h-7 px-2.5 text-xs">
              生成 {counts.generated}
            </ToggleGroupItem>
          </ToggleGroup>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            {clips && clips.length > 0 ? (
              <span className="text-xs text-muted-foreground tabular">利用可能な素材の合計 {formatDuration(counts.seconds)}</span>
            ) : null}
            <div className="flex items-center gap-2">
              <Switch id="materials-show-cancelled" checked={showCancelled} onCheckedChange={setShowCancelled} />
              <Label htmlFor="materials-show-cancelled" className="text-xs font-normal text-muted-foreground">
                キャンセル済みを表示
              </Label>
              {hiddenCancelled > 0 ? (
                <span className="text-2xs text-subtle-foreground tabular">キャンセル済み {formatCount(hiddenCancelled)} 本を非表示</span>
              ) : null}
            </div>
          </div>
        </div>
        {body}
      </div>

      <aside className="flex min-w-0 flex-col gap-6">
        <Alert tone="neutral" icon={Info} title="素材の使われ方">
          <ul className="flex list-disc flex-col gap-1 pl-4">
            <li>
              アップロードした素材は、生成設定でプロバイダー「アップロード素材」を選ぶと、ビートに合わせて切り替わる映像として使われます（AI 生成の費用はかかりません）。
            </li>
            <li>AI で生成したクリップもここに追加され、同じ条件の次回以降の生成で再利用されます。</li>
            <li>「N本で使用」は、その素材を使っている動画の本数です（キャンセルした動画は数えません）。</li>
            <li>横長の素材は 9:16 に切り抜いて使われます。縦長の素材がおすすめです。</li>
          </ul>
          {counts.readyUploads === 0 && clips ? (
            <p className="mt-2 text-xs text-subtle-foreground">「アップロード素材」で生成するには、アップロード済みの素材が1本以上必要です。</p>
          ) : null}
        </Alert>
        <UploadCard projectId={project.id} />
      </aside>

      <ClipPreviewDialog clip={preview} onOpenChange={(open) => !open && setPreviewId(null)} onDelete={setDeleting} />
      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(open) => !open && setDeleting(null)}
        title="この素材を削除しますか？"
        description={
          deleting && deleting.used_in_videos > 0
            ? `この素材は${deleting.used_in_videos}本の動画で使われています。完成済みの動画はそのまま残りますが、再レンダリングや編集では使えなくなります。この操作は取り消せません。`
            : "ファイルも削除され、元に戻せません。"
        }
        confirmLabel="削除する"
        destructive
        onConfirm={confirmDelete}
      />
    </div>
  );
}
