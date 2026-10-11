"use client";

import * as React from "react";

import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { useCapabilities } from "@/hooks/use-capabilities";

import { AiFeaturesSection, CommercialUseSection, PricingSection, ProvidersSection } from "./capability-sections";
import { ConnectionSection } from "./connection-section";
import { TokenSection } from "./token-section";

const SECTIONS = [
  { id: "connection", label: "バックエンド接続" },
  { id: "token", label: "API トークン" },
  { id: "ai", label: "AI 機能" },
  { id: "providers", label: "動画生成プロバイダー" },
  { id: "pricing", label: "料金表" },
  { id: "commercial", label: "商用利用に関する注意" },
] as const;

function CapabilitiesSkeleton() {
  return (
    <div className="flex flex-col gap-6" aria-busy aria-label="読み込み中">
      <Skeleton className="h-40 w-full" />
      <Skeleton className="h-64 w-full" />
      <Skeleton className="h-56 w-full" />
    </div>
  );
}

export function SettingsView() {
  const { data: capabilities, error, isLoading, isValidating, mutate } = useCapabilities();

  let capabilitySections: React.ReactNode;
  if (capabilities) {
    capabilitySections = (
      <>
        <AiFeaturesSection capabilities={capabilities} />
        <ProvidersSection capabilities={capabilities} />
        <PricingSection capabilities={capabilities} />
        <CommercialUseSection capabilities={capabilities} />
      </>
    );
  } else if (error) {
    capabilitySections = (
      <ErrorState
        error={error}
        title="プロバイダー情報を読み込めませんでした"
        onRetry={() => void mutate()}
        retrying={isValidating}
      />
    );
  } else if (isLoading) {
    capabilitySections = <CapabilitiesSkeleton />;
  }

  return (
    <div className="grid grid-cols-1 items-start gap-8 lg:grid-cols-[200px_minmax(0,1fr)]">
      <nav aria-label="設定の項目" className="hidden lg:sticky lg:top-20 lg:block">
        <ul className="flex flex-col gap-0.5 border-l border-border">
          {SECTIONS.map((section) => (
            <li key={section.id}>
              <a
                href={`#${section.id}`}
                className="focus-ring-custom -ml-px block border-l border-transparent py-1.5 pl-3 text-sm text-muted-foreground outline-none transition-colors hover:border-border-strong hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/40"
              >
                {section.label}
              </a>
            </li>
          ))}
        </ul>
      </nav>
      <div className="flex max-w-5xl min-w-0 flex-col gap-6">
        <ConnectionSection />
        <TokenSection />
        {capabilitySections}
      </div>
    </div>
  );
}
