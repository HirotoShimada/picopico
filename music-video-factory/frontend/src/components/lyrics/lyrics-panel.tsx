"use client";

import { Download, Keyboard, Redo2, Save, Undo2, X } from "lucide-react";
import * as React from "react";

import { useAudioHotkeys, useAudioPlayer } from "@/components/audio/audio-player";
import { AudioTransport } from "@/components/audio/audio-transport";
import { RightsResetNotice } from "@/components/rights/rights-reset-notice";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { toast } from "@/components/ui/toast";
import { SimpleTooltip } from "@/components/ui/tooltip";
import { useClips } from "@/hooks/use-clips";
import { useLyrics } from "@/hooks/use-lyrics";
import { downloadLyricsExport, errorMessage } from "@/lib/api";
import type { LyricsExportFormat, ProjectOut, Segment } from "@/lib/types";

import { AutoSyncBar } from "./auto-sync-bar";
import { LyricPreview } from "./lyric-preview";
import { LyricsSourceCard } from "./lyrics-source-card";
import { LyricsTimingTable } from "./lyrics-timing-table";
import { countTimed, hasErrors, tapLineEnd, tapLineStart, validateLines } from "./timing";
import type { LyricsDraft } from "./use-lyrics-draft";

export interface LyricsPanelProps {
  project: ProjectOut;
  draft: LyricsDraft;
  /** Song length (analysis, else the audio asset). */
  duration: number | null;
  analysisReady: boolean;
  segment: Segment | null;
}

function Kbd({ children }: { children: React.ReactNode }) {
  return (
    <kbd className="rounded border border-border-strong bg-surface px-1 font-mono text-2xs leading-4 text-muted-foreground">{children}</kbd>
  );
}

function safeFilename(title: string): string {
  const cleaned = title.replace(/[\\/:*?"<>|\u0000-\u001f]/g, "").trim();
  return cleaned || "lyrics";
}

/** Tab 「歌詞」: lyrics input, auto-sync, timing table, 9:16 live preview and LRC/SRT export. */
export function LyricsPanel({ project, draft, duration, analysisReady, segment }: LyricsPanelProps) {
  const { data: lyrics, error, isLoading, mutate } = useLyrics(project.id);
  const { data: clips } = useClips(project.id);
  const player = useAudioPlayer();
  const [editText, setEditText] = React.useState(false);
  const [tapIndex, setTapIndex] = React.useState<number | null>(null);
  const [exporting, setExporting] = React.useState<LyricsExportFormat | null>(null);

  const lines = draft.lines;
  const issues = React.useMemo(() => validateLines(lines, duration), [lines, duration]);
  const blocking = hasErrors(issues);
  const timed = countTimed(lines);
  const background = clips?.find((clip) => clip.status === "ready" && clip.thumbnail_url)?.thumbnail_url ?? null;
  const tapping = tapIndex !== null;
  const tapTarget = tapIndex !== null && tapIndex < lines.length ? lines[tapIndex] : null;

  function tapStart() {
    if (tapIndex === null || tapIndex >= lines.length) return;
    draft.edit(tapLineStart(lines, tapIndex, player.getTime()));
    setTapIndex(tapIndex + 1);
  }

  function tapEnd() {
    if (tapIndex === null || tapIndex === 0) return;
    draft.edit(tapLineEnd(lines, tapIndex - 1, player.getTime()));
  }

  useAudioHotkeys({ extra: tapping ? { t: tapStart, y: tapEnd } : undefined });

  // Ctrl/⌘+Z = undo, Ctrl/⌘+Shift+Z or Ctrl+Y = redo (outside text fields).
  const onUndoKey = React.useEffectEvent((event: KeyboardEvent) => {
    if (!(event.ctrlKey || event.metaKey) || event.altKey) return;
    const target = event.target as HTMLElement | null;
    if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)) return;
    const key = event.key.toLowerCase();
    if (key === "z" && !event.shiftKey) {
      event.preventDefault();
      draft.undo();
    } else if ((key === "z" && event.shiftKey) || key === "y") {
      event.preventDefault();
      draft.redo();
    }
  });
  React.useEffect(() => {
    const handler = (event: KeyboardEvent) => onUndoKey(event);
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  async function save() {
    try {
      await draft.save();
      toast.success("タイミングを保存しました");
    } catch (saveError) {
      toast.error("タイミングを保存できませんでした", errorMessage(saveError));
    }
  }

  async function exportAs(format: LyricsExportFormat) {
    setExporting(format);
    try {
      await downloadLyricsExport(project.id, format, `${safeFilename(project.title)}.${format}`);
    } catch (exportError) {
      toast.error(`${format.toUpperCase()} を書き出せませんでした`, errorMessage(exportError));
    } finally {
      setExporting(null);
    }
  }

  if (error && !lyrics) return <ErrorState error={error} onRetry={() => void mutate()} />;
  if (isLoading || !lyrics) {
    return (
      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_300px]" aria-busy aria-label="歌詞を読み込み中">
        <div className="flex flex-col gap-6">
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-96 w-full" />
        </div>
        <Skeleton className="aspect-[9/16] w-full" />
      </div>
    );
  }

  const exportDisabledReason = draft.dirty ? "未保存の変更を保存してから書き出してください" : timed === 0 ? "タイミング設定済みの行がありません" : null;

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_300px]">
      <div className="flex min-w-0 flex-col gap-6">
        <RightsResetNotice project={project} keys={["lyrics_rights"]} />
        <LyricsSourceCard
          projectId={project.id}
          rights={project.rights}
          lyrics={lyrics}
          hasTiming={timed > 0 || draft.dirty}
          onParsed={(result) => {
            draft.adopt(result.lines);
            setTapIndex(null);
          }}
        />

        <Card>
          <CardHeader>
            <CardTitle>
              タイミング
              <Badge tone={timed === lines.length && lines.length > 0 ? "success" : "neutral"}>
                {timed} / {lines.length} 行
              </Badge>
              {draft.dirty ? <Badge tone="warning">未保存</Badge> : null}
            </CardTitle>
            <CardDescription>
              各行の表示開始・終了時刻（曲の先頭からの秒）です。終了が空欄の行は、次の行の開始（最長8秒）まで表示されます。
            </CardDescription>
            <CardAction>
              <SimpleTooltip content="元に戻す（Ctrl+Z）">
                <Button type="button" variant="ghost" size="icon-sm" onClick={draft.undo} disabled={!draft.canUndo} aria-label="元に戻す">
                  <Undo2 aria-hidden />
                </Button>
              </SimpleTooltip>
              <SimpleTooltip content="やり直す（Ctrl+Shift+Z）">
                <Button type="button" variant="ghost" size="icon-sm" onClick={draft.redo} disabled={!draft.canRedo} aria-label="やり直す">
                  <Redo2 aria-hidden />
                </Button>
              </SimpleTooltip>
              <Button type="button" variant="ghost" size="sm" onClick={draft.discard} disabled={!draft.dirty || draft.saving}>
                変更を破棄
              </Button>
              <SimpleTooltip content={blocking ? "エラーのある行を修正してください" : null}>
                <span className="inline-flex">
                  <Button
                    type="button"
                    variant="primary"
                    size="sm"
                    onClick={() => void save()}
                    disabled={!draft.dirty || blocking}
                    loading={draft.saving}
                  >
                    {draft.saving ? null : <Save aria-hidden />}
                    タイミングを保存
                  </Button>
                </span>
              </SimpleTooltip>
            </CardAction>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {lines.length === 0 ? (
              <EmptyState
                title="歌詞行がまだありません"
                description="上の「歌詞テキスト」に歌詞を入力して「歌詞行を作成」を押すか、.txt / .lrc / .srt ファイルを読み込んでください。"
              />
            ) : (
              <>
                <AudioTransport duration={duration}>
                  <Button
                    type="button"
                    variant={tapping ? "outline" : "secondary"}
                    size="sm"
                    aria-pressed={tapping}
                    disabled={!project.audio}
                    onClick={() => setTapIndex(tapping ? null : Math.max(0, lines.findIndex((line) => line.start === null)))}
                  >
                    <Keyboard aria-hidden />
                    タップ入力
                  </Button>
                </AudioTransport>

                {tapping ? (
                  <div className="flex flex-wrap items-center gap-3 rounded-md border border-border-strong bg-surface-raised px-3 py-2.5" role="region" aria-label="タップ入力">
                    <div className="min-w-0 flex-1 text-sm">
                      {tapTarget ? (
                        <>
                          <span className="text-muted-foreground">次に記録する行: </span>
                          <span className="font-medium text-foreground">
                            {(tapIndex ?? 0) + 1}行目「{tapTarget.text}」
                          </span>
                        </>
                      ) : (
                        <span className="text-muted-foreground">最後の行まで記録しました。行番号をクリックすると、その行から記録し直せます。</span>
                      )}
                      <p className="text-2xs text-subtle-foreground">
                        再生しながら歌い出しで <Kbd>T</Kbd> を押すと開始位置を記録して次の行へ進みます。<Kbd>Y</Kbd>{" "}
                        で直前の行の終了位置を記録、<Kbd>Space</Kbd> で再生/一時停止。
                      </p>
                    </div>
                    <Button type="button" variant="outline" size="sm" onClick={tapStart} disabled={!tapTarget}>
                      開始を記録 <Kbd>T</Kbd>
                    </Button>
                    <Button type="button" variant="ghost" size="sm" onClick={tapEnd} disabled={!tapIndex}>
                      直前の行の終了 <Kbd>Y</Kbd>
                    </Button>
                    <Button type="button" variant="ghost" size="icon-sm" onClick={() => setTapIndex(null)} aria-label="タップ入力を終了">
                      <X aria-hidden />
                    </Button>
                  </div>
                ) : null}

                <AutoSyncBar
                  projectId={project.id}
                  lines={lines}
                  analysisReady={analysisReady}
                  segment={segment}
                  dirty={draft.dirty}
                  saveDraft={draft.save}
                  commit={draft.commit}
                />

                <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
                  <div className="flex items-center gap-2">
                    <Switch id="edit-lyric-text" checked={editText} onCheckedChange={setEditText} />
                    <Label htmlFor="edit-lyric-text" className="text-xs font-normal text-muted-foreground">
                      歌詞の表記を編集する
                    </Label>
                  </div>
                  <div className="ml-auto flex items-center gap-2">
                    <span className="text-xs text-muted-foreground">書き出し</span>
                    {(["lrc", "srt"] as const).map((format) => (
                      <SimpleTooltip key={format} content={exportDisabledReason}>
                        <span className="inline-flex">
                          <Button
                            type="button"
                            variant="secondary"
                            size="xs"
                            disabled={!!exportDisabledReason}
                            loading={exporting === format}
                            onClick={() => void exportAs(format)}
                          >
                            {exporting === format ? null : <Download aria-hidden />}
                            {format.toUpperCase()}
                          </Button>
                        </span>
                      </SimpleTooltip>
                    ))}
                  </div>
                </div>
                {editText ? (
                  <Alert tone="neutral">
                    歌詞の表記はここで明示的に編集したときだけ変わります。表記を変えた行は、カラオケ用の文字ごとのタイミングがリセットされ、均等割りで表示されます。表記を変えて保存すると、権利確認の「歌詞の権利」は確認し直しになります。
                  </Alert>
                ) : null}

                <LyricsTimingTable
                  lines={lines}
                  issues={issues}
                  duration={duration}
                  onChange={draft.edit}
                  editText={editText}
                  tapIndex={tapIndex}
                  onTapIndexChange={setTapIndex}
                />
                {blocking ? (
                  <Alert tone="danger" title="保存できない行があります">
                    赤く表示された行の開始・終了時刻を確認してください。
                  </Alert>
                ) : null}
              </>
            )}
          </CardContent>
        </Card>
      </div>

      <aside className="min-w-0 xl:sticky xl:top-20 xl:self-start">
        <Card>
          <CardHeader>
            <CardTitle>プレビュー（9:16）</CardTitle>
            <CardDescription>再生位置の歌詞を表示します。</CardDescription>
          </CardHeader>
          <CardContent>
            <LyricPreview lines={lines} duration={duration} backgroundUrl={background} />
          </CardContent>
        </Card>
      </aside>
    </div>
  );
}
