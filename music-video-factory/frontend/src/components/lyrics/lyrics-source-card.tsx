"use client";

import { ChevronDown, ChevronUp, FileText, ListPlus, Upload } from "lucide-react";
import * as React from "react";
import { useSWRConfig } from "swr";

import { recordRightsResets } from "@/components/rights/rights-reset";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { toast } from "@/components/ui/toast";
import { apiPaths, errorMessage, updateLyrics, uploadLyrics } from "@/lib/api";
import { LYRICS_EXTENSIONS, MAX_LYRICS_FILE_BYTES } from "@/lib/constants";
import { formatBytes } from "@/lib/format";
import type { LyricsFormatInput, LyricsOut, LyricsParseOut, ProjectOut } from "@/lib/types";
import { fileExtension } from "@/lib/utils";

const FORMAT_OPTIONS: { value: LyricsFormatInput; label: string }[] = [
  { value: "auto", label: "自動判定" },
  { value: "txt", label: "テキスト（TXT）" },
  { value: "lrc", label: "LRC（時刻付き）" },
  { value: "srt", label: "SRT（字幕）" },
];

const FORMAT_LABELS: Record<string, string> = { txt: "TXT", lrc: "LRC", srt: "SRT" };

type Pending = { kind: "text" } | { kind: "file"; file: File };

export interface LyricsSourceCardProps {
  projectId: string;
  /** The project's rights checklist before the change (a changed lyric text un-confirms `lyrics_rights`). */
  rights: ProjectOut["rights"];
  lyrics: LyricsOut;
  /** True when replacing the lines would discard timing (saved or unsaved). */
  hasTiming: boolean;
  /** Called with the newly parsed lines (they replace the editor's lines). */
  onParsed: (result: LyricsParseOut) => void;
}

/**
 * Lyrics text (stored verbatim) and file import. Parsing replaces the line list;
 * the user is asked first when timing would be lost. The parser's notes (excluded section
 * headings, LRC line ends estimated before an instrumental break, encoding…) are listed under
 * 「読み込み時の注意」; a withdrawn rights confirmation is shown by the panel instead.
 */
export function LyricsSourceCard({ projectId, rights, lyrics, hasTiming, onParsed }: LyricsSourceCardProps) {
  const { mutate } = useSWRConfig();
  const fileInput = React.useRef<HTMLInputElement>(null);
  const [text, setText] = React.useState<string | null>(null);
  const [format, setFormat] = React.useState<LyricsFormatInput>("auto");
  const [expanded, setExpanded] = React.useState<boolean | null>(null);
  const [pending, setPending] = React.useState<Pending | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [warnings, setWarnings] = React.useState<string[]>([]);
  const [problem, setProblem] = React.useState<string | null>(null);

  const value = text ?? lyrics.text;
  const edited = text !== null && text !== lyrics.text;
  const isOpen = expanded ?? lyrics.lines.length === 0;

  async function run(action: Pending) {
    setBusy(true);
    setProblem(null);
    try {
      const result =
        action.kind === "file"
          ? await uploadLyrics(projectId, action.file)
          : await updateLyrics(projectId, { text: value, format });
      setText(null);
      onParsed(result);
      await mutate<LyricsOut>(apiPaths.lyrics(projectId), (current) =>
        current ? { ...current, format: result.format, lines: result.lines } : current,
      );
      // Fresh rights (the lyrics are saved either way, so a failed refetch only skips the reset notice).
      const project = await mutate<ProjectOut>(apiPaths.project(projectId)).catch(() => undefined);
      const reset = recordRightsResets(projectId, rights, project?.rights, result.warnings);
      setWarnings(result.warnings.filter((warning) => !reset.messages.includes(warning)));
      toast.success(`${result.lines.length}行の歌詞を読み込みました`, `形式: ${FORMAT_LABELS[result.format] ?? result.format}`);
    } catch (error) {
      setProblem(errorMessage(error));
      throw error;
    } finally {
      setBusy(false);
    }
  }

  function request(action: Pending) {
    if (hasTiming) setPending(action);
    else void run(action).catch(() => {});
  }

  function onFileChosen(file: File) {
    const ext = fileExtension(file.name);
    if (!LYRICS_EXTENSIONS.includes(ext)) {
      setProblem(`${file.name}: 対応していない形式です（${LYRICS_EXTENSIONS.join(", ")}）。`);
      return;
    }
    if (file.size > MAX_LYRICS_FILE_BYTES) {
      setProblem(`${file.name}: ファイルサイズ（${formatBytes(file.size)}）が上限 ${formatBytes(MAX_LYRICS_FILE_BYTES)} を超えています。`);
      return;
    }
    request({ kind: "file", file });
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <FileText className="size-4 text-muted-foreground" aria-hidden />
          歌詞テキスト
          {lyrics.lines.length > 0 ? (
            <Badge tone="outline">
              {FORMAT_LABELS[lyrics.format] ?? lyrics.format} ・ {lyrics.lines.length}行
            </Badge>
          ) : null}
        </CardTitle>
        <CardDescription>
          テキストを貼り付けるか、.txt / .lrc / .srt ファイルを読み込みます。歌詞は入力どおりに保存され、自動で書き換えられることはありません。
        </CardDescription>
        <CardAction>
          <Button type="button" variant="ghost" size="sm" onClick={() => setExpanded(!isOpen)} aria-expanded={isOpen}>
            {isOpen ? <ChevronUp aria-hidden /> : <ChevronDown aria-hidden />}
            {isOpen ? "閉じる" : "編集"}
          </Button>
        </CardAction>
      </CardHeader>
      {isOpen ? (
        <CardContent className="flex flex-col gap-3">
          <Textarea
            aria-label="歌詞テキスト"
            rows={10}
            value={value}
            onChange={(event) => setText(event.target.value)}
            placeholder={"1行に1フレーズずつ入力します（空行は区切りとして扱われます）。\nLRC（[00:12.34]歌詞）や SRT もそのまま貼り付けられます。"}
            className="font-mono text-[13px]"
            spellCheck={false}
          />
          <div className="flex flex-wrap items-center gap-2">
            <Label htmlFor="lyrics-format" className="text-xs font-normal text-muted-foreground">
              形式
            </Label>
            <Select value={format} onValueChange={(next) => setFormat(next as LyricsFormatInput)}>
              <SelectTrigger id="lyrics-format" size="sm" className="w-44">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {FORMAT_OPTIONS.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <div className="ml-auto flex flex-wrap items-center gap-2">
              {edited ? (
                <Button type="button" variant="ghost" size="sm" onClick={() => setText(null)} disabled={busy}>
                  変更を破棄
                </Button>
              ) : null}
              <Button type="button" variant="secondary" size="sm" onClick={() => fileInput.current?.click()} disabled={busy}>
                <Upload aria-hidden />
                ファイルを読み込む
              </Button>
              <Button
                type="button"
                variant="secondary"
                size="sm"
                onClick={() => request({ kind: "text" })}
                disabled={!value.trim() || (!edited && lyrics.lines.length > 0)}
                loading={busy}
              >
                <ListPlus aria-hidden />
                歌詞行を作成
              </Button>
            </div>
            <input
              ref={fileInput}
              type="file"
              className="sr-only"
              tabIndex={-1}
              aria-label="歌詞ファイル（.txt / .lrc / .srt）"
              accept={LYRICS_EXTENSIONS.map((ext) => `.${ext}`).join(",")}
              onChange={(event) => {
                const file = event.currentTarget.files?.[0];
                event.currentTarget.value = "";
                if (file) onFileChosen(file);
              }}
            />
          </div>
          {problem ? (
            <Alert tone="danger" title="歌詞を読み込めませんでした">
              {problem}
            </Alert>
          ) : null}
        </CardContent>
      ) : null}
      {warnings.length > 0 ? (
        <CardContent className={isOpen ? "pt-0" : undefined}>
          <Alert
            tone="warning"
            title="読み込み時の注意"
            action={
              <Button type="button" variant="ghost" size="xs" onClick={() => setWarnings([])}>
                閉じる
              </Button>
            }
          >
            <ul className="flex list-disc flex-col gap-0.5 pl-4">
              {warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          </Alert>
        </CardContent>
      ) : null}

      <ConfirmDialog
        open={pending !== null}
        onOpenChange={(open) => !open && setPending(null)}
        title="歌詞行を作り直しますか？"
        description={
          pending?.kind === "file"
            ? `「${pending.file.name}」から歌詞行を作り直します。現在の行と設定済みのタイミングは置き換えられます（未保存の変更も破棄されます）。`
            : "入力したテキストから歌詞行を作り直します。現在の行と設定済みのタイミングは置き換えられます（未保存の変更も破棄されます）。"
        }
        confirmLabel="作り直す"
        destructive
        onConfirm={async () => {
          // Errors are shown inline in the card, so the dialog closes either way.
          if (pending) await run(pending).catch(() => {});
        }}
      />
    </Card>
  );
}
