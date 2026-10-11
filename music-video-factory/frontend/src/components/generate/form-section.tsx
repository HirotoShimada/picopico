import * as React from "react";

import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

export interface FormSectionProps {
  step?: number;
  title: React.ReactNode;
  description?: React.ReactNode;
  action?: React.ReactNode;
  className?: string;
  contentClassName?: string;
  children: React.ReactNode;
}

/** Numbered settings card of the generate page. */
export function FormSection({ step, title, description, action, className, contentClassName, children }: FormSectionProps) {
  return (
    <Card className={className}>
      <CardHeader>
        <CardTitle>
          {step !== undefined ? (
            <span className="flex size-5 items-center justify-center rounded-full border border-border-strong text-2xs text-muted-foreground tabular">
              {step}
            </span>
          ) : null}
          {title}
        </CardTitle>
        {description ? <CardDescription>{description}</CardDescription> : null}
        {action ? <CardAction>{action}</CardAction> : null}
      </CardHeader>
      <CardContent className={cn("flex flex-col gap-5", contentClassName)}>{children}</CardContent>
    </Card>
  );
}
