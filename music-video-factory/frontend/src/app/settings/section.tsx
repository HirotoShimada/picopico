import * as React from "react";

import { Card, CardAction, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

export function SettingsSection({
  id,
  title,
  description,
  action,
  children,
}: {
  id: string;
  title: string;
  description?: React.ReactNode;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section id={id} aria-labelledby={`${id}-title`} className="scroll-mt-20">
      <Card>
        <CardHeader>
          <CardTitle id={`${id}-title`}>{title}</CardTitle>
          {description ? <CardDescription>{description}</CardDescription> : null}
          {action ? <CardAction>{action}</CardAction> : null}
        </CardHeader>
        {children}
      </Card>
    </section>
  );
}
