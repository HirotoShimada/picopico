"use client";

import { AlertDialog as AlertDialogPrimitive } from "radix-ui";
import * as React from "react";

import { cn } from "@/lib/utils";

import { Button } from "./button";

export interface ConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: React.ReactNode;
  description?: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  /** Destructive actions get a red confirm button. */
  destructive?: boolean;
  /** Called on confirm; the dialog stays open (with a spinner) until the promise settles. */
  onConfirm: () => void | Promise<void>;
  children?: React.ReactNode;
}

/** Accessible confirmation dialog (Radix AlertDialog) for irreversible actions. */
function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel = "実行する",
  cancelLabel = "キャンセル",
  destructive = false,
  onConfirm,
  children,
}: ConfirmDialogProps) {
  const [pending, setPending] = React.useState(false);

  async function handleConfirm(event: React.MouseEvent) {
    event.preventDefault();
    setPending(true);
    try {
      await onConfirm();
      onOpenChange(false);
    } catch {
      // The caller reports the error (e.g. with a toast); keep the dialog open.
    } finally {
      setPending(false);
    }
  }

  return (
    <AlertDialogPrimitive.Root open={open} onOpenChange={(next) => !pending && onOpenChange(next)}>
      <AlertDialogPrimitive.Portal>
        <AlertDialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/70" />
        <AlertDialogPrimitive.Content
          className={cn(
            "fixed top-1/2 left-1/2 z-50 flex w-[calc(100%-2rem)] max-w-md -translate-x-1/2 -translate-y-1/2 flex-col gap-4",
            "rounded-lg border border-border-strong bg-surface p-6 text-foreground shadow-2xl shadow-black/60 outline-none",
          )}
        >
          <div className="flex flex-col gap-1.5">
            <AlertDialogPrimitive.Title className="text-base font-semibold leading-6">{title}</AlertDialogPrimitive.Title>
            {description ? (
              <AlertDialogPrimitive.Description className="text-sm leading-6 text-muted-foreground">
                {description}
              </AlertDialogPrimitive.Description>
            ) : null}
          </div>
          {children}
          <div className="mt-2 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
            <AlertDialogPrimitive.Cancel asChild>
              <Button variant="ghost" disabled={pending}>
                {cancelLabel}
              </Button>
            </AlertDialogPrimitive.Cancel>
            <AlertDialogPrimitive.Action asChild>
              <Button variant={destructive ? "destructive" : "primary"} loading={pending} onClick={handleConfirm}>
                {confirmLabel}
              </Button>
            </AlertDialogPrimitive.Action>
          </div>
        </AlertDialogPrimitive.Content>
      </AlertDialogPrimitive.Portal>
    </AlertDialogPrimitive.Root>
  );
}

export { ConfirmDialog };
