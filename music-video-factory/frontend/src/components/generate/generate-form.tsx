"use client";

import { Play, RotateCcw } from "lucide-react";
import { useRouter } from "next/navigation";
import * as React from "react";
import { useSWRConfig } from "swr";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { ErrorState } from "@/components/ui/error-state";
import { Field } from "@/components/ui/field";
import { Label } from "@/components/ui/label";
import { NumberInput } from "@/components/ui/number-input";
import { RadioCards } from "@/components/ui/radio-cards";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { toast } from "@/components/ui/toast";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { WorldPresetPicker } from "@/components/ui/world-preset-picker";
import { useCapabilities, useLimits, usePresets } from "@/hooks/use-capabilities";
import { useClips } from "@/hooks/use-clips";
import { apiPaths, createBatch, errorMessage } from "@/lib/api";
import { DEFAULT_COUNTS, DEFAULT_DURATIONS, providerLabel } from "@/lib/constants";
import { formatCount, formatDuration, formatUsd } from "@/lib/format";
import type {
  EstimateComparison,
  GenerationSettings,
  PresetId,
  ProjectOut,
  ProviderId,
  ReuseMode,
  WorldEstimate,
} from "@/lib/types";
import { emotionLabel } from "@/components/audio/world-estimate-card";
import { rightsResetName, useRightsResets } from "@/components/rights/rights-reset";

import { ColorGradeSelect, useColorGrades } from "./color-grade-select";
import { EstimatePanel, ProviderComparison } from "./estimate-panel";
import { FormSection } from "./form-section";
import {
  CUSTOM,
  formCount,
  formDuration,
  initialFormState,
  suggestedInitialPreset,
  validateForm,
  type GenerateFormState,
} from "./form-state";
import { batchHref } from "./links";
import { LyricStylePicker, useLyricStyles } from "./lyric-style-picker";
import { LyricStyleSample } from "./lyric-style-sample";
import { modelUnavailableReason, ProviderPicker, providerUnavailableReason, resolveModel } from "./provider-picker";
import { computeReadiness, estimateUnavailableReason, isReady, ReadinessList } from "./readiness";
import { StartPositionField } from "./start-position-field";
import { StoryboardPreview } from "./storyboard-preview";
import { TextStyleControls } from "./text-style-controls";
import { autoLyricStyles, colorGradeName, lyricStyleChoiceName, previewStyleId } from "./text-style";
import { useEstimate } from "./use-estimate";
import { mirrorLabel, reuseLabel, startStrategyLabel } from "./vocab";

const REUSE_OPTIONS: { value: ReuseMode; title: string; description: string }[] = [
  {
    value: "max_variety",
    title: "バリエーション最大",
    description: "ショットごとに新しい素材を生成します。映像の重複は最小ですが、費用は最大になります。",
  },
  {
    value: "balanced",
    title: "バランス",
    description: "ショット数の約 1/3 の素材を生成し、動画間で使い回します。",
  },
  {
    value: "economy",
    title: "節約",
    description: "素材を積極的に使い回して費用を抑えます。動画間で同じ映像が増えます。",
  },
];

function randomSeed(): number {
  const values = new Uint32Array(1);
  crypto.getRandomValues(values);
  return (values[0] % 2_147_483_646) + 1;
}

function SummaryRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="shrink-0 text-xs text-muted-foreground">{label}</dt>
      <dd className="min-w-0 truncate text-right text-xs text-foreground">{value}</dd>
    </div>
  );
}

function WorldEstimateHint({
  estimate,
  presetName,
  current,
  suggestedPreset,
  onPick,
}: {
  estimate: WorldEstimate;
  presetName: (id: string) => string;
  current: PresetId;
  suggestedPreset: PresetId | null;
  onPick: (preset: PresetId) => void;
}) {
  const emotions = [...estimate.emotions, ...estimate.themes].map(emotionLabel).join("・");
  return (
    <div className="flex flex-col gap-1.5 rounded-md border border-border bg-surface-raised/50 px-3 py-2.5 text-xs leading-5 text-muted-foreground">
      <p>
        <span className="text-foreground">感情・世界観の推定</span>
        {emotions ? `: ${emotions}` : ""}（{estimate.analysis_used ? "楽曲解析・" : ""}歌詞・ムードと目的から）
        {suggestedPreset && current === suggestedPreset ? " ・ 推定に基づいておすすめの世界観を選んでいます。" : null}
      </p>
      <div className="flex flex-wrap items-center gap-1.5">
        <span>おすすめ</span>
        {estimate.recommended_presets.map((id) => (
          <Button
            key={id}
            type="button"
            size="xs"
            variant={id === current ? "secondary" : "ghost"}
            aria-pressed={id === current}
            onClick={() => onPick(id as PresetId)}
          >
            {presetName(id)}
          </Button>
        ))}
      </div>
    </div>
  );
}

export interface GenerateFormProps {
  project: ProjectOut;
  /** Settings of the project's latest batch (start from them), if any. */
  previous: Partial<GenerationSettings> | null;
  /** 感情・世界観推定 of the project (recommendations; pre-selects the preset on a first run). */
  worldEstimate?: WorldEstimate | null;
}

/** All generation settings, the live estimate, readiness checks and the 生成開始 action. */
export function GenerateForm({ project, previous, worldEstimate = null }: GenerateFormProps) {
  const router = useRouter();
  const { mutate } = useSWRConfig();
  const { data: capabilities, error: capabilitiesError, mutate: reloadCapabilities, isLoading: capabilitiesLoading } = useCapabilities();
  const { presets } = usePresets();
  const limits = useLimits();
  const lyricStyles = useLyricStyles();
  const grades = useColorGrades();
  const { data: clips } = useClips(project.id);

  const durations = capabilities?.durations?.length ? capabilities.durations : DEFAULT_DURATIONS;
  const counts = capabilities?.counts?.length ? capabilities.counts : DEFAULT_COUNTS;

  // On a first run the world estimate may pick the starting preset (see suggestedInitialPreset).
  const [suggestedPreset] = React.useState(() => suggestedInitialPreset(project, previous, worldEstimate?.recommended_presets));
  const [state, setState] = React.useState<GenerateFormState>(() =>
    initialFormState(project, previous, { durations, counts, suggestedPreset }),
  );
  const update = React.useCallback((patch: Partial<GenerateFormState>) => setState((current) => ({ ...current, ...patch })), []);

  // One random seed per form: the estimate and the batch then describe exactly the same plan
  // (the server would otherwise estimate with seed 0 and run with a random one).
  const [seed, setSeed] = React.useState(randomSeed);
  const validated = React.useMemo(() => validateForm(state, limits), [state, limits]);
  const errors = validated.errors;
  const settings = React.useMemo(() => (validated.settings ? { ...validated.settings, seed } : null), [validated.settings, seed]);
  const duration = formDuration(state);
  const count = formCount(state);
  const songDuration = project.analysis?.duration ?? project.audio?.duration ?? null;

  const providers = capabilities?.providers ?? [];
  const providerInfo = providers.find((p) => p.id === state.provider);
  const modelInfo = resolveModel(providerInfo, state.model);
  const libraryClipCount = clips?.filter((clip) => clip.source === "upload" && clip.status === "ready").length;
  const providerName = providerInfo?.name || providerLabel(state.provider);
  const providerReason = providerInfo
    ? (providerUnavailableReason(providerInfo, libraryClipCount) ?? (modelInfo ? modelUnavailableReason(modelInfo, providerInfo.kind) : null))
    : capabilities
      ? "このプロバイダーはサーバーに登録されていません。"
      : null;

  const estimateReason = estimateUnavailableReason(project);
  const estimate = useEstimate(project.id, settings, estimateReason === null);
  const rightsResets = useRightsResets(project);

  const readiness = computeReadiness({
    project,
    rightsToRecheck: rightsResets.map((reset) => rightsResetName(reset.key)),
    providerName,
    providerReason,
    formInvalid: settings === null,
    estimate: estimate.data,
    estimateError: !!estimate.error,
    estimateStale: estimate.stale,
  });
  const canStart = isReady(readiness) && settings !== null && estimate.current && !!capabilities;
  const firstBlocker = readiness.find((item) => item.level === "blocked" || item.level === "pending");

  const presetInfo = presets.find((preset) => preset.id === state.preset);
  const palette = presetInfo?.palette;
  // `auto` rotates the preset's styles per video; previews and placeholders show the first video's style.
  const autoStyles = React.useMemo(() => autoLyricStyles(presetInfo), [presetInfo]);
  const previewStyle = previewStyleId(state.lyricStyle, autoStyles);
  const budgetSource = state.budget !== null ? "この生成" : project.budget_limit_usd !== null ? "プロジェクト設定" : "既定";

  const [confirmOpen, setConfirmOpen] = React.useState(false);
  const [submitting, setSubmitting] = React.useState(false);
  const [submitError, setSubmitError] = React.useState<unknown>(null);

  async function startBatch() {
    if (!settings) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const batch = await createBatch(project.id, settings);
      setSeed(randomSeed()); // a later batch from this form is a new variation
      toast.success("生成を開始しました", `${formatCount(count)}本の動画を生成します。進捗はこの画面で確認できます。`);
      void mutate(apiPaths.projectBatches(project.id));
      void mutate(apiPaths.project(project.id));
      void mutate(apiPaths.dashboard);
      router.push(batchHref(batch.id));
    } catch (error) {
      setSubmitError(error);
    } finally {
      setSubmitting(false);
    }
  }

  function handleStart() {
    if (!canStart) return;
    if ((estimate.data?.total_usd ?? 0) > 0) setConfirmOpen(true);
    else void startBatch();
  }

  function selectComparison(row: EstimateComparison) {
    const target = providers.find((p) => p.id === row.provider);
    update({ provider: row.provider as ProviderId, model: target?.default_model === row.model ? null : row.model });
  }

  function resetToDefaults() {
    setState(initialFormState(project, null, { durations, counts }));
  }

  return (
    <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1fr)_380px]">
      <div className="flex min-w-0 flex-col gap-6">
        <FormSection
          step={1}
          title="世界観"
          description="映像全体のトーン・ライティング・色を決めます。生成プロンプトは英語に変換して送信されます。"
          action={
            <Button variant="ghost" size="xs" onClick={resetToDefaults} title="プロジェクトの設定と既定値に戻します">
              <RotateCcw aria-hidden />
              既定値に戻す
            </Button>
          }
        >
          {worldEstimate ? (
            <WorldEstimateHint
              estimate={worldEstimate}
              presetName={(id) => presets.find((preset) => preset.id === id)?.name_ja ?? id}
              current={state.preset}
              suggestedPreset={suggestedPreset}
              onPick={(preset) => update({ preset })}
            />
          ) : null}
          <WorldPresetPicker
            presets={presets}
            value={state.preset}
            onValueChange={(value) => update({ preset: value as PresetId })}
            aria-label="世界観プリセット"
          />
          {state.preset === CUSTOM ? (
            <Field
              label="カスタム世界観"
              htmlFor="generate-custom-world"
              required
              error={errors.customWorld}
              description="場所・時間帯・天気・色・質感などを具体的に（日本語可）。実在の人物名やアーティスト名は入力しないでください。"
            >
              <Textarea
                id="generate-custom-world"
                rows={3}
                maxLength={4000}
                value={state.customWorld}
                onChange={(event) => update({ customWorld: event.target.value })}
                placeholder="例: 雨上がりの夜の港町。濡れた石畳に街灯が反射し、霧の中を灯台の光がゆっくり回る。"
                aria-invalid={errors.customWorld ? true : undefined}
              />
            </Field>
          ) : null}
        </FormSection>

        <FormSection
          step={2}
          title="動画生成エンジン"
          description="未設定のプロバイダーは、バックエンドに API キーを設定すると選べるようになります。"
        >
          {capabilitiesError && !capabilities ? (
            <ErrorState size="sm" error={capabilitiesError} title="プロバイダー情報を取得できませんでした" onRetry={() => void reloadCapabilities()} />
          ) : capabilitiesLoading || !capabilities ? (
            <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2 xl:grid-cols-3">
              {Array.from({ length: 5 }, (_, i) => (
                <Skeleton key={i} className="h-24 w-full" />
              ))}
            </div>
          ) : (
            <ProviderPicker
              projectId={project.id}
              providers={providers}
              provider={state.provider}
              model={state.model}
              onChange={(provider, model) => update({ provider, model })}
              libraryClipCount={libraryClipCount}
            />
          )}
        </FormSection>

        <FormSection step={3} title="長さ・本数・開始位置">
          <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
            <Field label="1 本の長さ" error={errors.duration}>
              <div className="flex flex-wrap items-center gap-2">
                <ToggleGroup
                  type="single"
                  value={state.durationChoice}
                  onValueChange={(value) => {
                    if (!value) return;
                    update(value === CUSTOM ? { durationChoice: value, customDuration: state.customDuration ?? duration } : { durationChoice: value });
                  }}
                  aria-label="1 本の長さ"
                >
                  {durations.map((value) => (
                    <ToggleGroupItem key={value} value={String(value)}>
                      {value}秒
                    </ToggleGroupItem>
                  ))}
                  <ToggleGroupItem value={CUSTOM}>指定</ToggleGroupItem>
                </ToggleGroup>
                {state.durationChoice === CUSTOM ? (
                  <NumberInput
                    value={state.customDuration}
                    onValueChange={(value) => update({ customDuration: value })}
                    min={limits.min_duration}
                    max={limits.max_duration}
                    step={1}
                    suffix="秒"
                    className="w-28"
                    aria-label="1 本の長さ（秒）"
                    aria-invalid={errors.duration ? true : undefined}
                  />
                ) : null}
              </div>
              {songDuration !== null && duration !== null && duration > songDuration ? (
                <p className="text-xs text-warning-foreground">曲の長さ（{formatDuration(songDuration)}）より長いため、曲の長さに短縮されます。</p>
              ) : null}
            </Field>

            <Field label="本数" error={errors.count}>
              <div className="flex flex-wrap items-center gap-2">
                <ToggleGroup
                  type="single"
                  value={state.countChoice}
                  onValueChange={(value) => {
                    if (!value) return;
                    update(value === CUSTOM ? { countChoice: value, customCount: state.customCount ?? count } : { countChoice: value });
                  }}
                  aria-label="本数"
                >
                  {counts.map((value) => (
                    <ToggleGroupItem key={value} value={String(value)}>
                      {value}本
                    </ToggleGroupItem>
                  ))}
                  <ToggleGroupItem value={CUSTOM}>指定</ToggleGroupItem>
                </ToggleGroup>
                {state.countChoice === CUSTOM ? (
                  <NumberInput
                    value={state.customCount}
                    onValueChange={(value) => update({ customCount: value })}
                    min={1}
                    max={limits.max_count}
                    step={1}
                    decimals={0}
                    suffix="本"
                    className="w-28"
                    aria-label="本数"
                    aria-invalid={errors.count ? true : undefined}
                  />
                ) : null}
              </div>
            </Field>
          </div>

          <div className="flex flex-col gap-2">
            <Label>開始位置</Label>
            <StartPositionField
              projectId={project.id}
              analysis={project.analysis}
              segment={
                project.segment_start !== null && project.segment_end !== null
                  ? { start: project.segment_start, end: project.segment_end }
                  : null
              }
              duration={duration}
              strategy={state.startStrategy}
              startSec={state.startSec}
              onChange={(patch) => update(patch)}
              error={errors.startSec}
            />
          </div>
        </FormSection>

        <FormSection
          step={4}
          title="歌詞の表示"
          description="歌詞は音楽に合わせて焼き込まれます。Instagram ストーリーズの上下の UI に隠れない範囲に配置されます。"
        >
          <LyricStylePicker
            value={state.lyricStyle}
            onValueChange={(lyricStyle) => update({ lyricStyle })}
            autoStyles={autoStyles}
            textStyle={state.textStyle}
            palette={palette}
          />
          <div className="grid grid-cols-1 gap-6 border-t border-border pt-5 md:grid-cols-[minmax(0,1fr)_168px]">
            <TextStyleControls
              idPrefix="generate-text"
              styleId={previewStyle}
              value={state.textStyle}
              onValueChange={(textStyle) => update({ textStyle })}
            />
            <div className="flex flex-col gap-1.5">
              <span className="text-xs text-muted-foreground">配置プレビュー</span>
              <LyricStyleSample variant="frame" showSafeArea styleId={previewStyle} textStyle={state.textStyle} palette={palette} />
              <span className="text-2xs leading-4 text-subtle-foreground">
                点線の外は IG の UI に隠れる範囲です（近似表示）。
                {state.lyricStyle === "auto"
                  ? `1 本目のスタイル（${lyricStyleChoiceName(previewStyle, lyricStyles)}）を表示しています。文字の設定はすべての動画に適用されます。`
                  : null}
              </span>
            </div>
          </div>
        </FormSection>

        <FormSection step={5} title="映像の仕上げと素材の再利用">
          <Field
            label="カラーグレード"
            htmlFor="generate-color-grade"
            description="「自動」は世界観に合うグレードを動画ごとに変えます。"
            className="max-w-sm"
          >
            <ColorGradeSelect id="generate-color-grade" value={state.colorGrade} onValueChange={(colorGrade) => update({ colorGrade })} includeAuto />
          </Field>

          <div className="flex max-w-2xl items-start gap-3">
            <Switch
              id="generate-allow-mirror"
              checked={state.allowMirror}
              onCheckedChange={(allowMirror) => update({ allowMirror })}
              aria-describedby="generate-allow-mirror-note"
              className="mt-0.5"
            />
            <div className="flex flex-col gap-0.5">
              <Label htmlFor="generate-allow-mirror">左右反転を使う</Label>
              <p id="generate-allow-mirror-note" className="text-xs leading-5 text-muted-foreground">
                一部のショットを左右反転して、動画ごとの見た目の変化を増やします。素材に映る文字や看板も鏡文字になるため、文字が映る素材ではオフのままにしてください。
              </p>
            </div>
          </div>

          <div className="flex flex-col gap-2">
            <Label>素材の再利用</Label>
            <RadioCards
              options={REUSE_OPTIONS}
              value={state.reuse}
              onValueChange={(reuse) => update({ reuse })}
              aria-label="素材の再利用"
              className="sm:grid-cols-3"
            />
            {state.provider === "library" ? (
              <p className="text-2xs text-subtle-foreground">アップロード素材を使う場合は新規生成を行わないため、この設定は素材の割り当て方にのみ影響します。</p>
            ) : (
              <p className="text-2xs text-subtle-foreground">同じプロンプト・設定で生成済みの素材は自動的に再利用され、費用はかかりません。</p>
            )}
          </div>

          <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
            <Field
              label="新規生成する素材の上限"
              error={errors.maxNewClips}
              description="上限を超える分は生成済み素材の使い回しで補います。"
            >
              <div className="flex items-center gap-3">
                <Switch
                  checked={state.limitNewClips}
                  onCheckedChange={(checked) => update({ limitNewClips: checked, maxNewClips: state.maxNewClips ?? estimate.data?.new_clips ?? 8 })}
                  aria-label="新規生成の上限を設定"
                />
                {state.limitNewClips ? (
                  <NumberInput
                    value={state.maxNewClips}
                    onValueChange={(value) => update({ maxNewClips: value })}
                    min={0}
                    max={500}
                    step={1}
                    decimals={0}
                    suffix="本"
                    className="w-28"
                    aria-label="新規生成の上限"
                    aria-invalid={errors.maxNewClips ? true : undefined}
                  />
                ) : (
                  <span className="text-xs text-muted-foreground">上限なし</span>
                )}
              </div>
            </Field>

            <Field
              label="予算上限"
              htmlFor="generate-budget"
              optional
              error={errors.budget}
              description={
                project.budget_limit_usd !== null
                  ? `未入力の場合はプロジェクトの予算（${formatUsd(project.budget_limit_usd)}）を使います。`
                  : "未入力の場合はサーバーの既定の上限を使います。見積もりが上限を超えると開始できません。"
              }
            >
              <NumberInput
                id="generate-budget"
                value={state.budget}
                onValueChange={(budget) => update({ budget })}
                min={0}
                max={100_000}
                step={1}
                decimals={2}
                prefix="$"
                placeholder={project.budget_limit_usd !== null ? String(project.budget_limit_usd) : "既定"}
                className="w-40"
                aria-invalid={errors.budget ? true : undefined}
              />
            </Field>
          </div>
        </FormSection>

        {capabilities?.claude_configured && state.provider !== "library" ? (
          <div className="flex items-start justify-between gap-4 rounded-md border border-border bg-surface px-4 py-3">
            <div className="flex flex-col gap-0.5">
              <Label htmlFor="generate-new-storyboard">絵コンテを Claude で新しく作り直す</Label>
              <p className="text-xs leading-5 text-muted-foreground">
                オフの場合、同じ世界観・楽曲・歌詞で前回 Claude が作った絵コンテを再利用します（主人公や世界観がバッチ間で揃い、生成済みの素材も再利用できます）。オンにすると Claude
                の利用料と、新しい素材の生成費用がかかります。この設定は次回の生成には引き継がれません。
              </p>
            </div>
            <Switch
              id="generate-new-storyboard"
              checked={state.newStoryboard}
              onCheckedChange={(newStoryboard) => update({ newStoryboard })}
            />
          </div>
        ) : null}

        <StoryboardPreview
          projectId={project.id}
          preset={state.preset}
          customWorld={state.preset === CUSTOM ? state.customWorld.trim() || null : null}
          disabledReason={state.preset === CUSTOM && !state.customWorld.trim() ? "カスタム世界観を入力するとプレビューできます。" : null}
          claudeConfigured={capabilities?.claude_configured}
          lyricsRightsMissing={project.lyric_lines.length > 0 && project.rights.lyrics_rights !== true}
        />
      </div>

      <aside className="flex min-w-0 flex-col gap-4 xl:sticky xl:top-20 xl:max-h-[calc(100vh-6rem)] xl:self-start xl:overflow-y-auto xl:pb-1">
        <Card>
          <CardHeader>
            <CardTitle>生成の概要</CardTitle>
            <CardDescription>
              {count !== null && duration !== null
                ? `${formatDuration(duration)} × ${formatCount(count)}本 = 合計 ${formatDuration(duration * count)}`
                : "長さと本数を指定してください"}
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <dl className="flex flex-col gap-1.5">
              <SummaryRow label="世界観" value={presets.find((p) => p.id === state.preset)?.name_ja ?? state.preset} />
              <SummaryRow
                label="生成エンジン"
                value={`${providerName}${modelInfo && providerInfo?.kind === "api" ? ` / ${modelInfo.name || modelInfo.id}` : ""}`}
              />
              <SummaryRow label="歌詞スタイル" value={lyricStyleChoiceName(state.lyricStyle, lyricStyles)} />
              <SummaryRow label="カラー" value={colorGradeName(state.colorGrade, grades)} />
              <SummaryRow label="開始位置" value={startStrategyLabel(state.startStrategy)} />
              <SummaryRow label="素材の再利用" value={reuseLabel(state.reuse)} />
              <SummaryRow label="左右反転" value={mirrorLabel(state.allowMirror)} />
            </dl>
            <div className="border-t border-border pt-4">
              <ReadinessList items={readiness} />
            </div>
            {submitError ? (
              <Alert tone="danger" title="生成を開始できませんでした">
                {errorMessage(submitError)}
              </Alert>
            ) : null}
            <div className="flex flex-col gap-2">
              <Button variant="primary" size="lg" className="w-full" onClick={handleStart} disabled={!canStart} loading={submitting}>
                {submitting ? null : <Play aria-hidden />}
                生成開始
              </Button>
              {!canStart && firstBlocker ? (
                <p className="text-center text-2xs text-subtle-foreground">{firstBlocker.title}</p>
              ) : estimate.data && canStart ? (
                <p className="text-center text-2xs text-subtle-foreground">
                  見積もり {formatUsd(estimate.data.total_usd)} ・ 開始後はジョブ画面で進捗を確認できます
                </p>
              ) : null}
            </div>
          </CardContent>
        </Card>

        <EstimatePanel state={estimate} unavailableReason={estimateReason} budgetSource={budgetSource} />
        <ProviderComparison estimate={estimate.data} stale={estimate.stale} onSelect={selectComparison} />
      </aside>

      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title="費用が発生する生成を開始しますか？"
        confirmLabel="生成を開始"
        onConfirm={startBatch}
        description={
          estimate.data
            ? `${providerName} で新規素材 ${formatCount(estimate.data.new_clips)} 本を生成します。見積もり合計は ${formatUsd(estimate.data.total_usd)} です。`
            : undefined
        }
      >
        {estimate.data?.pricing_verified === false ? (
          <Alert tone="warning">
            この価格は {estimate.data.as_of ?? "調査時点"} の公式料金表に基づく概算で、実際の API キーでは未検証です。実際の請求額はプロバイダーの管理画面で確認してください。
          </Alert>
        ) : null}
      </ConfirmDialog>
    </div>
  );
}
