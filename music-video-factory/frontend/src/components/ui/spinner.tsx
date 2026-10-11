import { LoaderCircle } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/utils";

function Spinner({ className, label = "読み込み中", ...props }: React.ComponentProps<"svg"> & { label?: string }) {
  return <LoaderCircle role="status" aria-label={label} className={cn("size-4 animate-spin text-muted-foreground", className)} {...props} />;
}

export { Spinner };
