"use client";

import { Bot, Captions, CircleCheck, CircleMinus, TriangleAlert } from "lucide-react";
import * as React from "react";

import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { PROVIDER_KIND_LABELS, RIGHTS_ITEMS, providerLabel } from "@/lib/constants";
import { formatPricePerSecond, formatUsd } from "@/lib/format";
import type { CapabilitiesOut, ProviderInfo } from "@/lib/types";

import { SettingsSection } from "./section";

function ProviderStatusBadge({ provider }: { provider: ProviderInfo }) {
  if (!provider.configured) return <Badge>未設定</Badge>;
  if (!provider.available) return <Badge tone="warning">利用不可</Badge>;
  return <Badge tone="success">設定済み</Badge>;
}

function FeatureRow({
  icon: Icon,
  title,
  description,
  ok,
  okLabel,
  ngLabel,
  detail,
}: {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  description: string;
  ok: boolean;
  okLabel: string;
  ngLabel: string;
  detail: React.ReactNode;
}) {
  return (
    <div className="flex items-start gap-3 py-3 first:pt-0 last:pb-0">
      <div className="flex size-8 shrink-0 items-center justify-center rounded-md border border-border bg-surface-raised">
        <Icon className="size-4 text-muted-foreground" aria-hidden />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <p className="text-sm font-medium text-foreground">{title}</p>
          {ok ? <Badge tone="success">{okLabel}</Badge> : <Badge>{ngLabel}</Badge>}
        </div>
        <p className="text-xs leading-5 text-muted-foreground">{description}</p>
        <p className="mt-1 text-xs leading-5 text-foreground/80">{detail}</p>
      </div>
    </div>
  );
}

export function AiFeaturesSection({ capabilities }: { capabilities: CapabilitiesOut }) {
  const { whisper } = capabilities;
  return (
    <SettingsSection id="ai" title="AI 機能" description="LLM と音声認識は任意です。未設定でも代替手段で動作します。">
      <div className="flex flex-col divide-y divide-border px-5 pb-5">
        <FeatureRow
          icon={Bot}
          title="Claude（ストーリーボード生成・プロンプト翻訳）"
          description="歌詞・雰囲気からシーン構成と英語プロンプトを作成します。"
          ok={capabilities.claude_configured}
          okLabel="利用可能"
          ngLabel="未設定"
          detail={
            capabilities.claude_configured
              ? "ANTHROPIC_API_KEY が設定されています。費用は LLM 利用額として記録されます。"
              : "ANTHROPIC_API_KEY が未設定のため、テンプレートによるストーリーボード生成と内蔵辞書による翻訳を使用します。"
          }
        />
        <FeatureRow
          icon={Captions}
          title="Whisper（歌詞の自動タイミング合わせ）"
          description="faster-whisper で歌声を認識し、歌詞の各行に時刻を付けます（歌詞テキストは書き換えません）。"
          ok={whisper.available}
          okLabel="利用可能"
          ngLabel="利用不可"
          detail={
            whisper.available
              ? "高精度の自動タイミングを使用できます。"
              : `${whisper.reason ? `${whisper.reason} ` : ""}代わりに解析結果に基づくヒューリスティックな自動タイミングを使用します。`
          }
        />
      </div>
    </SettingsSection>
  );
}

export function ProvidersSection({ capabilities }: { capabilities: CapabilitiesOut }) {
  return (
    <SettingsSection
      id="providers"
      title="動画生成プロバイダー"
      description="API キーはバックエンドの環境変数（KLING_API_KEY / RUNWAY_API_KEY / GOOGLE_API_KEY）で設定します。"
    >
      <ul className="flex flex-col divide-y divide-border border-t border-border">
        {capabilities.providers.map((provider) => {
          const unverified = provider.models.filter((model) => !model.verified).length;
          return (
            <li key={provider.id} className="flex flex-col gap-2 px-5 py-4">
              <div className="flex flex-wrap items-center gap-2">
                <p className="text-sm font-medium text-foreground">{provider.name || providerLabel(provider.id)}</p>
                <Badge tone="outline">{PROVIDER_KIND_LABELS[provider.kind] ?? provider.kind}</Badge>
                <ProviderStatusBadge provider={provider} />
                {provider.kind === "api" && unverified > 0 ? (
                  <Badge tone="warning">
                    <TriangleAlert aria-hidden />
                    未検証 {unverified === provider.models.length ? "" : `${unverified}モデル`}
                  </Badge>
                ) : null}
              </div>
              {provider.reason ? (
                <p className="flex items-start gap-1.5 text-xs leading-5 text-muted-foreground">
                  {provider.configured ? (
                    <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-warning" aria-hidden />
                  ) : (
                    <CircleMinus className="mt-0.5 size-3.5 shrink-0 text-subtle-foreground" aria-hidden />
                  )}
                  {provider.reason}
                </p>
              ) : provider.configured && provider.available ? (
                <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
                  <CircleCheck className="size-3.5 text-success" aria-hidden />
                  生成に使用できます
                </p>
              ) : null}
              {provider.models.length > 0 ? (
                <p className="text-xs text-subtle-foreground">
                  モデル: {provider.models.map((model) => model.name || model.id).join(" / ")}
                </p>
              ) : null}
            </li>
          );
        })}
      </ul>
      {capabilities.providers.some((p) => p.kind === "api") ? (
        <p className="border-t border-border px-5 py-3 text-xs leading-5 text-subtle-foreground">
          「未検証」は、実際の API キーでの生成テストがまだ通っていないことを示します（この環境では外部 API に接続できないため、モックした応答でのみテスト済み）。
        </p>
      ) : null}
    </SettingsSection>
  );
}

/** "4 / 6 / 8 秒", or a range for long lists such as Kling's 3–15 s. */
function clipDurationsLabel(durations: readonly number[], maxClipDuration: number): string {
  if (durations.length === 0) return `最大 ${maxClipDuration} 秒`;
  return durations.length > 3 ? `${durations[0]}〜${durations[durations.length - 1]} 秒` : `${durations.join(" / ")} 秒`;
}

export function PricingSection({ capabilities }: { capabilities: CapabilitiesOut }) {
  const rows = capabilities.providers.flatMap((provider) => provider.models.map((model) => ({ provider, model })));
  return (
    <SettingsSection
      id="pricing"
      title="料金表"
      description="1秒あたりの生成単価（USD）。見積もりと予算チェックに使われます。最新の価格は各社の公式ページで確認してください。"
    >
      {rows.length === 0 ? (
        <p className="px-5 pb-5 text-sm text-muted-foreground">モデル情報がありません。</p>
      ) : (
        <Table containerClassName="border-t border-border">
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead>プロバイダー / モデル</TableHead>
              <TableHead className="text-right">単価</TableHead>
              <TableHead className="text-right">5秒クリップ</TableHead>
              <TableHead>クリップ長</TableHead>
              <TableHead>解像度 / 縦型</TableHead>
              <TableHead>価格情報</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map(({ provider, model }) => (
              <TableRow key={`${provider.id}:${model.id}`}>
                <TableCell>
                  <div className="flex min-w-36 flex-col">
                    <span className="text-foreground">{provider.name || providerLabel(provider.id)}</span>
                    <span className="font-mono text-2xs break-all text-muted-foreground">{model.id}</span>
                  </div>
                </TableCell>
                <TableCell className="text-right whitespace-nowrap tabular">{formatPricePerSecond(model.price_per_second_usd)}</TableCell>
                <TableCell className="text-right whitespace-nowrap tabular">{formatUsd(model.price_per_second_usd * 5)}</TableCell>
                <TableCell className="whitespace-nowrap text-muted-foreground tabular">
                  {clipDurationsLabel(model.clip_durations, model.max_clip_duration)}
                </TableCell>
                <TableCell>
                  <div className="flex flex-col items-start gap-1">
                    <span className="whitespace-nowrap text-muted-foreground">{model.resolution}</span>
                    {model.supports_vertical ? <Badge tone="success">9:16 対応</Badge> : <Badge tone="warning">クロップ</Badge>}
                  </div>
                </TableCell>
                <TableCell>
                  <div className="flex w-64 flex-col items-start gap-1">
                    {provider.kind === "api" ? (
                      model.verified ? <Badge tone="success">検証済み</Badge> : <Badge tone="warning">未検証</Badge>
                    ) : null}
                    {model.price_note ? <span className="text-xs leading-5 text-muted-foreground">{model.price_note}</span> : null}
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </SettingsSection>
  );
}

export function CommercialUseSection({ capabilities }: { capabilities: CapabilitiesOut }) {
  const notes = capabilities.providers.filter((provider) => provider.commercial_use_note);
  return (
    <SettingsSection
      id="commercial"
      title="商用利用に関する注意"
      description="生成した動画を公開・商用利用する前に、各プロバイダーの規約と楽曲・歌詞の権利を確認してください。"
    >
      <div className="flex flex-col gap-5 px-5 pb-5">
        {notes.length > 0 ? (
          <dl className="flex flex-col divide-y divide-border rounded-md border border-border">
            {notes.map((provider) => (
              <div key={provider.id} className="grid grid-cols-1 gap-1 px-4 py-3 md:grid-cols-[180px_1fr] md:gap-4">
                <dt className="text-sm font-medium text-foreground">{provider.name || providerLabel(provider.id)}</dt>
                <dd className="text-sm leading-6 whitespace-pre-line text-muted-foreground">{provider.commercial_use_note}</dd>
              </div>
            ))}
          </dl>
        ) : null}
        <div>
          <p className="mb-2 text-xs font-medium text-muted-foreground">生成前に必要な権利確認（プロジェクトごと）</p>
          <ul className="flex flex-col gap-1.5">
            {RIGHTS_ITEMS.map((item) => (
              <li key={item.key} className="flex items-start gap-2 text-sm leading-6 text-foreground/90">
                <CircleCheck className="mt-1 size-3.5 shrink-0 text-subtle-foreground" aria-hidden />
                {item.label}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </SettingsSection>
  );
}
