/**
 * Claude routes (storyboards / prompt translation) and the OpenRouter model listing:
 * Japanese labels and helpers shared by the settings, generate and batch screens.
 *
 * Route order on the server (`CapabilitiesOut.llm`): ANTHROPIC_API_KEY → Anthropic directly,
 * else OPENROUTER_API_KEY → Claude via OpenRouter, else the template (no LLM).
 */
import type { CapabilitiesOut, LlmInfo, LlmRoute, ModelsSource, ProviderInfo, ProviderModelInfo, Storyboard } from "./types";

export const LLM_ROUTE_LABELS: Record<LlmRoute, string> = {
  anthropic: "Anthropic 直接",
  openrouter: "OpenRouter 経由",
};

/** 「Anthropic 直接」/「OpenRouter 経由」, or 「テンプレート」 when Claude is not used. */
export function llmRouteLabel(route: LlmRoute | null | undefined): string {
  return route ? LLM_ROUTE_LABELS[route] : "テンプレート";
}

/** 「Claude（OpenRouter 経由）」 — `Claude` alone when the route is unknown. */
export function claudeLabel(route: LlmRoute | null | undefined): string {
  return route ? `Claude（${LLM_ROUTE_LABELS[route]}）` : "Claude";
}

/**
 * The Claude route the server reports. A backend without the `llm` block only knew the direct
 * route, so `claude_configured` then means Anthropic; `undefined` while capabilities load.
 */
export function resolveLlm(capabilities: CapabilitiesOut | undefined): LlmInfo | undefined {
  if (!capabilities) return undefined;
  const llm = capabilities.llm;
  if (llm && (llm.route !== null || !capabilities.claude_configured)) return llm;
  return {
    route: capabilities.claude_configured ? "anthropic" : null,
    model: llm?.model ?? null,
    data_policy_note: "",
    verified: false,
  };
}

/** Which generator produced a storyboard: 「Claude（OpenRouter 経由）」 or 「テンプレート」. */
export function storyboardSourceLabel(storyboard: Pick<Storyboard, "source" | "route">): string {
  return storyboard.source === "claude" ? claudeLabel(storyboard.route) : "テンプレート";
}

/** For a template storyboard made after a Claude call that could not be used (the warnings say why), else null. */
export function storyboardFallbackNote(storyboard: Pick<Storyboard, "source" | "route">): string | null {
  if (storyboard.source !== "template" || !storyboard.route) return null;
  return `${claudeLabel(storyboard.route)}の応答を使えなかったため、テンプレートで作成しました`;
}

// --- OpenRouter video models ------------------------------------------------------------------------

export const OPENROUTER_PROVIDER_ID = "openrouter";

export function isOpenRouter(id: string | null | undefined): boolean {
  return id === OPENROUTER_PROVIDER_ID;
}

/** False when the model has no readable price (it cannot be used: the batch budget cannot be reserved). */
export function isPriceKnown(model: Pick<ProviderModelInfo, "price_known" | "price_per_second_usd">): boolean {
  return model.price_known !== false && typeof model.price_per_second_usd === "number";
}

export const MODELS_SOURCE_LABELS: Record<ModelsSource, string> = {
  live: "OpenRouter から取得",
  cache: "キャッシュ",
  fallback: "組み込みの候補",
};

/** Label of where a provider's model list came from, or null for providers with a fixed list. */
export function modelsSourceLabel(provider: Pick<ProviderInfo, "models_source">): string | null {
  return provider.models_source ? (MODELS_SOURCE_LABELS[provider.models_source] ?? provider.models_source) : null;
}

const VENDOR_LABELS: Record<string, string> = {
  google: "Google",
  kwaivgi: "Kuaishou（Kling）",
  minimax: "MiniMax",
  alibaba: "Alibaba",
  bytedance: "ByteDance",
  openai: "OpenAI",
  runway: "Runway",
  lumaai: "Luma AI",
  pixverse: "PixVerse",
  "black-forest-labs": "Black Forest Labs",
};

/** The company behind an OpenRouter model id (`google/veo-3.1-lite` → Google), or null. */
export function openRouterVendor(modelId: string | null | undefined): string | null {
  const prefix = modelId?.includes("/") ? modelId.split("/", 1)[0].trim() : "";
  if (!prefix) return null;
  return VENDOR_LABELS[prefix.toLowerCase()] ?? prefix;
}
