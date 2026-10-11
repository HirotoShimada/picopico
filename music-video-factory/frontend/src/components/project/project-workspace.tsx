"use client";

import { AudioLines, CircleAlert, CircleCheck, Clapperboard, FileText, Film, LoaderCircle, ShieldCheck, TriangleAlert } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import * as React from "react";

import { AnalysisPanel } from "@/components/audio/analysis-panel";
import { AudioPlayerProvider } from "@/components/audio/audio-player";
import { ProjectHeader } from "@/components/layout/project-header";
import { projectSectionHref } from "@/components/layout/project-nav";
import { LyricsPanel } from "@/components/lyrics/lyrics-panel";
import { countTimed } from "@/components/lyrics/timing";
import { useLyricsDraft } from "@/components/lyrics/use-lyrics-draft";
import { MaterialsPanel } from "@/components/materials/materials-panel";
import { RightsPanel } from "@/components/rights/rights-panel";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useAnalysis } from "@/hooks/use-analysis";
import { useLyrics } from "@/hooks/use-lyrics";
import { useProject } from "@/hooks/use-projects";
import { isAnalysisActive } from "@/lib/status";
import type { Segment } from "@/lib/types";

import { generationBlockers, generationHints, projectReadiness, type WorkspaceReadiness } from "./readiness";
import { WorkspaceBodySkeleton } from "./workspace-skeleton";
import { parseWorkspaceTab, WORKSPACE_TABS, type WorkspaceTab } from "./workspace-tabs";

const MemoAnalysisPanel = React.memo(AnalysisPanel);
const MemoMaterialsPanel = React.memo(MaterialsPanel);
const MemoRightsPanel = React.memo(RightsPanel);

function StatusIcon({ tone, label }: { tone: "done" | "busy" | "error" | "warning"; label: string }) {
  const Icon = tone === "done" ? CircleCheck : tone === "busy" ? LoaderCircle : tone === "error" ? CircleAlert : TriangleAlert;
  const color =
    tone === "done" ? "text-success" : tone === "busy" ? "animate-spin text-primary" : tone === "error" ? "text-danger" : "text-warning";
  return (
    <>
      <Icon className={`size-3.5 ${color}`} aria-hidden />
      <span className="sr-only">（{label}）</span>
    </>
  );
}

function CountBadge({ children }: { children: React.ReactNode }) {
  return <span className="rounded bg-surface-raised px-1.5 text-2xs leading-4 text-muted-foreground tabular">{children}</span>;
}

function TabStatus({ tab, readiness }: { tab: WorkspaceTab; readiness: WorkspaceReadiness }) {
  switch (tab) {
    case "analysis":
      if (!readiness.hasAudio) return null;
      if (isAnalysisActive(readiness.analysisStatus)) return <StatusIcon tone="busy" label="解析中" />;
      if (readiness.analysisStatus === "failed") return <StatusIcon tone="error" label="解析失敗" />;
      if (readiness.analysisStatus === "done") return <StatusIcon tone="done" label="解析済み" />;
      return null;
    case "lyrics":
      if (readiness.lyricsDirty) return <StatusIcon tone="warning" label="未保存の変更あり" />;
      if (readiness.lyricLines === 0) return null;
      if (readiness.lyricTimed === readiness.lyricLines) return <StatusIcon tone="done" label="全行タイミング済み" />;
      return (
        <CountBadge>
          {readiness.lyricTimed}/{readiness.lyricLines}
        </CountBadge>
      );
    case "materials":
      return readiness.clips > 0 ? <CountBadge>{readiness.clips}</CountBadge> : null;
    case "rights":
      return readiness.rightsConfirmed ? <StatusIcon tone="done" label="確認済み" /> : <StatusIcon tone="warning" label="未確認" />;
  }
}

const TAB_META: Record<WorkspaceTab, { label: string; icon: React.ComponentType<{ className?: string }> }> = {
  analysis: { label: "楽曲解析", icon: AudioLines },
  lyrics: { label: "歌詞", icon: FileText },
  materials: { label: "素材", icon: Film },
  rights: { label: "権利確認", icon: ShieldCheck },
};

function GenerateLink({ projectId, readiness }: { projectId: string; readiness: WorkspaceReadiness }) {
  const blockers = generationBlockers(readiness);
  const hints = generationHints(readiness);
  const notes = [...blockers, ...hints];
  // Once nothing blocks generation, going to the generate page is the next step (primary).
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button variant={blockers.length === 0 ? "primary" : "outline"} size="sm" asChild>
          <Link href={projectSectionHref(projectId, "generate")}>
            <Clapperboard aria-hidden />
            生成設定へ
            {blockers.length > 0 ? <span className="text-2xs font-normal text-warning-foreground">（準備中 {blockers.length}件）</span> : null}
          </Link>
        </Button>
      </TooltipTrigger>
      <TooltipContent align="end" className="max-w-sm">
        {notes.length === 0 ? (
          <p>準備が整っています。生成設定で本数・長さ・プロバイダーを選んで生成を開始できます。</p>
        ) : (
          <ul className="flex list-disc flex-col gap-0.5 pl-4">
            {notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        )}
      </TooltipContent>
    </Tooltip>
  );
}

/** Warn before a reload/close while lyric timing edits are unsaved. */
function useUnsavedChangesGuard(dirty: boolean) {
  React.useEffect(() => {
    if (!dirty) return;
    const handler = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty]);
}

/**
 * `/projects/[id]` workspace: project header + sub-nav and the tabs
 * 楽曲解析 / 歌詞 / 素材 / 権利確認. One audio player is shared by all tabs, and
 * hidden tabs keep their state (React `<Activity>`). The lyric timing draft lives
 * here so unsaved edits survive tab switches and are guarded on reload.
 */
export function ProjectWorkspace({ projectId }: { projectId: string }) {
  const { data: project, error, mutate } = useProject(projectId);
  const searchParams = useSearchParams();
  const tab = parseWorkspaceTab(searchParams.get("tab"));

  const { data: lyrics } = useLyrics(project ? projectId : null);
  const { data: analysisOut } = useAnalysis(project?.audio ? projectId : null);
  const analysis = analysisOut?.analysis ?? null;
  const duration = analysis?.duration ?? project?.audio?.duration ?? null;
  const draft = useLyricsDraft(projectId, lyrics?.lines, duration);
  useUnsavedChangesGuard(draft.dirty);

  const segment: Segment | null =
    analysisOut?.segment ??
    (project && project.segment_start !== null && project.segment_end !== null ? { start: project.segment_start, end: project.segment_end } : null);
  const analysisReady = (analysisOut?.status ?? project?.analysis_status) === "done" && analysis !== null;

  const readiness = project
    ? projectReadiness(project, { lines: draft.lines.length, timed: countTimed(draft.lines), dirty: draft.dirty }, segment !== null)
    : null;

  // The analysis endpoint polls faster than the project; when it reports the end of the
  // analysis, refresh the project at once so the header badge and tab icons stop spinning.
  const analysisStatus = analysisOut?.status;
  const projectAnalysisStatus = project?.analysis_status;
  React.useEffect(() => {
    if (!analysisStatus || !projectAnalysisStatus) return;
    if (!isAnalysisActive(analysisStatus) && isAnalysisActive(projectAnalysisStatus)) void mutate();
  }, [analysisStatus, projectAnalysisStatus, mutate]);

  const reloadAudioUrl = React.useCallback(() => void mutate(), [mutate]);

  function changeTab(value: string) {
    const next = parseWorkspaceTab(value);
    const params = new URLSearchParams(searchParams.toString());
    if (next === "analysis") params.delete("tab");
    else params.set("tab", next);
    const query = params.toString();
    // Native history updates are integrated with the App Router (useSearchParams stays in sync).
    window.history.replaceState(null, "", query ? `?${query}` : window.location.pathname);
  }

  return (
    <AudioPlayerProvider src={project?.audio?.url ?? null} sourceKey={project?.audio?.id ?? null} onSourceError={reloadAudioUrl}>
      <ProjectHeader
        projectId={projectId}
        active="assets"
        actions={project && readiness ? <GenerateLink projectId={project.id} readiness={readiness} /> : undefined}
      />
      {error && !project ? null : !project || !readiness ? (
        <WorkspaceBodySkeleton />
      ) : (
        <Tabs value={tab} onValueChange={changeTab}>
          <TabsList aria-label="プロジェクトの作業" className="h-10">
            {WORKSPACE_TABS.map((id) => {
              const Icon = TAB_META[id].icon;
              return (
                <TabsTrigger key={id} value={id} className="gap-2 px-3.5">
                  <Icon aria-hidden />
                  {TAB_META[id].label}
                  <TabStatus tab={id} readiness={readiness} />
                </TabsTrigger>
              );
            })}
          </TabsList>

          <TabsContent value="analysis" forceMount className="data-[state=inactive]:hidden">
            <React.Activity mode={tab === "analysis" ? "visible" : "hidden"}>
              <MemoAnalysisPanel project={project} />
            </React.Activity>
          </TabsContent>
          <TabsContent value="lyrics" forceMount className="data-[state=inactive]:hidden">
            <React.Activity mode={tab === "lyrics" ? "visible" : "hidden"}>
              <LyricsPanel project={project} draft={draft} duration={duration} analysisReady={analysisReady} segment={segment} />
            </React.Activity>
          </TabsContent>
          <TabsContent value="materials" forceMount className="data-[state=inactive]:hidden">
            <React.Activity mode={tab === "materials" ? "visible" : "hidden"}>
              <MemoMaterialsPanel project={project} />
            </React.Activity>
          </TabsContent>
          <TabsContent value="rights" forceMount className="data-[state=inactive]:hidden">
            <React.Activity mode={tab === "rights" ? "visible" : "hidden"}>
              <MemoRightsPanel project={project} />
            </React.Activity>
          </TabsContent>
        </Tabs>
      )}
    </AudioPlayerProvider>
  );
}
