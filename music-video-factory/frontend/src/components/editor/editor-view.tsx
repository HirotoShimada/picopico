"use client";

import { Download, Film, Undo2, Wand2 } from "lucide-react";
import Link from "next/link";
import * as React from "react";
import { useSWRConfig } from "swr";

import { ColorGradeSelect } from "@/components/generate/color-grade-select";
import { galleryHref } from "@/components/generate/links";
import { LyricStylePicker } from "@/components/generate/lyric-style-picker";
import { LyricStyleSample } from "@/components/generate/lyric-style-sample";
import { TextStyleControls } from "@/components/generate/text-style-controls";
import { ProjectHeader } from "@/components/layout/project-header";
import { QualitySummary } from "@/components/videos/quality-summary";
import { rerenderErrorMessage, useVideoActions, videoTitle } from "@/components/videos/video-actions";
import { VideoPlayer } from "@/components/videos/video-player";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ErrorState } from "@/components/ui/error-state";
import { Field } from "@/components/ui/field";
import { isRunway, PoweredByRunway } from "@/components/ui/powered-by-runway";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/ui/status-badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { toast } from "@/components/ui/toast";
import { usePresets } from "@/hooks/use-capabilities";
import { useProject } from "@/hooks/use-projects";
import { useVideo } from "@/hooks/use-videos";
import { apiPaths, editVideo } from "@/lib/api";
import { formatDateTime, formatDuration, formatPercent } from "@/lib/format";
import { isVideoActive } from "@/lib/status";
import type { LyricOffset, VideoDetailOut } from "@/lib/types";

import { ClipPickerDialog } from "./clip-picker-dialog";
import { applyDraft, baseOf, draftToEdit, EMPTY_DRAFT, linesInWindow, normalizeDraft, summarizeDraft, type EditDraft } from "./edit-draft";
import { LyricTimingEditor } from "./lyric-timing-editor";
import { ShotList } from "./shot-list";
import { StartPositionEditor } from "./start-position-editor";

type EditorTab = "lyrics" | "style" | "look" | "shots";

export function EditorSkeleton() {
  return (
    <div className="flex flex-col gap-6" aria-busy aria-label="読み込み中">
      <div className="flex flex-col gap-3">
        <Skeleton className="h-3.5 w-24" />
        <Skeleton className="h-7 w-72" />
      </div>
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(260px,340px)_minmax(0,1fr)]">
        <Skeleton className="aspect-[9/16] w-full" />
        <Skeleton className="h-[480px] w-full" />
      </div>
    </div>
  );
}

/** `/videos/[id]/edit`: load the video and its project, then the editor. */
export function EditorView({ videoId }: { videoId: string }) {
  const { data: video, error, mutate } = useVideo(videoId);

  if (error && !video) {
    return (
      <ErrorState
        error={error}
        title={error.isNotFound ? "動画が見つかりません" : undefined}
        onRetry={error.isNotFound ? undefined : () => void mutate()}
      />
    );
  }
  if (!video) return <EditorSkeleton />;
  return <VideoEditor video={video} />;
}

function VideoEditor({ video }: { video: VideoDetailOut }) {
  const { data: project } = useProject(video.project_id);
  const { presets } = usePresets();
  const { mutate } = useSWRConfig();
  const actions = useVideoActions();

  const [rawDraft, setRawDraft] = React.useState<EditDraft>(EMPTY_DRAFT);
  const [tab, setTab] = React.useState<EditorTab>("lyrics");
  const [playerTime, setPlayerTime] = React.useState<number | null>(null);
  const [pickerShot, setPickerShot] = React.useState<number | null>(null);
  const [saving, setSaving] = React.useState(false);
  const [saveError, setSaveError] = React.useState<unknown>(null);
  const playerRef = React.useRef<HTMLVideoElement | null>(null);

  // The ref cleanup also runs when the route is hidden (Cache Components keep it
  // mounted inside <Activity>), so a playing preview never keeps sounding.
  const attachPlayer = React.useCallback((element: HTMLVideoElement | null) => {
    playerRef.current = element;
    if (!element) return;
    return () => {
      element.pause();
      if (playerRef.current === element) playerRef.current = null;
    };
  }, []);

  // Close the clip picker when the route is hidden.
  React.useLayoutEffect(() => () => setPickerShot(null), []);

  const draft = React.useMemo(() => normalizeDraft(video, rawDraft), [video, rawDraft]);
  const base = baseOf(video);
  const effective = applyDraft(video, draft);
  const changes = summarizeDraft(draft);
  const rendering = isVideoActive(video.status);
  const songDuration = project?.analysis?.duration ?? project?.audio?.duration ?? null;
  const clipsById = React.useMemo(() => new Map(video.available_clips.map((clip) => [clip.id, clip])), [video.available_clips]);
  // The current render (or the pending edit) contains Runway footage → attribution.
  const usesRunway = [...base.shots, ...effective.shots].some((shot) => isRunway(clipsById.get(shot.clip_id ?? "")?.source));
  const sampleLine = linesInWindow(effective.lines, effective.start, effective.duration)[0]?.text;
  const substituted = base.shots.filter((shot) => shot.substituted_from).length;
  const palette = presets.find((preset) => preset.id === project?.world_preset)?.palette;

  const updateDraft = React.useCallback((patch: (current: EditDraft) => EditDraft) => setRawDraft((current) => patch(current)), []);

  function setLineTiming(lineId: string, timing: LyricOffset) {
    updateDraft((current) => ({ ...current, lyricTimes: { ...current.lyricTimes, [lineId]: timing } }));
  }

  function resetLine(lineId: string) {
    updateDraft((current) => {
      const lyricTimes = { ...current.lyricTimes };
      delete lyricTimes[lineId];
      return { ...current, lyricTimes };
    });
  }

  function setShotClip(index: number, clipId: string) {
    updateDraft((current) => ({ ...current, shots: { ...current.shots, [index]: clipId } }));
  }

  function resetShot(index: number) {
    updateDraft((current) => {
      const shots = { ...current.shots };
      delete shots[index];
      return { ...current, shots };
    });
  }

  function seek(seconds: number) {
    const player = playerRef.current;
    if (!player) return;
    player.currentTime = Math.max(0, seconds);
    void player.play().catch(() => undefined);
  }

  async function rerender() {
    if (changes.count === 0 || rendering) return;
    setSaving(true);
    setSaveError(null);
    try {
      const updated = await editVideo(video.id, draftToEdit(draft));
      await mutate(apiPaths.video(video.id), (current: VideoDetailOut | undefined) => (current ? { ...current, ...updated } : current), {
        revalidate: true,
      });
      setRawDraft(EMPTY_DRAFT);
      void actions.revalidate(video);
      toast.success("再レンダリングを開始しました", `完了するとバージョン ${updated.render_version} に置き換わります。`);
    } catch (error) {
      setSaveError(error);
    } finally {
      setSaving(false);
    }
  }

  const busy = actions.pending[video.id];

  return (
    <>
      <ProjectHeader
        projectId={video.project_id}
        active="videos"
        actions={
          <>
            <Button variant="ghost" asChild>
              <Link href={galleryHref(video.project_id, video.batch_id)}>
                <Film aria-hidden />
                動画一覧へ
              </Link>
            </Button>
            <Button
              variant="secondary"
              onClick={() => void actions.download(video)}
              disabled={video.status !== "done" || !!busy}
              loading={busy === "download"}
            >
              {busy === "download" ? null : <Download aria-hidden />}
              ダウンロード
            </Button>
          </>
        }
      />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(260px,340px)_minmax(0,1fr)]">
        <aside className="flex min-w-0 flex-col gap-4 lg:sticky lg:top-20 lg:max-h-[calc(100vh-6rem)] lg:self-start lg:overflow-y-auto">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="truncate text-base font-semibold text-foreground">{videoTitle(video)}</h2>
            <StatusBadge kind="video" status={video.status} />
            <span className="rounded border border-border px-1.5 font-mono text-2xs leading-4 text-muted-foreground" title="レンダリングのバージョン">
              v{video.render_version}
            </span>
          </div>
          <VideoPlayer
            video={video}
            videoRef={attachPlayer}
            onTimeUpdate={setPlayerTime}
            onReload={() => void mutate(apiPaths.video(video.id))}
          />
          {rendering ? (
            <div className="flex flex-col gap-1.5">
              <div className="flex justify-between text-xs">
                <span className="text-muted-foreground">
                  {video.status === "rendering" ? `バージョン ${video.render_version} をレンダリング中` : "レンダリングの順番待ち"}
                </span>
                <span className="font-mono text-foreground tabular">{video.status === "rendering" ? formatPercent(video.progress) : ""}</span>
              </div>
              <Progress value={video.status === "rendering" ? video.progress * 100 : null} aria-label="レンダリング進捗" />
            </div>
          ) : (
            <p className="text-xs text-muted-foreground tabular">
              {video.rendered_at ? `${formatDateTime(video.rendered_at)} にレンダリング` : "未レンダリング"}
              {` ・ ${formatDuration(base.duration)}`}
            </p>
          )}
          {video.status === "failed" && video.error ? (
            <Alert tone="danger" title="レンダリングに失敗しました">
              {video.error}
            </Alert>
          ) : null}
          {video.status === "done" && video.error ? (
            <Alert tone="warning" title="前回の動画を表示しています">
              {video.error}
            </Alert>
          ) : null}
          {substituted > 0 && !rendering ? (
            <Alert tone="neutral" title={`代替素材 ${substituted} ショット`}>
              予定していた素材を生成できなかったショットは、同じ生成ジョブの別の素材で補っています。「ショット」タブで差し替えできます。
            </Alert>
          ) : null}
          {usesRunway ? <PoweredByRunway /> : null}
          <details className="rounded-md border border-border bg-surface px-3.5 py-2.5">
            <summary className="cursor-pointer text-xs font-medium text-foreground select-none">品質チェック</summary>
            <QualitySummary report={video.quality} className="mt-3" />
          </details>
        </aside>

        <div className="flex min-w-0 flex-col gap-4">
          <Card>
            <CardContent className="flex flex-wrap items-center justify-between gap-3 pt-4 pb-4">
              <div className="flex min-w-0 flex-1 basis-64 flex-col gap-0.5">
                <span className="text-sm font-medium text-foreground">
                  {changes.count > 0 ? `未適用の変更 ${changes.count} 件` : "変更はありません"}
                </span>
                <span className="truncate text-xs text-muted-foreground">
                  {changes.count > 0
                    ? changes.labels.join("、")
                    : "下のタブで歌詞のタイミング・スタイル・色・開始位置・ショットを調整し、再レンダリングで反映します。"}
                </span>
              </div>
              <div className="flex items-center gap-2">
                <Button variant="ghost" onClick={() => setRawDraft(EMPTY_DRAFT)} disabled={changes.count === 0 || saving}>
                  <Undo2 aria-hidden />
                  変更を破棄
                </Button>
                <Button variant="primary" onClick={() => void rerender()} disabled={changes.count === 0 || rendering} loading={saving}>
                  {saving ? null : <Wand2 aria-hidden />}
                  再レンダリング
                </Button>
              </div>
              {rendering && changes.count > 0 ? (
                <p className="w-full text-2xs text-subtle-foreground">現在のレンダリングが終わると適用できます。</p>
              ) : null}
              {saveError ? (
                <Alert tone="danger" title="再レンダリングを開始できませんでした" className="w-full">
                  {rerenderErrorMessage(saveError)}
                </Alert>
              ) : null}
            </CardContent>
          </Card>

          <Tabs value={tab} onValueChange={(value) => setTab(value as EditorTab)}>
            <TabsList variant="line">
              <TabsTrigger value="lyrics">歌詞タイミング</TabsTrigger>
              <TabsTrigger value="style">歌詞スタイル</TabsTrigger>
              <TabsTrigger value="look">色・開始位置</TabsTrigger>
              <TabsTrigger value="shots">ショット</TabsTrigger>
            </TabsList>

            <TabsContent value="lyrics">
              <LyricTimingEditor
                projectId={video.project_id}
                lines={effective.lines}
                baseLines={base.lines}
                windowStart={effective.start}
                duration={effective.duration}
                renderStart={base.start}
                playerTime={playerTime}
                onChange={setLineTiming}
                onReset={resetLine}
                onSeek={seek}
              />
            </TabsContent>

            <TabsContent value="style" className="flex flex-col gap-5">
              <LyricStylePicker
                value={effective.lyricStyle}
                onValueChange={(lyricStyle) => updateDraft((current) => ({ ...current, lyricStyle }))}
                textStyle={effective.textStyle}
                palette={palette}
                className="2xl:grid-cols-5"
              />
              <Card>
                <CardHeader>
                  <CardTitle>文字の設定</CardTitle>
                  <CardDescription>未変更の項目はスタイルの既定値を使います。</CardDescription>
                </CardHeader>
                <CardContent className="grid grid-cols-1 gap-6 md:grid-cols-[minmax(0,1fr)_168px]">
                  <TextStyleControls
                    idPrefix="editor-text"
                    styleId={effective.lyricStyle}
                    value={effective.textStyle}
                    onValueChange={(textStyle) => updateDraft((current) => ({ ...current, textStyle }))}
                  />
                  <div className="flex flex-col gap-1.5">
                    <span className="text-xs text-muted-foreground">配置プレビュー</span>
                    <LyricStyleSample
                      variant="frame"
                      showSafeArea
                      styleId={effective.lyricStyle}
                      textStyle={effective.textStyle}
                      text={sampleLine}
                    />
                    <span className="text-2xs leading-4 text-subtle-foreground">近似表示です。正確な見た目は再レンダリング後に確認してください。</span>
                  </div>
                </CardContent>
              </Card>
            </TabsContent>

            <TabsContent value="look" className="flex flex-col gap-4">
              <Card>
                <CardHeader>
                  <CardTitle>カラーグレード</CardTitle>
                </CardHeader>
                <CardContent>
                  <Field label="グレード" htmlFor="editor-color-grade" className="max-w-sm">
                    <ColorGradeSelect
                      id="editor-color-grade"
                      value={effective.colorGrade}
                      onValueChange={(colorGrade) => updateDraft((current) => ({ ...current, colorGrade }))}
                    />
                  </Field>
                </CardContent>
              </Card>
              <Card>
                <CardHeader>
                  <CardTitle>開始位置</CardTitle>
                  <CardDescription>曲のどこから使うかを変更します。</CardDescription>
                </CardHeader>
                <CardContent>
                  <StartPositionEditor
                    analysis={project?.analysis ?? null}
                    songDuration={songDuration}
                    start={effective.start}
                    renderStart={base.start}
                    duration={effective.duration}
                    onChange={(start) => updateDraft((current) => ({ ...current, start }))}
                  />
                </CardContent>
              </Card>
            </TabsContent>

            <TabsContent value="shots" className="flex flex-col gap-3">
              <p className="text-xs text-muted-foreground">
                ショットごとに素材を差し替えられます。サムネイルを押すと、現在のレンダリングでそのショットから再生します。
              </p>
              <ShotList
                shots={effective.shots}
                baseShots={base.shots}
                clipsById={clipsById}
                playerTime={playerTime}
                onSwap={setPickerShot}
                onReset={resetShot}
                onSeek={seek}
              />
            </TabsContent>
          </Tabs>
        </div>
      </div>

      <ClipPickerDialog
        open={pickerShot !== null}
        onOpenChange={(open) => !open && setPickerShot(null)}
        shotIndex={pickerShot}
        shot={pickerShot !== null ? (effective.shots[pickerShot] ?? null) : null}
        shots={effective.shots}
        clips={video.available_clips}
        onPick={(clipId) => pickerShot !== null && setShotClip(pickerShot, clipId)}
      />
    </>
  );
}
