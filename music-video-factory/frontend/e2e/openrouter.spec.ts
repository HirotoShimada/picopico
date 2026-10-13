import fs from "node:fs";
import path from "node:path";

import { expect, test, type APIRequestContext, type Locator, type Page } from "@playwright/test";

import {
  API_URL,
  apiContext,
  apiJson,
  demoFile,
  patchApiJson,
  requireDemoAssets,
  RIGHTS_KEYS,
  screenshot,
  SCREENSHOT_DIR,
  storeApiToken,
  waitFor,
  watchConsole,
} from "./support";

/**
 * OpenRouter in the UI (video provider + Claude route). There is no OPENROUTER_API_KEY / ANTHROPIC_API_KEY
 * here and openrouter.ai is unreachable, so:
 * - the "not configured" screens use the backend's real answer (OpenRouter with its built-in model candidates,
 *   Claude via the template);
 * - the "configured" screens patch the backend's real `GET /api/system/capabilities` (OpenRouter configured
 *   with a fetched listing incl. a model without a readable price, Claude via OpenRouter), the storyboard
 *   preview response (route/model as the backend reports them) and the usage / clip / batch JSON.
 * Estimate and batch requests go to the real backend, which still refuses the batch (no key).
 */

interface ModelDto {
  id: string;
  name: string;
  clip_durations: number[];
  max_clip_duration: number;
  resolution: string;
  supports_vertical: boolean;
  price_per_second_usd: number | null;
  price_note: string;
  verified: boolean;
  price_known?: boolean;
  default_duration?: number;
  text_to_video?: boolean;
  image_to_video?: boolean;
  as_of?: string;
  source_url?: string;
}
interface ProviderDto {
  id: string;
  name: string;
  configured: boolean;
  available: boolean;
  reason: string | null;
  models: ModelDto[];
  default_model?: string;
  models_source?: string;
  models_fetched_at?: string | null;
  models_note?: string;
}
interface LlmDto {
  route: "anthropic" | "openrouter" | null;
  model: string | null;
  data_policy_note: string;
  verified: boolean;
}
interface CapabilitiesDto {
  providers: ProviderDto[];
  claude_configured: boolean;
  llm?: LlmDto;
}
interface StoryboardDto {
  source: string;
  route?: string | null;
  model: string | null;
  usage: Record<string, number> | null;
  cost_usd: number;
  warnings: string[];
  scenes: unknown[];
}
interface ClipDto {
  id: string;
  source: string;
  provider: string;
  model: string | null;
  status: string;
  cost_usd: number;
}
interface BatchDto {
  id: string;
  status: string;
  settings: { provider: string; model: string | null };
  estimate: { provider: string; model: string } | null;
  storyboard: StoryboardDto | null;
  clips?: ClipDto[];
}

const ARTIST = "デモアーティスト";
const CLAUDE_VIA_OPENROUTER = "anthropic/claude-opus-5.5";
const FINAL_BATCH = ["completed", "partial", "failed", "cancelled", "budget_exceeded"];
/** `CapabilitiesOut.llm.data_policy_note` of the OpenRouter route (backend `app/services/llm.py`). */
const OPENROUTER_DATA_POLICY =
  "ANTHROPIC_API_KEY がないため、Claude には OpenRouter 経由で送ります。絵コンテの生成時に、歌詞の本文を含む同じ内容（楽曲名・ムード・目的・カスタム世界観・楽曲解析の要約）が OpenRouter と、その先の Claude の提供元（Anthropic / Amazon Bedrock / Google Vertex / Azure など）を通ります。毎回、データを収集しない提供元だけ（data_collection: deny）・データを保持しない提供元だけ（zdr: true）を指定しますが、実際の扱いは未検証です。OpenRouter のアカウント設定でプロンプトのログ記録はオフのままにしてください。";
/** A listing entry without a readable price, as the backend reports it (`price_known: false`, price null). */
const UNKNOWN_PRICE_MODEL: ModelDto = {
  id: "bytedance/seedance-2.0-pro",
  name: "Seedance 2.0 Pro",
  clip_durations: [5, 10],
  max_clip_duration: 10,
  resolution: "720x1280",
  supports_vertical: true,
  price_per_second_usd: null,
  price_note:
    "料金を確認できません（モデル一覧の料金項目（video_seconds_720p）に、この条件（720p・テキスト→動画）で使える秒単価がありません）。予算を確保できないため使用できません",
  verified: false,
  price_known: false,
  default_duration: 5,
  text_to_video: true,
  image_to_video: false,
  as_of: "2026-10-13",
  source_url: "https://openrouter.ai/bytedance/seedance-2.0-pro",
};
const FETCHED_MINUTES_AGO = 35;

let api: APIRequestContext;
let projectId = "";
let batchId = "";

/** OpenRouter configured with a freshly fetched listing (+ a model without a price); Claude via OpenRouter. */
function configureOpenRouter(capabilities: CapabilitiesDto, llmRoute: "openrouter" | "anthropic" = "openrouter"): void {
  const fetchedAt = new Date(Date.now() - FETCHED_MINUTES_AGO * 60_000).toISOString();
  for (const provider of capabilities.providers) {
    if (provider.id !== "openrouter") continue;
    Object.assign(provider, {
      configured: true,
      available: true,
      reason: null,
      models_source: "live",
      models_fetched_at: fetchedAt,
      models_note: `OpenRouter のモデル一覧（${fetchedAt.slice(0, 10)} 取得）より。9:16 に対応していないモデルなど 3 件は表示していません。`,
      models: [
        // Prices read from the listing, worded as the backend does for a fetched listing.
        ...provider.models
          .filter((model) => model.id !== UNKNOWN_PRICE_MODEL.id)
          .map((model) => ({
            ...model,
            price_note: `OpenRouter のモデル一覧の料金（video_seconds_720p: $${model.price_per_second_usd}/秒、${fetchedAt.slice(0, 10)} 取得）。未検証。実費は完了時の usage.cost で記録します`,
          })),
        UNKNOWN_PRICE_MODEL,
      ],
    });
  }
  capabilities.claude_configured = true;
  capabilities.llm =
    llmRoute === "openrouter"
      ? { route: "openrouter", model: CLAUDE_VIA_OPENROUTER, data_policy_note: OPENROUTER_DATA_POLICY, verified: false }
      : {
          route: "anthropic",
          model: "claude-opus-5-5",
          data_policy_note: "絵コンテの生成時に、楽曲名・ムード・目的・カスタム世界観・歌詞の本文・楽曲解析の要約を Anthropic（Claude）に直接送ります。",
          verified: false,
        };
}

async function openPage(page: Page, url: string): Promise<void> {
  await storeApiToken(page);
  await page.goto(url);
}

function settingsSection(page: Page, title: string): Locator {
  return page.locator("section").filter({ has: page.getByRole("heading", { name: title, exact: true }) });
}

/** Viewport screenshot (keeps an open popover such as a select list, which a full-page capture would close). */
async function viewportShot(page: Page, name: string): Promise<void> {
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, `${name}.png`), animations: "disabled" });
}

/** The value cell of a visible `<dt>label</dt><dd>value</dd>` row. */
function settingValue(page: Page, label: string): Locator {
  return page.locator(`xpath=//dt[normalize-space()="${label}"]/following-sibling::dd[1]`).filter({ visible: true });
}

test.beforeAll(async ({ playwright }) => {
  requireDemoAssets();
  api = await apiContext(playwright);
  const project = await apiJson<{ id: string }>(api, "POST", "/api/projects", {
    title: `OpenRouter E2E ${Date.now()}`,
    artist_name: ARTIST,
    world_preset: "urban_night",
  });
  projectId = project.id;
  const audio = await api.post(`${API_URL}/api/projects/${projectId}/audio`, {
    multipart: { file: { name: "demo_song.mp3", mimeType: "audio/mpeg", buffer: fs.readFileSync(demoFile("demo_song.mp3")) } },
  });
  expect(audio.status()).toBe(201);
  await waitFor(async () => (await apiJson<{ status: string }>(api, "GET", `/api/projects/${projectId}/analysis`)).status === "done", {
    message: "analysis",
    timeout: 180_000,
  });
  await apiJson(api, "PUT", `/api/projects/${projectId}/lyrics`, { text: fs.readFileSync(demoFile("demo_lyrics_ja.txt"), "utf8") });
  const upload = await api.post(`${API_URL}/api/projects/${projectId}/clips/upload`, {
    multipart: { files: { name: "clip_01.mp4", mimeType: "video/mp4", buffer: fs.readFileSync(demoFile("clip_01.mp4")) } },
  });
  expect(upload.ok(), await upload.text()).toBeTruthy();
  await apiJson(api, "PUT", `/api/projects/${projectId}/rights`, { items: Object.fromEntries(RIGHTS_KEYS.map((key) => [key, true])) });
  // One short library video: a real batch whose JSON is patched to look like an OpenRouter run.
  const batch = await apiJson<BatchDto>(api, "POST", `/api/projects/${projectId}/batches`, {
    provider: "library",
    count: 1,
    duration_sec: 5,
    lyric_style: "simple",
  });
  batchId = batch.id;
  await waitFor(
    async () => {
      const current = await apiJson<BatchDto>(api, "GET", `/api/batches/${batchId}`);
      return FINAL_BATCH.includes(current.status) ? current : null;
    },
    { message: "library batch", timeout: 5 * 60_000 },
  );
});

test.afterAll(async () => {
  await api?.dispose();
});

test("settings without keys: OpenRouter with its built-in candidates, Claude route = template", async ({ page }, testInfo) => {
  const pageLog = watchConsole(page, testInfo);
  const capabilities = await apiJson<CapabilitiesDto>(api, "GET", "/api/system/capabilities");
  const openrouter = capabilities.providers.find((provider) => provider.id === "openrouter");
  expect(openrouter, "the backend lists the openrouter provider").toBeTruthy();
  expect(openrouter!.configured).toBe(false);
  expect(openrouter!.models_source).toBe("fallback");
  expect(capabilities.llm?.route ?? null).toBeNull();

  await openPage(page, "/settings");
  const row = page.locator("li[data-provider=openrouter]");
  await expect(row).toContainText("OpenRouter");
  await expect(row).toContainText("未設定");
  await expect(row).toContainText("OPENROUTER_API_KEY が未設定です");
  await expect(row).toContainText(`モデル一覧: 組み込みの候補`);
  await expect(row).toContainText(`${openrouter!.models.length} モデル`);
  await expect(row).toContainText(openrouter!.models_note!);
  // The built-in candidates have no fetch age ("取得 35分前"); their note names the date the listing was copied.
  await expect(row).not.toContainText(/取得 \d/);

  const claude = page.getByTestId("claude-route");
  await expect(claude.getByText("テンプレート", { exact: true })).toBeVisible();
  await expect(claude).toContainText("ANTHROPIC_API_KEY・OPENROUTER_API_KEY がどちらも未設定");
  await expect(claude).toContainText(capabilities.llm!.data_policy_note);
  await expect(claude).toContainText("経路の優先順");

  const pricing = settingsSection(page, "料金表");
  for (const model of openrouter!.models) await expect(pricing.getByText(model.id, { exact: true })).toBeVisible();
  // Built-in Hailuo: 9:16 support unknown (OpenRouter is always asked for 9:16).
  if (openrouter!.models.some((model) => !model.supports_vertical)) await expect(pricing.getByText("9:16 未確認").first()).toBeVisible();
  await screenshot(page, "27-settings-openrouter-unconfigured");
  pageLog.assertClean();
});

test("settings with OpenRouter configured: listing status and age, Claude via OpenRouter with its data policy", async ({ page }, testInfo) => {
  const pageLog = watchConsole(page, testInfo);
  await patchApiJson<CapabilitiesDto>(page, "/api/system/capabilities", (capabilities) => configureOpenRouter(capabilities));
  await openPage(page, "/settings");

  const claude = page.getByTestId("claude-route");
  await expect(claude.getByText("OpenRouter 経由", { exact: true })).toBeVisible();
  await expect(claude.getByText("未検証", { exact: true })).toBeVisible();
  await expect(claude).toContainText(CLAUDE_VIA_OPENROUTER);
  await expect(claude).toContainText("OpenRouter 経由で Claude を使います");
  await expect(claude).toContainText("送信するデータ:");
  await expect(claude).toContainText("data_collection: deny");
  await expect(claude).toContainText("zdr: true");

  const row = page.locator("li[data-provider=openrouter]");
  await expect(row).toContainText("設定済み");
  await expect(row).toContainText("モデル一覧: OpenRouter から取得");
  await expect(row).toContainText(`取得 ${FETCHED_MINUTES_AGO}分前`);
  await expect(row).toContainText("料金未取得 1・選択不可");
  await expect(row).toContainText("9:16 に対応していないモデルなど 3 件は表示していません");

  const pricingRow = settingsSection(page, "料金表").getByRole("row").filter({ hasText: UNKNOWN_PRICE_MODEL.id });
  await expect(pricingRow).toContainText("料金未取得");
  await expect(pricingRow).not.toContainText("$0.00");
  await screenshot(page, "28-settings-openrouter-configured");

  // ANTHROPIC_API_KEY wins over OPENROUTER_API_KEY: the direct route.
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await patchApiJson<CapabilitiesDto>(page, "/api/system/capabilities", (capabilities) => configureOpenRouter(capabilities, "anthropic"));
  await page.reload();
  await expect(claude.getByText("Anthropic 直接", { exact: true })).toBeVisible();
  await expect(claude).toContainText("Anthropic の API に直接送ります");
  await expect(claude).toContainText("claude-opus-5-5");
  await page.unrouteAll({ behavior: "ignoreErrors" });
  pageLog.assertClean();
});

test("generate: OpenRouter card, models with price / 料金未取得, notes; estimate and batch requests use it", async ({ page }, testInfo) => {
  // The backend refuses the batch (no OPENROUTER_API_KEY): Chromium logs that 422 as a console error.
  const pageLog = watchConsole(page, testInfo, { ignore: [/status of 422/] });
  await patchApiJson<CapabilitiesDto>(page, "/api/system/capabilities", (capabilities) => configureOpenRouter(capabilities));
  await openPage(page, `/projects/${projectId}/generate`);

  const card = page.getByRole("radio", { name: /^OpenRouter/ });
  await expect(card).toContainText("外部 API");
  await expect(card).toContainText("未検証");
  await expect(card).toContainText("生成に使用できます");
  await card.click();
  await expect(card).toHaveAttribute("aria-checked", "true");

  const notes = page.getByRole("region", { name: "OpenRouter での生成について" });
  await expect(notes).toContainText("未検証");
  await expect(notes).toContainText("音声の生成はオフ（generate_audio: false）");
  await expect(notes).toContainText("OpenRouter を経由して、モデルの提供元（Google）に送られます");
  await expect(notes).toContainText("OpenRouter の規約に加えて、実際に使うモデル（提供元: Google）の規約が適用されます");
  await expect(notes).toContainText("OpenRouter のモデル一覧（");
  await expect(notes).toContainText("9:16 に対応していないモデルなど 3 件は表示していません");

  const modelSelect = page.getByRole("combobox", { name: "モデル" });
  await expect(modelSelect).toContainText("Veo 3.1 Lite（既定）");
  await page.getByRole("radiogroup", { name: "動画生成プロバイダー" }).scrollIntoViewIfNeeded();
  await page.evaluate(() => window.scrollBy(0, 260));
  await modelSelect.click();
  const unknown = page.getByRole("option", { name: /Seedance 2\.0 Pro/ });
  await expect(unknown).toContainText("料金未取得");
  await expect(unknown).toHaveAttribute("aria-disabled", "true");
  // The built-in list is a copy of the real 2026-10-13 listing: duration_seconds_without_audio_720p = $0.03/s.
  await expect(page.getByRole("option", { name: /Veo 3\.1 Lite/ })).toContainText("$0.03/秒");
  await expect(page.getByRole("option", { name: /Veo 3\.1 Lite/ })).toContainText("提供元 Google");
  await viewportShot(page, "29-generate-openrouter-models");

  const estimateRequest = page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      request.url().endsWith(`/api/projects/${projectId}/estimate`) &&
      request.postDataJSON()?.model === "kwaivgi/kling-v3.0-std",
  );
  await page.getByRole("option", { name: /Kling: Video v3\.0 Standard/ }).click();
  expect((await estimateRequest).postDataJSON()).toMatchObject({ provider: "openrouter", model: "kwaivgi/kling-v3.0-std" });
  await expect(notes).toContainText("モデルの提供元（Kuaishou（Kling））");
  await expect(page.getByText("kwaivgi/kling-v3.0-std", { exact: true })).toBeVisible();
  await expect(settingValue(page, "生成エンジン")).toHaveText("OpenRouter / Kling: Video v3.0 Standard");

  // The real backend prices the selection from OpenRouter's (built-in) model data.
  const estimatePanel = page.locator("[data-slot=card]").filter({ hasText: "費用の見積もり" });
  await expect(estimatePanel).toContainText("$0.084/秒");
  await expect(estimatePanel.getByText("未検証", { exact: true })).toBeVisible();
  await expect(estimatePanel).toContainText("OpenRouter が返す実費（usage.cost）で記録します");
  // The built-in list is a copy of the real listing: the pricing basis never mentions researched values (調査値).
  const pricingBasis = "時点の OpenRouter のモデル一覧（最新の一覧を取得できないときは組み込みの写し）の料金に基づく";
  await expect(estimatePanel).toContainText(pricingBasis);
  await expect(estimatePanel.getByTitle(pricingBasis, { exact: false })).toBeVisible();
  await expect(estimatePanel).not.toContainText("調査値");
  await expect(page.getByText("見積もりは予算内です")).toBeVisible({ timeout: 60_000 });
  await screenshot(page, "30-generate-openrouter");

  // 生成開始 → the confirmation explains OpenRouter pricing; the backend refuses: no OPENROUTER_API_KEY here.
  const batchRequest = page.waitForRequest((request) => request.method() === "POST" && request.url().endsWith(`/api/projects/${projectId}/batches`));
  await page.getByRole("button", { name: "生成開始" }).click();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toContainText("OpenRouter で新規素材");
  await expect(dialog).toContainText("OpenRouter が返す金額で記録します");
  await expect(dialog).toContainText(pricingBasis);
  await expect(dialog).not.toContainText("調査値");
  await expect(dialog).toContainText("プロンプトは OpenRouter を経由してモデルの提供元に送られます");
  await dialog.getByRole("button", { name: "生成を開始" }).click();
  expect((await batchRequest).postDataJSON()).toMatchObject({ provider: "openrouter", model: "kwaivgi/kling-v3.0-std" });
  const refused = page.getByRole("alert").filter({ hasText: "生成を開始できませんでした" });
  await expect(refused).toContainText("OPENROUTER_API_KEY");
  await page.unrouteAll({ behavior: "ignoreErrors" });
  pageLog.assertClean();
});

test("generate: the last batch's OpenRouter model is gone from the listing → the default model is shown and requested", async ({ page }, testInfo) => {
  const pageLog = watchConsole(page, testInfo);
  await patchApiJson<CapabilitiesDto>(page, "/api/system/capabilities", (capabilities) => configureOpenRouter(capabilities));
  // The form starts from the latest batch's settings: pretend it ran on a model OpenRouter no longer lists.
  await patchApiJson<BatchDto[]>(page, `/api/projects/${projectId}/batches`, (batches) => {
    for (const batch of batches) Object.assign(batch.settings, { provider: "openrouter", model: "vendor/retired-video-model" });
  });
  const estimateRequest = page.waitForRequest(
    (request) => request.method() === "POST" && request.url().endsWith(`/api/projects/${projectId}/estimate`),
  );
  await openPage(page, `/projects/${projectId}/generate`);
  await expect(page.getByRole("radio", { name: /^OpenRouter/ })).toHaveAttribute("aria-checked", "true");
  await expect(page.getByRole("combobox", { name: "モデル" })).toContainText("Veo 3.1 Lite（既定）");
  // null = the provider's default model (what the select shows), not the unlisted id (the server would refuse it).
  expect((await estimateRequest).postDataJSON()).toMatchObject({ provider: "openrouter", model: null });
  await expect(page.locator("[data-slot=card]").filter({ hasText: "費用の見積もり" })).toContainText("$0.03/秒");
  await page.unrouteAll({ behavior: "ignoreErrors" });
  pageLog.assertClean();
});

test("storyboard preview names the Claude route and model, and the template fallback after an OpenRouter call", async ({ page }, testInfo) => {
  const pageLog = watchConsole(page, testInfo);
  await patchApiJson<CapabilitiesDto>(page, "/api/system/capabilities", (capabilities) => configureOpenRouter(capabilities));
  // No key: the backend answers with the template; report what the OpenRouter route returns instead.
  let reply: (storyboard: StoryboardDto) => void = () => undefined;
  await page.route(`${API_URL}/api/projects/${projectId}/storyboard`, async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    const response = await route.fetch();
    const storyboard = (await response.json()) as StoryboardDto;
    expect(storyboard.source).toBe("template");
    reply(storyboard);
    const headers = Object.fromEntries(
      Object.entries(response.headers()).filter(([name]) => !["content-length", "content-encoding"].includes(name.toLowerCase())),
    );
    await route.fulfill({ status: response.status(), headers, json: storyboard });
  });
  await openPage(page, `/projects/${projectId}/generate`);
  const card = page.locator("[data-slot=card]").filter({ hasText: "絵コンテのプレビュー" });
  await expect(card).toContainText(`Claude（OpenRouter 経由）で作成します（モデル ${CLAUDE_VIA_OPENROUTER}）。`);
  await expect(card).toContainText("歌詞の本文は OpenRouter を経由して Claude の提供元に送られます。");

  reply = (storyboard) =>
    Object.assign(storyboard, {
      source: "claude",
      route: "openrouter",
      model: CLAUDE_VIA_OPENROUTER,
      usage: { input_tokens: 6120, output_tokens: 4210 },
      cost_usd: 0.0042,
      warnings: [],
    });
  await card.getByRole("button", { name: "プレビューを作成" }).click();
  const source = card.getByTestId("storyboard-source");
  await expect(source.getByText("Claude（OpenRouter 経由）", { exact: true })).toBeVisible();
  await expect(source).toContainText(CLAUDE_VIA_OPENROUTER);
  await expect(source).toContainText("LLM 費用 $0.0042");
  await screenshot(page, "31-storyboard-openrouter");

  // A call that produced no usable answer (e.g. no endpoint meets the data policy): template, route still named.
  const fallbackWarning = "OpenRouter にデータ保持なしの条件を満たす Claude の提供元が見つからなかったため、テンプレートで絵コンテを作成しました";
  reply = (storyboard) => Object.assign(storyboard, { route: "openrouter", model: null, warnings: [fallbackWarning, ...storyboard.warnings] });
  await card.getByRole("button", { name: "作り直す" }).click();
  await expect(source.getByText("テンプレート", { exact: true })).toBeVisible();
  await expect(source).toContainText("Claude（OpenRouter 経由）の応答を使えなかったため、テンプレートで作成しました");
  await expect(source).not.toContainText(CLAUDE_VIA_OPENROUTER);
  await expect(card.getByRole("alert").filter({ hasText: "注意" })).toContainText(fallbackWarning);
  await page.unrouteAll({ behavior: "ignoreErrors" });
  pageLog.assertClean();
});

test("usage, batch and clips label OpenRouter (video and Claude costs) and the model's company", async ({ page }, testInfo) => {
  const pageLog = watchConsole(page, testInfo);
  type UsageDto = {
    usage: { total_usd: number; month_usd: number; clips_generated: number; by_provider: { provider: string; amount_usd: number; clips: number }[] };
  };
  await patchApiJson<UsageDto>(page, "/api/dashboard", (dashboard) => {
    // OpenRouter = its video clips + the Claude storyboards sent through it; anthropic = Claude called directly.
    Object.assign(dashboard.usage, {
      total_usd: 1.285,
      month_usd: 1.285,
      clips_generated: 6,
      by_provider: [
        { provider: "openrouter", amount_usd: 1.234, clips: 6 },
        { provider: "anthropic", amount_usd: 0.051, clips: 0 },
      ],
    });
  });
  await openPage(page, "/");
  const usage = page.locator("[data-slot=card]").filter({ hasText: "API 利用状況" });
  const usageTable = usage.getByRole("table", { name: "プロバイダー別の利用額" });
  await expect(usageTable.getByRole("row").filter({ hasText: "OpenRouter" })).toContainText("$1.23");
  await expect(usageTable.getByRole("row").filter({ hasText: "Claude（Anthropic 直接）" })).toContainText("$0.05");
  await expect(usage).toContainText("OpenRouter 経由の Claude（絵コンテ）の費用も含みます");
  await screenshot(page, "32-dashboard-usage-openrouter");
  await page.unrouteAll({ behavior: "ignoreErrors" });

  // The batch: generated with OpenRouter, storyboard by Claude via OpenRouter (patched on this batch only).
  await patchApiJson<BatchDto>(page, `/api/batches/${batchId}`, (batch) => {
    batch.settings.provider = "openrouter";
    batch.settings.model = "google/veo-3.1-lite";
    if (batch.estimate) Object.assign(batch.estimate, { provider: "openrouter", model: "google/veo-3.1-lite" });
    // A library batch has no storyboard: give it one as the OpenRouter route returns it.
    batch.storyboard = {
      ...(batch.storyboard ?? {
        world_bible: { summary_ja: "雨上がりの夜の街を、架空の若者がひとりで歩く。", look_en: "", protagonist_en: "", location_en: "", palette: [], consistency_tokens_en: "" },
        scenes: [{ id: "s01", description_ja: "ネオンが映る濡れた路面" }],
        warnings: [],
      }),
      source: "claude",
      route: "openrouter",
      model: CLAUDE_VIA_OPENROUTER,
      usage: { input_tokens: 6120, output_tokens: 4210 },
      cost_usd: 0.0042,
    } as StoryboardDto;
  });
  await openPage(page, `/batches/${batchId}`);
  await expect(settingValue(page, "生成エンジン")).toHaveText("OpenRouter / Google: Veo 3.1 Lite");
  const storyboardCard = page.locator("[data-slot=card]").filter({ has: page.getByText("絵コンテ", { exact: true }) });
  await expect(storyboardCard).toContainText("Claude（OpenRouter 経由）で作成 ・ 1 シーン ・ $0.0042");
  await expect(storyboardCard.getByTitle("絵コンテを作成したモデル")).toHaveText(CLAUDE_VIA_OPENROUTER);
  await screenshot(page, "34-batch-openrouter");
  await page.unrouteAll({ behavior: "ignoreErrors" });

  // Materials: a clip generated through OpenRouter.
  await patchApiJson<ClipDto[]>(page, `/api/projects/${projectId}/clips`, (clips) => {
    Object.assign(clips[0], { source: "openrouter", provider: "openrouter", model: "google/veo-3.1-lite", cost_usd: 0.3 });
  });
  await openPage(page, `/projects/${projectId}?tab=materials`);
  const tile = page.getByRole("button", { name: /^OpenRouterの素材/ }).first();
  await expect(tile).toBeVisible();
  await tile.click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("OpenRouter（google/veo-3.1-lite）");
  await expect(dialog).toContainText("OpenRouter 経由・モデルの提供元 Google");
  await expect(settingValue(page, "費用")).toHaveText("$0.30");
  await viewportShot(page, "33-clip-openrouter");
  await page.keyboard.press("Escape");
  await page.unrouteAll({ behavior: "ignoreErrors" });
  pageLog.assertClean();
});
