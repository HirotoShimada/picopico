import type { AnalysisStatus, ProjectOut } from "@/lib/types";

/** What the workspace has prepared for generation (shown on the tabs and the 生成設定 link). */
export interface WorkspaceReadiness {
  hasAudio: boolean;
  analysisStatus: AnalysisStatus;
  hasSegment: boolean;
  lyricLines: number;
  lyricTimed: number;
  lyricsDirty: boolean;
  clips: number;
  rightsConfirmed: boolean;
  /** Short names of checklist items un-confirmed by a song / lyric text change (「音源の権利」…). */
  rightsToRecheck: string[];
}

export function projectReadiness(
  project: ProjectOut,
  lyrics: { lines: number; timed: number; dirty: boolean },
  hasSegment: boolean,
  rightsToRecheck: string[] = [],
): WorkspaceReadiness {
  return {
    hasAudio: project.audio !== null,
    analysisStatus: project.analysis_status,
    hasSegment,
    lyricLines: lyrics.lines,
    lyricTimed: lyrics.timed,
    lyricsDirty: lyrics.dirty,
    clips: project.counts.clips,
    rightsConfirmed: project.rights_confirmed_at !== null,
    rightsToRecheck,
  };
}

/** Preconditions of `POST /batches` (§5.6) that are not met yet, in Japanese. */
export function generationBlockers(readiness: WorkspaceReadiness): string[] {
  const blockers: string[] = [];
  if (!readiness.hasAudio) blockers.push("音源をアップロードしてください");
  else if (readiness.analysisStatus !== "done") blockers.push("楽曲解析の完了を待ってください");
  if (readiness.rightsToRecheck.length > 0) {
    blockers.push(`音源・歌詞が変わったため、権利確認の${readiness.rightsToRecheck.map((name) => `「${name}」`).join("")}を確認し直してください`);
  } else if (!readiness.rightsConfirmed) {
    blockers.push("権利確認の5項目をすべて確認してください");
  }
  return blockers;
}

/** Recommended but optional steps. */
export function generationHints(readiness: WorkspaceReadiness): string[] {
  const hints: string[] = [];
  if (readiness.lyricLines === 0) hints.push("歌詞が未入力です（歌詞なしの動画になります）");
  else if (readiness.lyricTimed < readiness.lyricLines) hints.push(`歌詞のタイミングが未設定の行があります（${readiness.lyricLines - readiness.lyricTimed}行）`);
  if (readiness.lyricsDirty) hints.push("歌詞タイミングに未保存の変更があります");
  return hints;
}
