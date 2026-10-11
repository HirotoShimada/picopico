import * as React from "react";

import { cn } from "@/lib/utils";

import { Label } from "./label";

export interface FieldProps extends React.ComponentProps<"div"> {
  label: React.ReactNode;
  /** `id` of the control the label points to. */
  htmlFor?: string;
  required?: boolean;
  optional?: boolean;
  /** Help text under the control. */
  description?: React.ReactNode;
  /** Validation message (shown in red, replaces nothing). */
  error?: React.ReactNode;
}

/** Label + control + help text + error, vertically stacked. */
function Field({ label, htmlFor, required, optional, description, error, className, children, ...props }: FieldProps) {
  const descriptionId = htmlFor && description ? `${htmlFor}-description` : undefined;
  const errorId = htmlFor && error ? `${htmlFor}-error` : undefined;
  return (
    <div data-slot="field" className={cn("flex flex-col gap-1.5", className)} {...props}>
      <Label htmlFor={htmlFor}>
        {label}
        {required ? (
          <span className="text-2xs font-normal text-danger-foreground" aria-hidden>
            必須
          </span>
        ) : null}
        {optional ? <span className="text-2xs font-normal text-subtle-foreground">任意</span> : null}
      </Label>
      {children}
      {description ? (
        <p id={descriptionId} className="text-xs leading-5 text-muted-foreground">
          {description}
        </p>
      ) : null}
      {error ? (
        <p id={errorId} role="alert" className="text-xs leading-5 text-danger-foreground">
          {error}
        </p>
      ) : null}
    </div>
  );
}

export { Field };
