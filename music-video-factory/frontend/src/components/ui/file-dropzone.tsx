"use client";

import { Upload } from "lucide-react";
import * as React from "react";

import { formatBytes } from "@/lib/format";
import { cn, fileExtension } from "@/lib/utils";

import { Button } from "./button";

export interface FileDropzoneProps {
  /** Allowed extensions without the dot, e.g. `["mp3", "wav"]`. */
  accept: readonly string[];
  multiple?: boolean;
  /** Max number of files per selection (with `multiple`). */
  maxFiles?: number;
  maxSizeBytes?: number;
  disabled?: boolean;
  /** Called with the files that passed extension/size checks. */
  onFiles: (files: File[]) => void;
  /** Called with Japanese messages for rejected files. */
  onReject?: (messages: string[]) => void;
  title?: React.ReactNode;
  description?: React.ReactNode;
  buttonLabel?: string;
  icon?: React.ComponentType<{ className?: string }>;
  id?: string;
  className?: string;
  /** Marks the zone invalid (red border). */
  invalid?: boolean;
}

/** Drag-and-drop area with a keyboard-accessible file picker button. */
export function FileDropzone({
  accept,
  multiple = false,
  maxFiles,
  maxSizeBytes,
  disabled = false,
  onFiles,
  onReject,
  title = "ここにファイルをドロップ",
  description,
  buttonLabel = "ファイルを選択",
  icon: Icon = Upload,
  id,
  className,
  invalid = false,
}: FileDropzoneProps) {
  const generatedId = React.useId();
  const inputId = id ?? generatedId;
  const inputRef = React.useRef<HTMLInputElement>(null);
  const dragDepth = React.useRef(0);
  const [dragging, setDragging] = React.useState(false);

  const acceptAttr = accept.map((ext) => `.${ext}`).join(",");

  function handleFiles(list: FileList | null) {
    if (!list || list.length === 0) return;
    const files = Array.from(list);
    const accepted: File[] = [];
    const rejected: string[] = [];

    if (!multiple && files.length > 1) rejected.push("ファイルは1つだけ選択してください。");
    const candidates = multiple ? files : files.slice(0, 1);
    if (maxFiles !== undefined && candidates.length > maxFiles) {
      rejected.push(`一度に選択できるのは${maxFiles}件までです。`);
    }
    for (const file of candidates.slice(0, maxFiles ?? candidates.length)) {
      const ext = fileExtension(file.name);
      if (!accept.includes(ext)) {
        rejected.push(`${file.name}: 対応していない形式です（${accept.join(", ")}）。`);
      } else if (maxSizeBytes !== undefined && file.size > maxSizeBytes) {
        rejected.push(`${file.name}: ファイルサイズ（${formatBytes(file.size)}）が上限 ${formatBytes(maxSizeBytes)} を超えています。`);
      } else if (file.size === 0) {
        rejected.push(`${file.name}: 空のファイルです。`);
      } else {
        accepted.push(file);
      }
    }
    if (rejected.length > 0) onReject?.(rejected);
    if (accepted.length > 0) onFiles(accepted);
  }

  return (
    <div
      data-slot="file-dropzone"
      data-dragging={dragging || undefined}
      aria-disabled={disabled || undefined}
      onDragEnter={(event) => {
        if (disabled) return;
        event.preventDefault();
        dragDepth.current += 1;
        setDragging(true);
      }}
      onDragOver={(event) => {
        if (disabled) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
      }}
      onDragLeave={() => {
        if (disabled) return;
        dragDepth.current = Math.max(0, dragDepth.current - 1);
        if (dragDepth.current === 0) setDragging(false);
      }}
      onDrop={(event) => {
        if (disabled) return;
        event.preventDefault();
        dragDepth.current = 0;
        setDragging(false);
        handleFiles(event.dataTransfer.files);
      }}
      className={cn(
        "flex flex-col items-center justify-center gap-3 rounded-lg border border-dashed px-6 py-8 text-center transition-colors",
        dragging ? "border-zinc-300 bg-surface-hover" : "border-border-strong bg-surface",
        invalid && !dragging && "border-danger/60",
        disabled && "pointer-events-none opacity-50",
        className,
      )}
    >
      <div className="flex size-10 items-center justify-center rounded-full border border-border bg-surface-raised">
        <Icon className="size-5 text-muted-foreground" aria-hidden />
      </div>
      <div className="flex flex-col gap-1">
        <p className="text-sm font-medium text-foreground">{dragging ? "ドロップして追加" : title}</p>
        {description ? <p className="text-xs leading-5 text-muted-foreground">{description}</p> : null}
      </div>
      <Button type="button" variant="secondary" size="sm" disabled={disabled} onClick={() => inputRef.current?.click()}>
        {buttonLabel}
      </Button>
      <input
        ref={inputRef}
        id={inputId}
        type="file"
        className="sr-only"
        tabIndex={-1}
        accept={acceptAttr}
        multiple={multiple}
        disabled={disabled}
        onChange={(event) => {
          handleFiles(event.currentTarget.files);
          // Allow selecting the same file again after an error.
          event.currentTarget.value = "";
        }}
      />
    </div>
  );
}
