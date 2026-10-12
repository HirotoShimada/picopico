import { DEFAULT_COUNTS, DEFAULT_DURATIONS, DEFAULT_GENERATION_SETTINGS, PRESET_IDS, PROVIDER_LABELS } from "@/lib/constants";
import type {
  GenerationSettings,
  GenerationSettingsInput,
  LimitsInfo,
  LyricStyleChoice,
  PresetId,
  ProjectOut,
  ProviderId,
  ReuseMode,
  StartStrategy,
  TextStyle,
} from "@/lib/types";

import { compactTextStyle, toLyricStyleChoice } from "./text-style";

export const CUSTOM = "custom";

/** Editable state of the generate form (choices kept separate from custom numbers). */
export interface GenerateFormState {
  preset: PresetId;
  customWorld: string;
  provider: ProviderId;
  /** `null` = provider default. */
  model: string | null;
  /** `"15" | "30" | "60" | "custom"` */
  durationChoice: string;
  customDuration: number | null;
  /** `"1" | "3" | … | "custom"` */
  countChoice: string;
  customCount: number | null;
  /** `auto` = the preset's styles rotated per video. */
  lyricStyle: LyricStyleChoice;
  textStyle: TextStyle | null;
  colorGrade: string;
  startStrategy: StartStrategy;
  startSec: number | null;
  reuse: ReuseMode;
  limitNewClips: boolean;
  maxNewClips: number | null;
  /** `null` = project budget, else the server default. */
  budget: number | null;
  /** Ask Claude for a fresh storyboard (never carried over from the previous batch). */
  newStoryboard: boolean;
  /** Let the planner flip shots horizontally (mirrors text in the footage; off by default). */
  allowMirror: boolean;
}

export type FormErrors = Partial<Record<"customWorld" | "duration" | "count" | "startSec" | "maxNewClips" | "budget", string>>;

// Every provider id (PROVIDER_LABELS is a Record over ProviderId, so a new provider cannot be missed here).
const PROVIDER_IDS = Object.keys(PROVIDER_LABELS) as ProviderId[];
const START_STRATEGIES: readonly StartStrategy[] = ["segment", "chorus", "varied", "custom"];
const REUSE_MODES: readonly ReuseMode[] = ["max_variety", "balanced", "economy"];

function pick<T extends string>(value: unknown, allowed: readonly T[], fallback: T): T {
  return typeof value === "string" && (allowed as readonly string[]).includes(value) ? (value as T) : fallback;
}

function choiceFor(value: number, choices: readonly number[]): { choice: string; custom: number | null } {
  return choices.includes(value) ? { choice: String(value), custom: null } : { choice: CUSTOM, custom: value };
}

/**
 * The preset 感情・世界観推定 suggests for a first run: only when there is no previous batch and
 * the project is still on the default preset without a custom world (an explicit choice wins).
 */
export function suggestedInitialPreset(
  project: ProjectOut,
  previous: Partial<GenerationSettings> | null,
  recommended: readonly string[] | null | undefined,
): PresetId | null {
  if (previous || project.custom_world || project.world_preset !== DEFAULT_GENERATION_SETTINGS.preset) return null;
  const top = recommended?.find((id) => (PRESET_IDS as readonly string[]).includes(id) && id !== "custom");
  return top && top !== project.world_preset ? (top as PresetId) : null;
}

/**
 * Initial state: the latest batch's settings when there is one (so a second run
 * starts from what worked), otherwise the project's world preset (or the preset the
 * world estimate suggests, see {@link suggestedInitialPreset}) and defaults.
 * `new_storyboard` is never carried over.
 */
export function initialFormState(
  project: ProjectOut,
  previous: Partial<GenerationSettings> | null,
  options: { durations?: readonly number[]; counts?: readonly number[]; suggestedPreset?: PresetId | null } = {},
): GenerateFormState {
  const defaults = DEFAULT_GENERATION_SETTINGS;
  const durations = options.durations ?? DEFAULT_DURATIONS;
  const counts = options.counts ?? DEFAULT_COUNTS;
  const source: Partial<GenerationSettings> = previous ?? {
    preset: options.suggestedPreset ?? pick(project.world_preset, PRESET_IDS, defaults.preset),
    custom_world: project.custom_world,
  };
  const duration = choiceFor(Number(source.duration_sec ?? defaults.duration_sec), durations);
  const count = choiceFor(Number(source.count ?? defaults.count), counts);
  return {
    preset: pick(source.preset, PRESET_IDS, defaults.preset),
    customWorld: source.custom_world ?? project.custom_world ?? "",
    provider: pick(source.provider, PROVIDER_IDS, defaults.provider),
    model: source.model ?? null,
    durationChoice: duration.choice,
    customDuration: duration.custom,
    countChoice: count.choice,
    customCount: count.custom,
    lyricStyle: toLyricStyleChoice(source.lyric_style ?? defaults.lyric_style),
    textStyle: compactTextStyle(source.text_style),
    colorGrade: source.color_grade || defaults.color_grade,
    startStrategy: pick(source.start_strategy, START_STRATEGIES, defaults.start_strategy),
    startSec: source.start_sec ?? null,
    reuse: pick(source.reuse, REUSE_MODES, defaults.reuse),
    limitNewClips: source.max_new_clips !== null && source.max_new_clips !== undefined,
    maxNewClips: source.max_new_clips ?? null,
    budget: source.budget_limit_usd ?? null,
    newStoryboard: false,
    allowMirror: source.allow_mirror === true,
  };
}

export function formDuration(state: GenerateFormState): number | null {
  return state.durationChoice === CUSTOM ? state.customDuration : Number(state.durationChoice);
}

export function formCount(state: GenerateFormState): number | null {
  return state.countChoice === CUSTOM ? state.customCount : Number(state.countChoice);
}

export interface ValidatedForm {
  errors: FormErrors;
  /** Request body for estimate / batch creation, or `null` while the form is invalid. */
  settings: GenerationSettingsInput | null;
}

/** Validate against the server limits (`schemas.GenerationSettings`). */
export function validateForm(state: GenerateFormState, limits: LimitsInfo): ValidatedForm {
  const errors: FormErrors = {};
  const duration = formDuration(state);
  const count = formCount(state);

  if (state.preset === "custom" && !state.customWorld.trim()) {
    errors.customWorld = "カスタム世界観の説明を入力してください。";
  }
  if (duration === null || !Number.isFinite(duration) || duration < limits.min_duration || duration > limits.max_duration) {
    errors.duration = `動画の長さは ${limits.min_duration}〜${limits.max_duration} 秒で指定してください。`;
  }
  if (count === null || !Number.isInteger(count) || count < 1 || count > limits.max_count) {
    errors.count = `本数は 1〜${limits.max_count} 本の整数で指定してください。`;
  }
  if (state.startStrategy === "custom" && (state.startSec === null || state.startSec < 0)) {
    errors.startSec = "開始位置を指定してください。";
  }
  if (state.limitNewClips && (state.maxNewClips === null || !Number.isInteger(state.maxNewClips) || state.maxNewClips < 0 || state.maxNewClips > 500)) {
    errors.maxNewClips = "新規生成の上限は 0〜500 の整数で指定してください。";
  }
  if (state.budget !== null && (state.budget < 0 || state.budget > 100_000)) {
    errors.budget = "予算は 0〜100,000 ドルの範囲で指定してください。";
  }

  if (Object.keys(errors).length > 0 || duration === null || count === null) return { errors, settings: null };

  const settings: GenerationSettingsInput = {
    preset: state.preset,
    custom_world: state.preset === "custom" ? state.customWorld.trim() : null,
    provider: state.provider,
    model: state.model,
    duration_sec: duration,
    count,
    lyric_style: state.lyricStyle,
    text_style: compactTextStyle(state.textStyle),
    color_grade: state.colorGrade,
    start_strategy: state.startStrategy,
    start_sec: state.startStrategy === "custom" ? state.startSec : null,
    reuse: state.reuse,
    max_new_clips: state.limitNewClips ? state.maxNewClips : null,
    budget_limit_usd: state.budget,
    seed: null,
    new_storyboard: state.newStoryboard,
    allow_mirror: state.allowMirror,
  };
  return { errors, settings };
}
