"use client";

import { AudioWaveform, Wand2 } from "lucide-react";
import * as React from "react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { toast } from "@/components/ui/toast";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { SimpleTooltip } from "@/components/ui/tooltip";
import { useCapabilities } from "@/hooks/use-capabilities";
import { autoTimeLyrics, errorMessage } from "@/lib/api";
import { formatTime } from "@/lib/format";
import type { AutoTimeMethod, AutoTimeOut, LyricLine, Segment } from "@/lib/types";

import { linesEqual } from "./timing";

export interface AutoSyncBarProps {
  projectId: string;
  lines: readonly LyricLine[];
  /** Analysis is done (heuristic timing needs vocal activity / beats). */
  analysisReady: boolean;
  segment: Segment | null;
  /** Unsaved edits are saved before the server re-times the lines. */
  dirty: boolean;
  saveDraft: () => Promise<unknown>;
  /** Persist and adopt the auto-timed lines (undoable). */
  commit: (lines: LyricLine[]) => Promise<unknown>;
}

type Scope = "song" | "segment";

/** Heuristic / Whisper auto-timing (`POST /lyrics/auto-time`). Line text is never changed. */
export function AutoSyncBar({ projectId, lines, analysisReady, segment, dirty, saveDraft, commit }: AutoSyncBarProps) {
  const { data: capabilities, error: capabilitiesError, isLoading } = useCapabilities();
  const [scope, setScope] = React.useState<Scope>("song");
  const [overwrite, setOverwrite] = React.useState(false);
  const [running, setRunning] = React.useState<AutoTimeMethod | null>(null);
  const [result, setResult] = React.useState<AutoTimeOut | null>(null);

  const noLines = lines.length === 0;
  const blockedReason = noLines
    ? "先に歌詞行を作成してください"
    : !analysisReady
      ? "楽曲解析が完了すると使えます"
      : null;

  let whisperReason: string | null = null;
  if (isLoading) whisperReason = "利用可否を確認しています…";
  else if (capabilitiesError || !capabilities) whisperReason = "利用可否を取得できませんでした";
  else if (!capabilities.whisper.available) whisperReason = capabilities.whisper.reason || "Whisper（音声認識）はこのサーバーでは利用できません";

  const effectiveScope = scope === "segment" && !segment ? "song" : scope;

  async function run(method: AutoTimeMethod) {
    setRunning(method);
    setResult(null);
    try {
      if (dirty) await saveDraft();
      const response = await autoTimeLyrics(projectId, {
        method,
        start: effectiveScope === "segment" && segment ? segment.start : null,
        end: effectiveScope === "segment" && segment ? segment.end : null,
        overwrite_timed: overwrite,
      });
      setResult(response);
      if (linesEqual(response.lines, lines)) {
        // Nothing to apply: no save, no undo step, no success claim.
        toast({ title: "変更はありませんでした", description: "「タイミング設定済みの行も上書きする」をオンにすると再推定します。" });
        return;
      }
      await commit(response.lines);
      toast.success(
        response.method_used === "whisper" ? "Whisper でタイミングを合わせました" : "タイミングを自動で推定しました",
        "結果を再生して確認し、必要に応じて調整してください。元に戻すこともできます。",
      );
    } catch (error) {
      toast.error("自動タイミングに失敗しました", errorMessage(error));
    } finally {
      setRunning(null);
    }
  }

  return (
    <div className="flex flex-col gap-3 rounded-md border border-border bg-surface-raised/40 p-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <span className="text-xs font-medium text-foreground">自動タイミング</span>
        <ToggleGroup
          type="single"
          value={effectiveScope}
          onValueChange={(value) => value && setScope(value as Scope)}
          aria-label="対象範囲"
          className="p-0.5"
        >
          <ToggleGroupItem value="song" className="h-7 px-2.5 text-xs">
            曲全体
          </ToggleGroupItem>
          <ToggleGroupItem value="segment" className="h-7 px-2.5 text-xs" disabled={!segment}>
            {segment ? `選択区間 ${formatTime(segment.start)}–${formatTime(segment.end)}` : "選択区間（未設定）"}
          </ToggleGroupItem>
        </ToggleGroup>
        <div className="flex items-center gap-2">
          <Checkbox id="auto-overwrite" checked={overwrite} onCheckedChange={(checked) => setOverwrite(checked === true)} />
          <Label htmlFor="auto-overwrite" className="text-xs font-normal text-muted-foreground">
            タイミング設定済みの行も上書きする
          </Label>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <SimpleTooltip content={blockedReason ?? "音量・ボーカルらしさ・拍から各行の開始位置を推定します"}>
            <span tabIndex={blockedReason ? 0 : -1} className="inline-flex rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring/40">
              <Button
                type="button"
                variant="secondary"
                size="sm"
                disabled={!!blockedReason || running !== null}
                loading={running === "heuristic"}
                onClick={() => void run("heuristic")}
              >
                {running === "heuristic" ? null : <Wand2 aria-hidden />}
                推定で合わせる
              </Button>
            </span>
          </SimpleTooltip>
          <SimpleTooltip content={blockedReason ?? whisperReason ?? "音声認識の単語タイミングに歌詞を整列します（歌詞は変更しません）"}>
            <span
              tabIndex={blockedReason || whisperReason ? 0 : -1}
              className="inline-flex rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring/40"
            >
              <Button
                type="button"
                variant="secondary"
                size="sm"
                disabled={!!blockedReason || !!whisperReason || running !== null}
                loading={running === "whisper"}
                onClick={() => void run("whisper")}
              >
                {running === "whisper" ? null : <AudioWaveform aria-hidden />}
                Whisper で整列
              </Button>
            </span>
          </SimpleTooltip>
        </div>
      </div>
      {whisperReason && !isLoading && !blockedReason ? (
        <p className="text-2xs leading-4 text-subtle-foreground">Whisper が使えない理由: {whisperReason}</p>
      ) : null}
      {dirty ? <p className="text-2xs leading-4 text-subtle-foreground">未保存の変更は、自動タイミングの実行前に保存されます。</p> : null}
      {result && result.warnings.length > 0 ? (
        <Alert
          tone="warning"
          title="自動タイミングの注意"
          action={
            <Button type="button" variant="ghost" size="xs" onClick={() => setResult(null)}>
              閉じる
            </Button>
          }
        >
          <ul className="flex list-disc flex-col gap-0.5 pl-4">
            {result.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </Alert>
      ) : null}
    </div>
  );
}
