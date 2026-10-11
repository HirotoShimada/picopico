"use client";

import { FileAudio, Music, X } from "lucide-react";
import * as React from "react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { FileDropzone } from "@/components/ui/file-dropzone";
import { AUDIO_EXTENSIONS, AUDIO_MAX_SECONDS, AUDIO_MIN_SECONDS } from "@/lib/constants";
import { formatBytes, formatDuration } from "@/lib/format";

export interface SelectedAudio {
  file: File;
  /** Duration read by the browser; `null` when it could not decode the metadata. */
  duration: number | null;
  previewUrl: string;
}

/** Reads the duration from the file's metadata in the browser (best effort). */
export function readAudioDuration(url: string): Promise<number | null> {
  return new Promise((resolve) => {
    const audio = new Audio();
    audio.preload = "metadata";
    const timer = window.setTimeout(() => resolve(null), 8000);
    audio.onloadedmetadata = () => {
      window.clearTimeout(timer);
      resolve(Number.isFinite(audio.duration) ? audio.duration : null);
    };
    audio.onerror = () => {
      window.clearTimeout(timer);
      resolve(null);
    };
    audio.src = url;
  });
}

export function audioDurationProblem(duration: number | null): string | null {
  if (duration === null) return null;
  if (duration < AUDIO_MIN_SECONDS) return `楽曲が短すぎます（${formatDuration(duration)}）。${AUDIO_MIN_SECONDS}秒以上の音源を選択してください。`;
  if (duration > AUDIO_MAX_SECONDS) return `楽曲が長すぎます（${formatDuration(duration)}）。15分以内の音源を選択してください。`;
  return null;
}

interface AudioFileCardProps {
  audio: SelectedAudio | null;
  maxAudioMb: number;
  disabled: boolean;
  onSelect: (file: File) => void;
  onClear: () => void;
  rejectMessages: string[];
  onReject: (messages: string[]) => void;
}

export function AudioFileSection({ audio, maxAudioMb, disabled, onSelect, onClear, rejectMessages, onReject }: AudioFileCardProps) {
  const durationProblem = audio ? audioDurationProblem(audio.duration) : null;

  return (
    <div className="flex flex-col gap-3">
      {audio ? (
        <div className="flex flex-col gap-3 rounded-lg border border-border bg-surface-raised p-3">
          <div className="flex items-start gap-3">
            <div className="flex size-9 shrink-0 items-center justify-center rounded-md border border-border bg-surface">
              <FileAudio className="size-4 text-muted-foreground" aria-hidden />
            </div>
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium text-foreground" title={audio.file.name}>
                {audio.file.name}
              </p>
              <p className="text-xs text-muted-foreground tabular">
                {formatBytes(audio.file.size)}
                {audio.duration !== null ? ` ・ ${formatDuration(audio.duration)}` : " ・ 長さはアップロード後に確認します"}
              </p>
            </div>
            <Button type="button" variant="ghost" size="icon-sm" onClick={onClear} disabled={disabled} aria-label="音源の選択を解除">
              <X aria-hidden />
            </Button>
          </div>
          <audio controls preload="metadata" src={audio.previewUrl} className="h-9 w-full" aria-label="選択した音源のプレビュー" />
        </div>
      ) : (
        <FileDropzone
          accept={AUDIO_EXTENSIONS}
          maxSizeBytes={maxAudioMb * 1024 * 1024}
          disabled={disabled}
          onFiles={(files) => onSelect(files[0])}
          onReject={onReject}
          icon={Music}
          title="音源ファイルをドロップ"
          description={
            <>
              {AUDIO_EXTENSIONS.join(" / ")}
              <br />
              最大 {maxAudioMb} MB ・ {AUDIO_MIN_SECONDS}秒〜15分
            </>
          }
          invalid={rejectMessages.length > 0}
        />
      )}
      {durationProblem ? <Alert tone="danger">{durationProblem}</Alert> : null}
      {rejectMessages.length > 0 ? (
        <Alert tone="danger">
          {rejectMessages.map((message) => (
            <p key={message}>{message}</p>
          ))}
        </Alert>
      ) : null}
    </div>
  );
}
