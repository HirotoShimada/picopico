import * as React from "react";

import { cn } from "@/lib/utils";

import { fieldControlClasses } from "./input";

function Textarea({ className, rows = 3, ...props }: React.ComponentProps<"textarea">) {
  return (
    <textarea
      data-slot="textarea"
      rows={rows}
      className={cn(fieldControlClasses, "min-h-16 resize-y py-2 leading-6", className)}
      {...props}
    />
  );
}

export { Textarea };
