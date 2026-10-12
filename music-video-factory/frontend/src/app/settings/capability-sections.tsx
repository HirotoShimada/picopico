"use client";

import { Bot, Captions, CircleCheck, CircleMinus, ListChecks, ShieldAlert, TriangleAlert } from "lucide-react";
import * as React from "react";

import { Badge } from "@/components/ui/badge";
import { isRunway, PoweredByRunway } from "@/components/ui/powered-by-runway";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useNow } from "@/hooks/use-now";
import { PROVIDER_KIND_LABELS, RIGHTS_ITEMS, providerLabel } from "@/lib/constants";
import { formatCount, formatDateTime, formatPricePerSecond, formatRelativeTime, formatUsd } from "@/lib/format";
import { isOpenRouter, isPriceKnown, llmRouteLabel, modelsSourceLabel, resolveLlm } from "@/lib/llm";
import type { CapabilitiesOut, LlmInfo, ProviderInfo } from "@/lib/types";

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

function ClaudeRouteDetail({ llm }: { llm: LlmInfo }) {
  const model = llm.model ? <code className="font-mono text-2xs text-foreground">{llm.model}</code> : null;
  if (llm.route === "anthropic") {
    return <>ANTHROPIC_API_KEY が設定されているため、Anthropic の API に直接送ります{model ? <>（モデル {model}）</> : null}。費用は LLM 利用額として記録されます。</>;
  }
  if (llm.route === "openrouter") {
    return (
      <>
        ANTHROPIC_API_KEY が未設定で OPENROUTER_API_KEY が設定されているため、OpenRouter 経由で Claude を使います{model ? <>（モデル {model}）</> : null}。
        費用は OpenRouter の利用額として記録されます。
      </>
    );
  }
  return <>ANTHROPIC_API_KEY・OPENROUTER_API_KEY がどちらも未設定のため、テンプレートによる絵コンテ生成と内蔵辞書による翻訳を使用します。</>;
}

/** Which Claude route is in use (Anthropic directly / via OpenRouter / template), its model and data policy. */
function ClaudeRouteRow({ llm }: { llm: LlmInfo }) {
  return (
    <div className="flex items-start gap-3 py-3 first:pt-0 last:pb-0" data-testid="claude-route">
      <div className="flex size-8 shrink-0 items-center justify-center rounded-md border border-border bg-surface-raised">
        <Bot className="size-4 text-muted-foreground" aria-hidden />
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <p className="text-sm font-medium text-foreground">Claude（絵コンテ生成・プロンプト翻訳）</p>
          <Badge tone={llm.route ? "success" : "neutral"}>{llmRouteLabel(llm.route)}</Badge>
          {llm.route && !llm.verified ? (
            <Badge tone="warning" title="実際の API キーでの呼び出しはまだ確認していません（モックした応答でのみテスト済み）">
              <TriangleAlert aria-hidden />
              未検証
            </Badge>
          ) : null}
        </div>
        <p className="text-xs leading-5 text-muted-foreground">歌詞・雰囲気からシーン構成と英語プロンプトを作成します。</p>
        <p className="text-xs leading-5 text-foreground/80">
          <ClaudeRouteDetail llm={llm} />
        </p>
        <p className="text-2xs leading-4 text-subtle-foreground">
          経路の優先順: ANTHROPIC_API_KEY（Anthropic 直接）→ OPENROUTER_API_KEY（OpenRouter 経由）→ テンプレート
        </p>
        {llm.data_policy_note ? (
          <div className="mt-1 flex items-start gap-2 rounded-md border border-border bg-surface-raised/50 px-3 py-2 text-xs leading-5 text-muted-foreground">
            <ShieldAlert className="mt-0.5 size-3.5 shrink-0 text-subtle-foreground" aria-hidden />
            <p>
              <span className="font-medium text-foreground/90">送信するデータ: </span>
              {llm.data_policy_note}
            </p>
          </div>
        ) : null}
      </div>
    </div>
  );
}

export function AiFeaturesSection({ capabilities }: { capabilities: CapabilitiesOut }) {
  const { whisper } = capabilities;
  const llm = resolveLlm(capabilities);
  return (
    <SettingsSection id="ai" title="AI 機能" description="LLM と音声認識は任意です。未設定でも代替手段で動作します。">
      <div className="flex flex-col divide-y divide-border px-5 pb-5">
        {llm ? <ClaudeRouteRow llm={llm} /> : null}
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

/** Where a fetched model list (OpenRouter) came from, how many models it has and how old it is. */
function ModelListStatus({ provider }: { provider: ProviderInfo }) {
  const now = useNow();
  const source = modelsSourceLabel(provider);
  const unknownPrice = provider.models.filter((model) => !isPriceKnown(model)).length;
  const fetchedAt = provider.models_fetched_at;
  const count = `${formatCount(provider.models.length)} モデル${unknownPrice > 0 ? `（料金未取得 ${formatCount(unknownPrice)}・選択不可）` : ""}`;
  return (
    <div className="flex flex-col gap-1 rounded-md border border-border bg-surface-raised/40 px-3 py-2 text-xs leading-5 text-muted-foreground">
      <p className="flex items-start gap-2">
        <ListChecks className="mt-0.5 size-3.5 shrink-0 text-subtle-foreground" aria-hidden />
        <span>
          <span className="text-foreground/90">モデル一覧: {source}</span> ・ {count}
          {/* The fetch time of the listing (also of a cached one); the built-in candidates have none. */}
          {fetchedAt ? (
            <span title={formatDateTime(fetchedAt)}> ・ 取得 {now > 0 ? formatRelativeTime(fetchedAt, now) : formatDateTime(fetchedAt)}</span>
          ) : null}
        </span>
      </p>
      {provider.models_note ? <p className="pl-5.5">{provider.models_note}</p> : null}
    </div>
  );
}

export function ProvidersSection({ capabilities }: { capabilities: CapabilitiesOut }) {
  return (
    <SettingsSection
      id="providers"
      title="動画生成プロバイダー"
      description="API キーはバックエンドの環境変数（KLING_API_KEY / RUNWAY_API_KEY / GOOGLE_API_KEY / OPENROUTER_API_KEY）で設定します。"
    >
      <ul className="flex flex-col divide-y divide-border border-t border-border">
        {capabilities.providers.map((provider) => {
          const unverified = provider.models.filter((model) => !model.verified).length;
          return (
            <li key={provider.id} className="flex flex-col gap-2 px-5 py-4" data-provider={provider.id}>
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
                {isRunway(provider.id) ? <PoweredByRunway /> : null}
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
              {provider.models_source ? <ModelListStatus provider={provider} /> : null}
              {provider.models.length > 0 ? (
                <p className="text-xs text-subtle-foreground">
                  モデル: {provider.models.map((model) => model.name || model.id).join(" / ")}
                </p>
              ) : null}
              {isOpenRouter(provider.id) ? (
                <p className="text-xs leading-5 text-subtle-foreground">
                  OpenRouter は各社の動画モデルを中継します。プロンプトは OpenRouter とモデルの提供元に送られ、音声の生成はオフで依頼します。
                  OPENROUTER_API_KEY だけを設定した場合は、Claude の絵コンテも OpenRouter 経由になります（上の「AI 機能」）。
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
                {isPriceKnown(model) && model.price_per_second_usd !== null ? (
                  <>
                    <TableCell className="text-right whitespace-nowrap tabular">{formatPricePerSecond(model.price_per_second_usd)}</TableCell>
                    <TableCell className="text-right whitespace-nowrap tabular">{formatUsd(model.price_per_second_usd * 5)}</TableCell>
                  </>
                ) : (
                  <>
                    <TableCell className="text-right whitespace-nowrap text-muted-foreground">料金未取得</TableCell>
                    <TableCell className="text-right text-muted-foreground">—</TableCell>
                  </>
                )}
                <TableCell className="whitespace-nowrap text-muted-foreground tabular">
                  {clipDurationsLabel(model.clip_durations, model.max_clip_duration)}
                </TableCell>
                <TableCell>
                  <div className="flex flex-col items-start gap-1">
                    <span className="whitespace-nowrap text-muted-foreground">{model.resolution}</span>
                    {model.supports_vertical ? (
                      <Badge tone="success">9:16 対応</Badge>
                    ) : isOpenRouter(provider.id) ? (
                      // The listing does not say; OpenRouter is always asked for 9:16 (an unsupported model refuses it).
                      <Badge tone="warning">9:16 未確認</Badge>
                    ) : (
                      <Badge tone="warning">クロップ</Badge>
                    )}
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
