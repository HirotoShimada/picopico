import fs from "node:fs";

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
  storeApiToken,
  waitFor,
  watchConsole,
} from "./support";

/**
 * No repeated footage inside one video (§6.4 / §6.5), shown before anything is spent: the estimate's
 * shots per video, longest / slowest shot and in-video repeats, the readiness warning, the budget-fit
 * suggestion and the 生成開始 confirmation. Estimates come from the real backend; no batch is started.
 * The OpenRouter test patches only `GET /api/system/capabilities` (no OPENROUTER_API_KEY here, as in
 * `openrouter.spec.ts`); its estimates are priced by the backend from OpenRouter's built-in model data.
 */

interface BudgetFitDto {
  max_new_clips: number;
  new_clips: number;
  total_usd: number;
  shots_per_video: number;
  beat_shots_per_video: number;
  max_shot_sec: number;
  min_speed: number;
  repeat_shots: number;
  note: string;
}
interface EstimateDto {
  provider: string;
  videos: number;
  new_clips: number;
  video_generation_usd: number;
  total_usd: number;
  within_budget: boolean;
  budget_limit_usd: number | null;
  variety_score: number | null;
  notes: string[];
  shots_per_video_min?: number;
  shots_per_video_max?: number;
  beat_shots_per_video_max?: number;
  fitted_videos?: number;
  max_shot_sec?: number;
  min_speed?: number;
  repeat_shots?: number;
  repeat_warning?: string | null;
  budget_fit?: BudgetFitDto | null;
}
interface CapabilitiesDto {
  providers: { id: string; configured: boolean; available: boolean; reason: string | null }[];
}

const ARTIST = "デモアーティスト";
const VARIETY_LABEL = "動画間の映像バリエーション";

let api: APIRequestContext;
let projectId = "";

async function openPage(page: Page, url: string): Promise<void> {
  await storeApiToken(page);
  await page.goto(url);
}

/**
 * The backend's answer to the (debounced) estimate request whose body has every `match` value.
 * Call before the action that changes the settings.
 */
function nextEstimate(page: Page, match: Record<string, unknown>): Promise<EstimateDto> {
  return page
    .waitForResponse(
      (response) => {
        const request = response.request();
        if (request.method() !== "POST" || !request.url().endsWith(`/api/projects/${projectId}/estimate`)) return false;
        const body = request.postDataJSON() as Record<string, unknown>;
        return Object.entries(match).every(([key, value]) => body[key] === value);
      },
      { timeout: 60_000 },
    )
    .then(async (response) => {
      expect(response.ok(), `estimate → ${response.status()}`).toBeTruthy();
      return (await response.json()) as EstimateDto;
    });
}

/** Batch creation requests (none may be sent: these tests never start a batch). */
function watchBatchRequests(page: Page): string[] {
  const requests: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().endsWith(`/api/projects/${projectId}/batches`)) requests.push(request.url());
  });
  return requests;
}

function estimatePanel(page: Page): Locator {
  return page.locator("[data-slot=card]").filter({ hasText: "費用の見積もり" });
}

/** The value of an estimate row; the label cell may carry a hint after the label (`<dt>label<span>hint</span></dt>`). */
function estimateRow(panel: Locator, label: string): Locator {
  return panel.locator(`xpath=.//dt[starts-with(normalize-space(), "${label}")]/following-sibling::dd[1]`);
}

/** 「1本あたりのショット」 as the panel words it. */
function shotsText(estimate: EstimateDto): string {
  const max = estimate.shots_per_video_max!;
  const min = estimate.shots_per_video_min ?? max;
  const beat = estimate.beat_shots_per_video_max ?? max;
  const recut = (estimate.fitted_videos ?? 0) > 0 && beat > max;
  return `${min === max ? max : `${min}〜${max}`}ショット${recut ? `（通常 ${beat}）` : ""}`;
}

/** 「ショットの長さ・速度」 as the panel words it. */
function paceText(estimate: EstimateDto): string {
  const slowest = estimate.min_speed!;
  return `最長 ${estimate.max_shot_sec!.toFixed(1)}秒 ・ ${slowest >= 0.995 ? "等速" : `最も遅い ${slowest.toFixed(2)}倍速`}`;
}

async function expectShotRows(panel: Locator, estimate: EstimateDto): Promise<void> {
  expect(estimate.shots_per_video_max, "estimate.shots_per_video_max").toEqual(expect.any(Number));
  expect(estimate.max_shot_sec, "estimate.max_shot_sec").toEqual(expect.any(Number));
  expect(estimate.min_speed, "estimate.min_speed").toEqual(expect.any(Number));
  expect(estimate.repeat_shots, "estimate.repeat_shots").toEqual(expect.any(Number));
  await expect(estimateRow(panel, "1本あたりのショット")).toHaveText(shotsText(estimate));
  await expect(estimateRow(panel, "ショットの長さ・速度")).toHaveText(paceText(estimate));
  await expect(estimateRow(panel, "動画内の同じ素材")).toHaveText(estimate.repeat_shots! > 0 ? `${estimate.repeat_shots}ショット` : "なし");
}

test.beforeAll(async ({ playwright }) => {
  requireDemoAssets();
  api = await apiContext(playwright);
  // A new project: no generated clips yet, so a cap of N new clips means a pool of N clips.
  const project = await apiJson<{ id: string }>(api, "POST", "/api/projects", {
    title: `同じ素材なし E2E ${Date.now()}`,
    artist_name: ARTIST,
    world_preset: "nostalgic",
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
  await apiJson(api, "POST", `/api/projects/${projectId}/lyrics/auto-time`, { method: "heuristic" });
  await apiJson(api, "PUT", `/api/projects/${projectId}/rights`, { items: Object.fromEntries(RIGHTS_KEYS.map((key) => [key, true])) });
});

test.afterAll(async () => {
  await api?.dispose();
});

test("mock: uncapped videos never repeat a clip; a cap of 1 shows the repeats and the warning before generating", async ({ page }, testInfo) => {
  const pageLog = watchConsole(page, testInfo);
  const batchRequests = watchBatchRequests(page);
  const panel = estimatePanel(page);
  const checks = page.getByRole("list", { name: "生成前チェック" });
  const variety = panel.getByRole("progressbar", { name: VARIETY_LABEL });

  await test.step("3 videos, uncapped: no repeats; the variety bar is cross-video", async () => {
    const estimated = nextEstimate(page, { provider: "mock", duration_sec: 15, count: 3, max_new_clips: null });
    await openPage(page, `/projects/${projectId}/generate`);
    await page.getByRole("radio", { name: /モック/ }).click();
    await page.getByRole("radiogroup", { name: "1 本の長さ" }).getByRole("radio", { name: "15秒" }).click();
    await page.getByRole("radiogroup", { name: "本数" }).getByRole("radio", { name: "3本" }).click();
    await expect(page.getByRole("switch", { name: "新規生成の上限を設定" })).toHaveAttribute("aria-checked", "false");
    const estimate = await estimated;
    expect(estimate.repeat_shots).toBe(0);
    await expectShotRows(panel, estimate);
    await expect(panel.getByText(VARIETY_LABEL, { exact: true })).toBeVisible();
    await expect(variety).toBeVisible();
  });

  await test.step("1 video, uncapped: 「動画内の同じ素材」 なし, no variety bar", async () => {
    const estimated = nextEstimate(page, { provider: "mock", duration_sec: 15, count: 1, max_new_clips: null });
    await page.getByRole("radiogroup", { name: "本数" }).getByRole("radio", { name: "1本" }).click();
    const estimate = await estimated;
    expect(estimate.videos).toBe(1);
    expect(estimate.repeat_shots).toBe(0);
    expect(estimate.repeat_warning ?? null).toBeNull();
    await expect(estimateRow(panel, "動画内の同じ素材")).toHaveText("なし");
    await expectShotRows(panel, estimate);
    // Variety compares videos with each other: one video has no bar.
    await expect(variety).toHaveCount(0);
    await expect(panel.getByText(VARIETY_LABEL, { exact: true })).toHaveCount(0);
    await expect(panel).not.toContainText("再登場");
    await expect(page.getByText("見積もりは予算内です")).toBeVisible({ timeout: 60_000 });
    await expect(checks).not.toContainText("再登場");
    await expect(panel).not.toHaveAttribute("aria-busy", "true");
    await screenshot(page, "35-generate-no-repeat");
  });

  await test.step("cap 1 new clip: the repeats are counted, warned about once, and do not block", async () => {
    const limit = page.getByRole("switch", { name: "新規生成の上限を設定" });
    await limit.click();
    await expect(limit).toHaveAttribute("aria-checked", "true");
    const estimated = nextEstimate(page, { provider: "mock", duration_sec: 15, count: 1, max_new_clips: 1 });
    await page.getByRole("spinbutton", { name: "新規生成の上限", exact: true }).fill("1");
    const estimate = await estimated;
    // One clip cannot fill 15 s at ≤ 8 s per shot: the last resort repeats it (counted, never silently).
    expect(estimate.new_clips).toBe(1);
    expect(estimate.repeat_shots).toBeGreaterThan(0);
    expect(estimate.repeat_warning).toContain("再登場");
    await expect(estimateRow(panel, "動画内の同じ素材")).toHaveText(/^\d+ショット$/);
    await expectShotRows(panel, estimate);
    await expect(panel.locator("dt", { hasText: "動画内の同じ素材" })).toContainText("連続しない・別の区間・寄りの構図");
    // Shown once, above the variety block (not repeated in the notes list).
    await expect(panel.getByText(estimate.repeat_warning!, { exact: true })).toBeVisible();

    await expect(checks).toContainText(`動画内で同じ素材が ${estimate.repeat_shots} ショット再登場します`);
    await expect(checks).toContainText("新規生成の上限を上げるか、動画を短くすると解消します。");
    await expect(page.getByText("見積もりは予算内です")).toBeVisible({ timeout: 60_000 });
    await expect(page.getByRole("button", { name: "生成開始" })).toBeEnabled();
    await expect(panel).not.toHaveAttribute("aria-busy", "true");
    await screenshot(page, "36-generate-repeat-warning");
  });

  // The mock estimate is $0: 生成開始 would start the batch without a confirmation (see the OpenRouter test).
  expect(batchRequests).toEqual([]);
  pageLog.assertClean();
});

test("OpenRouter: budget fit applies the cap that fits; the confirmation names the shots and the repeats; nothing starts", async ({ page }, testInfo) => {
  const pageLog = watchConsole(page, testInfo);
  const batchRequests = watchBatchRequests(page);
  // No OPENROUTER_API_KEY here: report the provider as configured so its card can be selected (estimates are real).
  await patchApiJson<CapabilitiesDto>(page, "/api/system/capabilities", (capabilities) => {
    for (const provider of capabilities.providers) {
      if (provider.id === "openrouter") Object.assign(provider, { configured: true, available: true, reason: null });
    }
  });
  const panel = estimatePanel(page);
  const checks = page.getByRole("list", { name: "生成前チェック" });
  const limitSwitch = page.getByRole("switch", { name: "新規生成の上限を設定" });
  const limitInput = page.getByRole("spinbutton", { name: "新規生成の上限", exact: true });
  const dialog = page.getByRole("alertdialog");

  const uncapped = nextEstimate(page, { provider: "openrouter", duration_sec: 15, count: 1, max_new_clips: null, budget_limit_usd: null });
  await openPage(page, `/projects/${projectId}/generate`);
  const card = page.getByRole("radio", { name: /^OpenRouter/ });
  await card.click();
  await expect(card).toHaveAttribute("aria-checked", "true");
  await page.getByRole("radiogroup", { name: "1 本の長さ" }).getByRole("radio", { name: "15秒" }).click();
  await page.getByRole("radiogroup", { name: "本数" }).getByRole("radio", { name: "1本" }).click();
  await expect(limitSwitch).toHaveAttribute("aria-checked", "false");
  const free = await uncapped;
  expect(free.repeat_shots).toBe(0);
  expect(free.new_clips).toBeGreaterThan(1);

  let budget = 0;
  let fit: BudgetFitDto;
  await test.step("a budget just under the uncapped plan: budget_fit proposes a lower cap", async () => {
    budget = Math.round((free.total_usd - 0.01) * 100) / 100;
    const over = nextEstimate(page, { provider: "openrouter", max_new_clips: null, budget_limit_usd: budget });
    await page.locator("#generate-budget").fill(String(budget));
    const estimate = await over;
    expect(estimate.within_budget).toBe(false);
    expect(estimate.budget_fit, "estimate.budget_fit").toBeTruthy();
    fit = estimate.budget_fit!;
    expect(fit.max_new_clips).toBeGreaterThan(1);
    expect(fit.max_new_clips).toBeLessThan(estimate.new_clips);
    expect(fit.total_usd).toBeLessThanOrEqual(budget);
    // The note is shown once, next to its button (not repeated in the notes list).
    await expect(panel.getByText(fit.note, { exact: true })).toBeVisible();
    await expect(panel.getByRole("button", { name: `上限を${fit.max_new_clips}本にする` })).toBeVisible();
    await expect(checks).toContainText("見積もりが予算上限を超えています");
    await expect(checks).toContainText("上限を下げると、同じ素材を繰り返さないようショットが長めになります");
    await expect(page.getByRole("button", { name: "生成開始" })).toBeDisabled();
    await expect(panel).not.toHaveAttribute("aria-busy", "true");
    await screenshot(page, "37-generate-budget-fit");
  });

  await test.step("applying it sets the cap; the same seed reproduces exactly the plan it priced", async () => {
    const applied = nextEstimate(page, { provider: "openrouter", max_new_clips: fit.max_new_clips, budget_limit_usd: budget });
    await panel.getByRole("button", { name: `上限を${fit.max_new_clips}本にする` }).click();
    await expect(limitSwitch).toHaveAttribute("aria-checked", "true");
    await expect(limitInput).toHaveValue(String(fit.max_new_clips));
    const estimate = await applied;
    expect(estimate.within_budget).toBe(true);
    expect(estimate.total_usd).toBe(fit.total_usd);
    expect(estimate.new_clips).toBe(fit.new_clips);
    expect(estimate.shots_per_video_max).toBe(fit.shots_per_video);
    expect(estimate.repeat_shots).toBe(fit.repeat_shots);
    await expect(page.getByText("見積もりは予算内です")).toBeVisible({ timeout: 60_000 });
    await expect(panel.getByRole("button", { name: /^上限を\d+本にする$/ })).toHaveCount(0);
    await expectShotRows(panel, estimate);

    await page.getByRole("button", { name: "生成開始" }).click();
    await expect(dialog).toContainText(`OpenRouter で新規素材 ${estimate.new_clips} 本`);
    await expect(dialog).toContainText(`1本あたり${estimate.shots_per_video_max}ショット（最長${estimate.max_shot_sec!.toFixed(1)}秒・`);
    if (estimate.repeat_shots === 0) {
      await expect(dialog).toContainText("動画内で同じ素材は繰り返しません。");
    } else {
      await expect(dialog.getByRole("alert").filter({ hasText: "再登場" })).toContainText(
        `素材が足りないため、動画内で同じ素材が${estimate.repeat_shots}ショット再登場します。`,
      );
    }
    await dialog.getByRole("button", { name: "キャンセル" }).click();
    await expect(dialog).toHaveCount(0);
  });

  await test.step("cap 1: the confirmation warns about the repeats instead of promising none", async () => {
    const capped = nextEstimate(page, { provider: "openrouter", max_new_clips: 1, budget_limit_usd: budget });
    await limitInput.fill("1");
    const estimate = await capped;
    expect(estimate.within_budget).toBe(true);
    expect(estimate.repeat_shots).toBeGreaterThan(0);
    await expect(estimateRow(panel, "動画内の同じ素材")).toHaveText(`${estimate.repeat_shots}ショット`);
    await expect(checks).toContainText(`動画内で同じ素材が ${estimate.repeat_shots} ショット再登場します`);
    await expect(page.getByText("見積もりは予算内です")).toBeVisible({ timeout: 60_000 });

    await page.getByRole("button", { name: "生成開始" }).click();
    await expect(dialog).toContainText("OpenRouter で新規素材 1 本");
    await expect(dialog.getByRole("alert").filter({ hasText: "再登場" })).toHaveText(
      `素材が足りないため、動画内で同じ素材が${estimate.repeat_shots}ショット再登場します。`,
    );
    await expect(dialog).not.toContainText("動画内で同じ素材は繰り返しません");
    await dialog.getByRole("button", { name: "キャンセル" }).click();
    await expect(dialog).toHaveCount(0);
  });

  expect(batchRequests).toEqual([]);
  await page.unrouteAll({ behavior: "ignoreErrors" });
  pageLog.assertClean();
});
