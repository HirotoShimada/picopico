"use client";

import { AudioLines, RefreshCw } from "lucide-react";
import * as React from "react";
import { useSWRConfig } from "swr";

import { ProjectInfoCard } from "@/components/project/project-info-card";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/ui/status-badge";
import { toast } from "@/components/ui/toast";
import { useAnalysis } from "@/hooks/use-analysis";
import { apiPaths, errorMessage, rerunAnalysis } from "@/lib/api";
import type { AnalysisOut, AnalysisStatus, ProjectOut } from "@/lib/types";

import { AnalysisSummary } from "./analysis-summary";
import { useAudioHotkeys } from "./audio-player";
import { AudioTransport } from "./audio-transport";
import { AudioUploadCard } from "./audio-upload-card";
import { SegmentEditor } from "./segment-editor";

function useRerun(projectId: string) {
  const { mutate } = useSWRConfig();
  const [pending, setPending] = React.useState(false);
  const run = React.useCallback(async () => {
    setPending(true);
    try {
      const result = await rerunAnalysis(projectId);
      await mutate<AnalysisOut>(
        apiPaths.analysis(projectId),
        (current) => (current ? { ...current, status: result.status, error: null } : current),
        { revalidate: true },
      );
      void mutate(apiPaths.project(projectId));
      toast.success("楽曲解析を再実行します");
    } catch (error) {
      toast.error("再解析を開始できませんでした", errorMessage(error));
    } finally {
      setPending(false);
    }
  }, [mutate, projectId]);
  return { run, pending };
}

function AnalysisInProgress({ status }: { status: AnalysisStatus }) {
  return (
    <Card>
      <CardContent className="flex flex-col gap-4 pt-5">
        <div className="flex items-center gap-3">
          <StatusBadge kind="analysis" status={status} />
          <p className="text-sm text-foreground">
            {status === "queued" ? "解析の順番を待っています…" : "BPM・拍・構成・サビ候補を解析しています…"}
          </p>
        </div>
        <Progress value={null} aria-label="楽曲解析中" />
        <p className="text-xs leading-5 text-muted-foreground">
          通常は数十秒で完了します。このページを開いたままにすると自動で結果が表示されます。解析中も歌詞・素材・権利確認の作業を進められます。
        </p>
        <AudioTransport />
      </CardContent>
    </Card>
  );
}

/** Keeps the song playable while there is no waveform (analysis pending, failed or unavailable). */
function PlaybackCard() {
  return (
    <Card>
      <CardContent className="flex flex-col gap-2 pt-4">
        <p className="text-xs text-muted-foreground">音源の再生</p>
        <AudioTransport />
      </CardContent>
    </Card>
  );
}

function AnalysisSkeleton() {
  return <Skeleton className="h-[420px] w-full" aria-label="解析結果を読み込み中" />;
}

interface LayoutProps {
  /** Full-width area above the columns (the waveform editor). */
  top?: React.ReactNode;
  main: React.ReactNode;
  /** Rendered under `main` in the left column. */
  extra?: React.ReactNode;
  aside: React.ReactNode;
}

function PanelLayout({ top, main, extra, aside }: LayoutProps) {
  return (
    <div className="flex flex-col gap-6">
      {top}
      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(320px,360px)]">
        <div className="flex min-w-0 flex-col gap-6">
          {main}
          {extra}
        </div>
        <aside className="flex min-w-0 flex-col gap-6">{aside}</aside>
      </div>
    </div>
  );
}

function AnalysisMain({ project, aside }: { project: ProjectOut; aside: React.ReactNode }) {
  const { data, error, isLoading, mutate } = useAnalysis(project.id);
  const rerun = useRerun(project.id);

  if (error && !data) {
    return (
      <PanelLayout
        main={
          <>
            <ErrorState error={error} title={error.isNotFound ? "解析結果を取得できませんでした" : undefined} onRetry={() => void mutate()} />
            <PlaybackCard />
          </>
        }
        aside={aside}
      />
    );
  }
  if (isLoading || !data) return <PanelLayout top={<AnalysisSkeleton />} main={<Skeleton className="h-48 w-full" />} aside={aside} />;

  const status = data.status;
  if (status === "queued" || status === "running") return <PanelLayout main={<AnalysisInProgress status={status} />} aside={aside} />;

  if (status === "failed") {
    return (
      <PanelLayout
        main={
          <Alert
            tone="danger"
            title="楽曲解析に失敗しました"
            action={
              <Button type="button" variant="secondary" size="sm" onClick={() => void rerun.run()} loading={rerun.pending}>
                {rerun.pending ? null : <RefreshCw aria-hidden />}
                再解析
              </Button>
            }
          >
            {data.error ?? project.analysis_error ?? "原因不明のエラーです。音源を差し替えるか、再解析をお試しください。"}
          </Alert>
        }
        aside={aside}
        extra={<PlaybackCard />}
      />
    );
  }

  if (status === "none" || !data.analysis) {
    return (
      <PanelLayout
        main={
          <EmptyState
            icon={AudioLines}
            title="まだ解析されていません"
            description="楽曲解析を実行すると、波形・BPM・構成・サビ候補が表示され、動画に使う区間を選べます。"
            action={
              <Button type="button" variant="primary" onClick={() => void rerun.run()} loading={rerun.pending}>
                解析を開始
              </Button>
            }
          />
        }
        aside={aside}
        extra={<PlaybackCard />}
      />
    );
  }

  return (
    <PanelLayout
      top={<SegmentEditor projectId={project.id} analysis={data.analysis} savedSegment={data.segment} />}
      main={
        <>
          <AnalysisSummary analysis={data.analysis} />
          <div className="flex justify-end">
            <Button type="button" variant="ghost" size="sm" onClick={() => void rerun.run()} loading={rerun.pending}>
              {rerun.pending ? null : <RefreshCw aria-hidden />}
              再解析
            </Button>
          </div>
        </>
      }
      aside={aside}
    />
  );
}

/** Tab 「楽曲解析」: waveform + segment selection (full width), analysis summary, audio file and project info. */
export function AnalysisPanel({ project }: { project: ProjectOut }) {
  useAudioHotkeys();

  if (!project.audio) {
    return <PanelLayout main={<AudioUploadCard project={project} />} aside={<ProjectInfoCard project={project} />} />;
  }
  return (
    <AnalysisMain
      project={project}
      aside={
        <>
          <AudioUploadCard project={project} />
          <ProjectInfoCard project={project} />
        </>
      }
    />
  );
}
