"use client";

import { CircleAlert, CircleCheck, Info, X } from "lucide-react";
import { Toast as ToastPrimitive } from "radix-ui";
import * as React from "react";

import { cn } from "@/lib/utils";

export type ToastVariant = "default" | "success" | "error";

export interface ToastOptions {
  title: React.ReactNode;
  description?: React.ReactNode;
  variant?: ToastVariant;
  /** Milliseconds; errors stay longer by default. */
  duration?: number;
  /** Optional action button (closes the toast when clicked). */
  action?: { label: string; onClick: () => void };
}

interface ToastItem extends ToastOptions {
  id: number;
  open: boolean;
}

// --- store (module-level so `toast()` works outside React components) -------------------

const MAX_TOASTS = 4;
let items: ToastItem[] = [];
let nextId = 1;
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

const EMPTY: ToastItem[] = [];
const getSnapshot = (): ToastItem[] => items;
const getServerSnapshot = (): ToastItem[] => EMPTY;

/** Show a toast. Returns its id for `dismissToast`. */
export function toast(options: ToastOptions): number {
  const id = nextId++;
  items = [...items, { ...options, id, open: true }].slice(-MAX_TOASTS);
  emit();
  return id;
}

toast.success = (title: React.ReactNode, description?: React.ReactNode) => toast({ title, description, variant: "success" });
toast.error = (title: React.ReactNode, description?: React.ReactNode) => toast({ title, description, variant: "error" });

export function dismissToast(id: number): void {
  items = items.map((item) => (item.id === id ? { ...item, open: false } : item));
  emit();
}

function removeToast(id: number): void {
  items = items.filter((item) => item.id !== id);
  emit();
}

/** Hook form of the toast API for components. */
export function useToast(): { toast: typeof toast; dismiss: typeof dismissToast } {
  return { toast, dismiss: dismissToast };
}

// --- rendering ----------------------------------------------------------------------------------

const VARIANT_STYLES: Record<ToastVariant, { className: string; icon: React.ComponentType<{ className?: string }>; iconClass: string }> = {
  default: { className: "border-border-strong", icon: Info, iconClass: "text-muted-foreground" },
  success: { className: "border-success/40", icon: CircleCheck, iconClass: "text-success" },
  error: { className: "border-danger/50", icon: CircleAlert, iconClass: "text-danger" },
};

/** Mount once (in the app providers). */
export function Toaster() {
  const toasts = React.useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);

  return (
    <ToastPrimitive.Provider swipeDirection="right" label="通知">
      {toasts.map((item) => {
        const variant = item.variant ?? "default";
        const style = VARIANT_STYLES[variant];
        const Icon = style.icon;
        return (
          <ToastPrimitive.Root
            key={item.id}
            open={item.open}
            duration={item.duration ?? (variant === "error" ? 8000 : 4000)}
            type={variant === "error" ? "foreground" : "background"}
            onOpenChange={(open) => {
              if (!open) {
                dismissToast(item.id);
                window.setTimeout(() => removeToast(item.id), 200);
              }
            }}
            className={cn(
              "group pointer-events-auto relative flex w-full items-start gap-3 rounded-lg border bg-surface-raised p-3.5 pr-9 text-sm shadow-xl shadow-black/50",
              "data-[swipe=move]:translate-x-(--radix-toast-swipe-move-x) data-[swipe=cancel]:translate-x-0 data-[state=closed]:opacity-0",
              style.className,
            )}
          >
            <Icon className={cn("mt-0.5 size-4 shrink-0", style.iconClass)} aria-hidden />
            <div className="flex min-w-0 flex-1 flex-col gap-0.5">
              <ToastPrimitive.Title className="font-medium leading-5 text-foreground">{item.title}</ToastPrimitive.Title>
              {item.description ? (
                <ToastPrimitive.Description className="text-xs leading-5 break-words text-muted-foreground">
                  {item.description}
                </ToastPrimitive.Description>
              ) : null}
              {item.action ? (
                <ToastPrimitive.Action
                  altText={item.action.label}
                  onClick={item.action.onClick}
                  className="focus-ring-custom mt-1.5 w-fit cursor-pointer rounded border border-border-strong px-2 py-1 text-xs font-medium text-foreground outline-none hover:bg-surface-hover focus-visible:ring-2 focus-visible:ring-ring/40"
                >
                  {item.action.label}
                </ToastPrimitive.Action>
              ) : null}
            </div>
            <ToastPrimitive.Close
              aria-label="閉じる"
              className="focus-ring-custom absolute top-2.5 right-2.5 cursor-pointer rounded p-1 text-subtle-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/40"
            >
              <X className="size-3.5" aria-hidden />
            </ToastPrimitive.Close>
          </ToastPrimitive.Root>
        );
      })}
      <ToastPrimitive.Viewport label="通知 ({hotkey})" className="fixed right-4 bottom-4 z-[100] flex w-[380px] max-w-[calc(100vw-2rem)] flex-col gap-2 outline-none" />
    </ToastPrimitive.Provider>
  );
}
