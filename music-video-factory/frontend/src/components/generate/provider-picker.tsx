"use client";

import { ExternalLink, Scale, TriangleAlert } from "lucide-react";
import Link from "next/link";
import * as React from "react";

import { Badge } from "@/components/ui/badge";
import { Label } from "@/components/ui/label";
import { isRunway, PoweredByRunway } from "@/components/ui/powered-by-runway";
import { RadioCards, type RadioCardOption } from "@/components/ui/radio-cards";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { PROVIDER_KIND_LABELS, providerLabel } from "@/lib/constants";
import { formatPricePerSecond } from "@/lib/format";
import { isOpenRouter, isPriceKnown, openRouterVendor } from "@/lib/llm";
import type { ProviderId, ProviderInfo, ProviderModelInfo } from "@/lib/types";

import { projectTabHref } from "./links";
import { OpenRouterNotes } from "./openrouter-notes";

/** Shown instead of a price for a model without a readable price (OpenRouter listing). */
export const PRICE_UNKNOWN_LABEL = "料金未取得";

/** Why a provider cannot be used right now (`null` when usable). */
export function providerUnavailableReason(provider: ProviderInfo, libraryClipCount: number | undefined): string | null {
  if (!provider.configured) return provider.reason || "API キーが未設定です。";
  if (!provider.available) return provider.reason || "現在は利用できません。";
  if (provider.kind === "library" && libraryClipCount === 0) {
    return "アップロード済みの素材がありません。素材タブから動画素材をアップロードしてください。";
  }
  return null;
}

/**
 * Why a model cannot be chosen here (`null` when usable). Only generating (API)
 * models are restricted: an image-only model needs an input image this form does
 * not collect, and a model without a known price cannot reserve the batch budget.
 * `library` / `mock` never generate from text, so the flags do not apply.
 */
export function modelUnavailableReason(model: ProviderModelInfo, providerKind: ProviderInfo["kind"]): string | null {
  if (providerKind !== "api") return null;
  if (!isPriceKnown(model)) return `${PRICE_UNKNOWN_LABEL}のため選択できません（素材ごとに予算を確保できないため）。`;
  return model.text_to_video === false ? "入力画像が必要なモデルのため、この画面からは使用できません。" : null;
}

/** `$0.05/秒`, 「無料」 or 「料金未取得」. */
export function modelPriceLabel(model: Pick<ProviderModelInfo, "price_known" | "price_per_second_usd">): string {
  return isPriceKnown(model) ? formatPricePerSecond(model.price_per_second_usd) : PRICE_UNKNOWN_LABEL;
}

export function resolveModel(provider: ProviderInfo | undefined, modelId: string | null): ProviderModelInfo | undefined {
  if (!provider) return undefined;
  const id = modelId ?? provider.default_model ?? provider.models[0]?.id;
  return provider.models.find((model) => model.id === id) ?? provider.models[0];
}

function priceSummary(provider: ProviderInfo): string {
  const usable = provider.models.filter((m) => m.text_to_video !== false || provider.kind !== "api");
  const prices = usable.flatMap((m) => (isPriceKnown(m) && m.price_per_second_usd !== null ? [m.price_per_second_usd] : []));
  if (prices.length === 0) return usable.length > 0 ? PRICE_UNKNOWN_LABEL : "—";
  const min = Math.min(...prices);
  const max = Math.max(...prices);
  if (max === 0) return "無料";
  return min === max ? formatPricePerSecond(min) : `${formatPricePerSecond(min)}〜${formatPricePerSecond(max)}`;
}

function clipDurationsLabel(model: ProviderModelInfo): string {
  if (model.clip_durations.length === 0) return `最大 ${model.max_clip_duration} 秒`;
  const values = model.clip_durations;
  return values.length > 3 ? `${values[0]}〜${values[values.length - 1]} 秒` : `${values.join(" / ")} 秒`;
}

export interface ProviderPickerProps {
  projectId: string;
  providers: readonly ProviderInfo[];
  provider: ProviderId;
  model: string | null;
  onChange: (provider: ProviderId, model: string | null) => void;
  /** Ready uploaded clips (library provider); `undefined` while loading. */
  libraryClipCount: number | undefined;
  disabled?: boolean;
}

/** Provider cards (unusable ones disabled with the reason) + model select with pricing details. */
export function ProviderPicker({ projectId, providers, provider, model, onChange, libraryClipCount, disabled }: ProviderPickerProps) {
  const selected = providers.find((p) => p.id === provider);
  const selectedModel = resolveModel(selected, model);
  const selectedReason = selected ? providerUnavailableReason(selected, libraryClipCount) : null;

  const options: RadioCardOption<ProviderId>[] = providers.map((p) => {
    const reason = providerUnavailableReason(p, libraryClipCount);
    const unverified = p.kind === "api" && p.models.some((m) => !m.verified);
    return {
      value: p.id as ProviderId,
      title: p.name || providerLabel(p.id),
      meta: (
        <>
          <Badge tone="outline">{PROVIDER_KIND_LABELS[p.kind] ?? p.kind}</Badge>
          {/* OpenRouter: not only the prices but the whole integration is untested with a real key. */}
          {isOpenRouter(p.id) ? (
            <Badge tone="warning">
              <TriangleAlert aria-hidden />
              未検証
            </Badge>
          ) : unverified ? (
            <Badge tone="warning">
              <TriangleAlert aria-hidden />
              価格未検証
            </Badge>
          ) : null}
          {/* The card is a radio button: text only here, the linked mark is shown below when selected. */}
          {isRunway(p.id) ? <PoweredByRunway link={false} /> : null}
        </>
      ),
      description: (
        <span className="flex flex-col gap-0.5">
          <span className="font-mono text-foreground/90">{priceSummary(p)}</span>
          {!reason ? <span>{p.kind === "library" ? `素材 ${libraryClipCount ?? "…"} 本から構成` : "生成に使用できます"}</span> : null}
        </span>
      ),
      // A selected-but-unusable provider stays enabled so the selection is visible.
      disabled: reason !== null && p.id !== provider,
      disabledReason: reason,
    };
  });

  return (
    <div className="flex flex-col gap-4">
      <RadioCards
        options={options}
        value={provider}
        onValueChange={(next) => {
          if (next !== provider) onChange(next, null);
        }}
        disabled={disabled}
        aria-label="動画生成プロバイダー"
        className="sm:grid-cols-2 xl:grid-cols-3"
      />

      {selected && selectedReason ? (
        <p className="flex items-start gap-2 rounded-md border border-danger/35 bg-danger-soft px-3 py-2 text-xs leading-5 text-foreground" role="alert">
          <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-danger" aria-hidden />
          <span>
            {selected.name || providerLabel(selected.id)} は現在使用できません: {selectedReason}
            {selected.kind === "library" ? (
              <>
                {" "}
                <Link href={projectTabHref(projectId, "materials")} className="underline underline-offset-2 hover:text-foreground">
                  素材タブを開く
                </Link>
              </>
            ) : null}
          </span>
        </p>
      ) : null}

      {selected && selected.models.length > 0 ? (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,320px)_minmax(0,1fr)]">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="generate-model">モデル</Label>
            <Select
              value={selectedModel?.id}
              onValueChange={(next) => onChange(provider, next === selected.default_model ? null : next)}
              disabled={disabled || selected.models.length < 2}
            >
              <SelectTrigger id="generate-model">
                <SelectValue placeholder="モデルを選択" />
              </SelectTrigger>
              <SelectContent>
                {selected.models.map((m) => {
                  const reason = modelUnavailableReason(m, selected.kind);
                  const vendor = isOpenRouter(selected.id) ? openRouterVendor(m.id) : null;
                  const details = [vendor ? `提供元 ${vendor}` : null, m.resolution, clipDurationsLabel(m), modelPriceLabel(m)];
                  return (
                    <SelectItem
                      key={m.id}
                      value={m.id}
                      disabled={reason !== null}
                      description={
                        reason ?? `${details.filter(Boolean).join(" ・ ")}${m.verified ? "" : "（未検証）"}`
                      }
                    >
                      {m.name || m.id}
                      {m.id === selected.default_model ? "（既定）" : ""}
                    </SelectItem>
                  );
                })}
              </SelectContent>
            </Select>
          </div>
          {selectedModel ? (
            <div className="flex min-w-0 flex-col gap-1.5 rounded-md border border-border bg-surface-raised/50 px-3 py-2.5 text-xs leading-5">
              <div className="flex flex-wrap items-center gap-2">
                <span className={isPriceKnown(selectedModel) ? "font-mono text-sm text-foreground" : "text-sm text-foreground"}>
                  {modelPriceLabel(selectedModel)}
                </span>
                {!selectedModel.verified && (selectedModel.price_per_second_usd ?? 0) > 0 ? (
                  <Badge tone="warning">
                    <TriangleAlert aria-hidden />
                    未検証
                  </Badge>
                ) : null}
                {!selectedModel.supports_vertical ? (
                  <Badge tone="warning" title="モデルの情報に 9:16（縦型）への対応が記載されていません。詳細は下の説明を参照してください。">
                    9:16 未確認
                  </Badge>
                ) : null}
                <span className="text-subtle-foreground">
                  {isOpenRouter(selected.id) && openRouterVendor(selectedModel.id) ? `提供元 ${openRouterVendor(selectedModel.id)} ・ ` : ""}
                  {selectedModel.resolution} ・ クリップ {clipDurationsLabel(selectedModel)}
                </span>
              </div>
              {isOpenRouter(selected.id) ? <p className="font-mono text-2xs break-all text-subtle-foreground">{selectedModel.id}</p> : null}
              {selectedModel.price_note ? <p className="text-muted-foreground">{selectedModel.price_note}</p> : null}
              {selectedModel.source_url ? (
                <a
                  href={selectedModel.source_url}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="inline-flex w-fit items-center gap-1 text-subtle-foreground underline-offset-2 hover:text-foreground hover:underline"
                >
                  {isOpenRouter(selected.id) ? "OpenRouter のモデルページ" : "公式料金表"}
                  {selectedModel.as_of ? `（${selectedModel.as_of} 時点）` : ""}
                  <ExternalLink className="size-3" aria-hidden />
                </a>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}

      {selected && isOpenRouter(selected.id) ? <OpenRouterNotes provider={selected} model={selectedModel} /> : null}

      {selected?.commercial_use_note ? (
        <p className="flex items-start gap-2 text-xs leading-5 text-muted-foreground">
          <Scale className="mt-0.5 size-3.5 shrink-0 text-subtle-foreground" aria-hidden />
          <span>
            <span className="font-medium text-foreground/90">利用条件: </span>
            {selected.commercial_use_note}
          </span>
        </p>
      ) : null}

      {selected && isRunway(selected.id) ? (
        <p className="flex flex-wrap items-center gap-2 text-xs leading-5 text-muted-foreground">
          <PoweredByRunway />
          <span>Runway で生成した映像を使う画面には、この表記が表示されます。</span>
        </p>
      ) : null}
    </div>
  );
}
