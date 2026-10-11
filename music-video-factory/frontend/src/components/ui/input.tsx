import * as React from "react";

import { cn } from "@/lib/utils";

export const fieldControlClasses = cn(
  "focus-ring-custom w-full min-w-0 rounded-md border border-border bg-surface px-3 text-sm text-foreground",
  "placeholder:text-subtle-foreground transition-colors outline-none",
  "hover:border-border-strong",
  "focus-visible:border-zinc-400 focus-visible:ring-2 focus-visible:ring-ring/25",
  "disabled:cursor-not-allowed disabled:opacity-50",
  "aria-invalid:border-danger/70 aria-invalid:focus-visible:ring-danger/30",
);

function Input({ className, type = "text", ...props }: React.ComponentProps<"input">) {
  return (
    <input
      type={type}
      data-slot="input"
      className={cn(
        fieldControlClasses,
        "h-9 py-1",
        "file:mr-3 file:h-7 file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-foreground",
        type === "number" && "tabular",
        className,
      )}
      {...props}
    />
  );
}

export { Input };
