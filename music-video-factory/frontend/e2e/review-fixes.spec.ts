import fs from "node:fs";

import { expect, test, type APIRequestContext, type Page, type Request } from "@playwright/test";

import {
  API_TOKEN,
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
  zipEntries,
} from "./support";

/**
 * Review follow-ups in the UI, against the real backend (one shared project, run in order):
 * signed ZIP links, hiding cancelled videos/clips, the 左右反転 option, the rights reset after a
 * song or lyric text change, the lyric import notes and the storyboard preview's rights/429 handling.
 * Also runs against a backend in `AUTH_MODE=token` (`E2E_API_TOKEN=<token>`).
 */

interface BatchDto {
  id: string;
  status: string;
  settings: { allow_mirror?: boolean; provider: string };
  counts: { done: number; cancelled: number; total: number };
}
interface ClipDto {
  id: string;
  source: string;
  status: string;
  used_in_videos: number;
}
interface VideoDto {
  id: string;
  status: string;
}
interface ProjectDto {
  id: string;
  rights: Record<string, boolean>;
  rights_confirmed_at: string | null;
  analysis_status: string;
}

const ARTIST = "デモアーティスト";
const RIGHTS_RESET_TITLE = "権利確認をやり直してください";
const FINAL_BATCH = ["completed", "partial", "failed", "cancelled", "budget_exceeded"];

// One project per worker (beforeAll), tests in file order. Not `serial`: each test sets the state it needs
// (rights, lyrics), so one failure does not skip the others (a new worker sets up a new project).

let api: APIRequestContext;
let projectId = "";
let title = "";
/** Library batch with 2 finished videos. */
let doneBatchId = "";
/** Mock batch cancelled while generating clips (cancelled videos and clips). */
let cancelledBatchId = "";

async function waitForBatch(batchId: string, statuses: string[], interval = 1_000): Promise<BatchDto> {
  return waitFor(
    async () => {
      const batch = await apiJson<BatchDto>(api, "GET", `/api/batches/${batchId}`);
      return statuses.includes(batch.status) ? batch : null;
    },
    { timeout: 8 * 60_000, interval, message: `batch ${batchId} → ${statuses.join("|")}` },
  );
}

async function waitForAnalysis(): Promise<void> {
  await waitFor(async () => (await apiJson<{ status: string }>(api, "GET", `/api/projects/${projectId}/analysis`)).status === "done", {
    message: "analysis",
    timeout: 180_000,
  });
}

async function setRights(items: Partial<Record<(typeof RIGHTS_KEYS)[number], boolean>>): Promise<ProjectDto> {
  return apiJson<ProjectDto>(api, "PUT", `/api/projects/${projectId}/rights`, { items });
}

const allRights = () => Object.fromEntries(RIGHTS_KEYS.map((key) => [key, true]));

/** Screenshots of the changed screens (dev mode only; a token-mode run checks the same screens). */
async function shot(page: Page, name: string): Promise<void> {
  if (!API_TOKEN) await screenshot(page, name);
}

async function openPage(page: Page, url: string): Promise<void> {
  await storeApiToken(page);
  await page.goto(url);
}

test.beforeAll(async ({ playwright }) => {
  requireDemoAssets();
  api = await apiContext(playwright);
  title = `レビュー修正 E2E ${Date.now()}`;
  const project = await apiJson<{ id: string }>(api, "POST", "/api/projects", { title, artist_name: ARTIST, world_preset: "nostalgic" });
  projectId = project.id;
  const audio = await api.post(`${API_URL}/api/projects/${projectId}/audio`, {
    multipart: { file: { name: "demo_song.mp3", mimeType: "audio/mpeg", buffer: fs.readFileSync(demoFile("demo_song.mp3")) } },
  });
  expect(audio.status()).toBe(201);
  await waitForAnalysis();
  await apiJson(api, "PUT", `/api/projects/${projectId}/lyrics`, { text: fs.readFileSync(demoFile("demo_lyrics_ja.txt"), "utf8") });
  const upload = await api.post(`${API_URL}/api/projects/${projectId}/clips/upload`, {
    multipart: { files: { name: "clip_01.mp4", mimeType: "video/mp4", buffer: fs.readFileSync(demoFile("clip_01.mp4")) } },
  });
  expect(upload.ok(), await upload.text()).toBeTruthy();
  await setRights(allRights());

  // Two short library videos that finish…
  const done = await apiJson<BatchDto>(api, "POST", `/api/projects/${projectId}/batches`, {
    provider: "library",
    count: 2,
    duration_sec: 5,
    lyric_style: "simple",
  });
  doneBatchId = done.id;
  expect((await waitForBatch(doneBatchId, FINAL_BATCH)).status).toBe("completed");

  // …and a mock batch cancelled while its clips are generated: its videos and pending clips end up cancelled.
  const cancelled = await apiJson<BatchDto>(api, "POST", `/api/projects/${projectId}/batches`, {
    provider: "mock",
    count: 3,
    duration_sec: 15,
    reuse: "max_variety",
  });
  cancelledBatchId = cancelled.id;
  await waitForBatch(cancelledBatchId, ["generating_clips", "rendering", ...FINAL_BATCH], 100);
  await apiJson(api, "POST", `/api/batches/${cancelledBatchId}/cancel`);
  await waitForBatch(cancelledBatchId, FINAL_BATCH);
  await waitFor(
    async () => {
      const clips = await apiJson<ClipDto[]>(api, "GET", `/api/projects/${projectId}/clips`);
      return clips.every((clip) => clip.status !== "pending" && clip.status !== "generating");
    },
    { message: "clips settled", timeout: 120_000 },
  );
});

test.afterAll(async () => {
  await api?.dispose();
});

test("ZIP downloads stream from a signed link (selection and job), without the old endpoints", async ({ page }, testInfo) => {
  const pageLog = watchConsole(page, testInfo);
  const apiRequests: string[] = [];
  page.on("request", (request: Request) => {
    if (request.url().startsWith(API_URL)) apiRequests.push(`${request.method()} ${new URL(request.url()).pathname}`);
  });

  await test.step("gallery: selected videos", async () => {
    await openPage(page, `/projects/${projectId}/videos?batch=${doneBatchId}`);
    await expect(page.getByRole("button", { name: /をプレビュー$/ })).toHaveCount(2);
    await page.getByRole("button", { name: "完成分をすべて選択" }).click();
    const [download] = await Promise.all([
      page.waitForEvent("download", { timeout: 60_000 }),
      page.getByRole("button", { name: "選択した 2 本を ZIP で保存" }).click(),
    ]);
    // The browser downloads the signed /media-zip link itself (streamed, no Blob in page memory, no auth header).
    expect(download.url()).toMatch(new RegExp(`^${API_URL}/media-zip/videos\\.zip\\?`));
    expect(download.suggestedFilename()).toBe("videos.zip");
    const target = testInfo.outputPath("selection.zip");
    await download.saveAs(target);
    const entries = zipEntries(target);
    expect(entries).toHaveLength(2);
    expect(entries.every((name) => name.endsWith(".mp4"))).toBeTruthy();
  });

  await test.step("job page: all finished videos of the job", async () => {
    await page.goto(`/batches/${doneBatchId}`);
    const [download] = await Promise.all([
      page.waitForEvent("download", { timeout: 60_000 }),
      page.getByRole("button", { name: "完成分を ZIP で保存" }).click(),
    ]);
    expect(download.url()).toMatch(new RegExp(`^${API_URL}/media-zip/`));
    expect(download.suggestedFilename()).toMatch(new RegExp(`^${ARTIST}_.*_videos\\.zip$`));
    const target = testInfo.outputPath("job.zip");
    await download.saveAs(target);
    expect(zipEntries(target)).toHaveLength(2);
  });

  expect(apiRequests).toContain("POST /api/videos/zip-link");
  expect(apiRequests).toContain(`POST /api/batches/${doneBatchId}/zip-link`);
  expect(apiRequests.filter((line) => /\/zip$/.test(line)), "old in-memory ZIP endpoints").toEqual([]);
  pageLog.assertClean();
});

test("cancelled videos and clips are hidden by default, with a toggle; usage counts skip cancelled videos", async ({ page }, testInfo) => {
  const pageLog = watchConsole(page, testInfo);
  const videos = await apiJson<VideoDto[]>(api, "GET", `/api/projects/${projectId}/videos`);
  const cancelledVideos = videos.filter((video) => video.status === "cancelled").length;
  expect(cancelledVideos).toBeGreaterThan(0);
  const clips = await apiJson<ClipDto[]>(api, "GET", `/api/projects/${projectId}/clips`);
  const cancelledClips = clips.filter((clip) => clip.status === "cancelled").length;
  expect(cancelledClips, "the cancelled mock batch left cancelled clips").toBeGreaterThan(0);

  await test.step("gallery", async () => {
    await openPage(page, `/projects/${projectId}/videos`);
    const toggle = page.getByRole("switch", { name: "キャンセル済みを表示" });
    await expect(toggle).toHaveAttribute("aria-checked", "false");
    await expect(page.getByRole("button", { name: /をプレビュー$/ })).toHaveCount(videos.length - cancelledVideos);
    await expect(page.getByText(`キャンセル済み ${cancelledVideos} 本を非表示`)).toBeVisible();
    await shot(page, "18-gallery-cancelled-hidden");
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-checked", "true");
    await expect(page).toHaveURL(/[?&]cancelled=1/);
    await expect(page.getByRole("button", { name: /をプレビュー$/ })).toHaveCount(videos.length);
  });

  await test.step("materials", async () => {
    await page.goto(`/projects/${projectId}?tab=materials`);
    const panel = page.getByRole("tabpanel", { name: /素材/ });
    const toggle = panel.getByRole("switch", { name: "キャンセル済みを表示" });
    await expect(toggle).toHaveAttribute("aria-checked", "false");
    await expect(panel.getByRole("button", { name: /をプレビュー$/ })).toHaveCount(clips.length - cancelledClips);
    await expect(panel.getByText("キャンセル済み", { exact: true })).toHaveCount(0);
    await expect(panel.getByText(`キャンセル済み ${cancelledClips} 本を非表示`)).toBeVisible();
    // The uploaded clip is used by the 2 finished videos; the cancelled mock videos do not count.
    const upload = clips.find((clip) => clip.source === "upload")!;
    expect(upload.used_in_videos).toBe(2);
    await expect(panel.getByText("2本で使用")).toHaveCount(1);
    expect(clips.filter((clip) => clip.source === "mock").every((clip) => clip.used_in_videos === 0)).toBeTruthy();
    await shot(page, "19-materials-cancelled-hidden");
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-checked", "true");
    await expect(panel.getByRole("button", { name: /をプレビュー$/ })).toHaveCount(clips.length);
    await expect(panel.getByText("キャンセル済み", { exact: true }).first()).toBeVisible();
  });
  pageLog.assertClean();
});

test("generate: 左右反転を使う is off by default, is sent with estimate and batch, and is carried over", async ({ page }, testInfo) => {
  const pageLog = watchConsole(page, testInfo);
  await openPage(page, `/projects/${projectId}/generate`);
  const mirror = page.getByRole("switch", { name: "左右反転を使う" });
  await expect(mirror).toHaveAttribute("aria-checked", "false");
  await expect(page.getByText(/文字や看板.*鏡文字/).first()).toBeVisible();

  await page.getByRole("radio", { name: /アップロード素材/ }).click();
  await page.getByRole("radiogroup", { name: "1 本の長さ" }).getByRole("radio", { name: "15秒" }).click();
  await page.getByRole("radiogroup", { name: "本数" }).getByRole("radio", { name: "1本" }).click();
  const estimateWithMirror = page.waitForRequest(
    (request) => request.method() === "POST" && request.url().endsWith(`/api/projects/${projectId}/estimate`) && request.postDataJSON()?.allow_mirror === true,
  );
  await mirror.click();
  await expect(mirror).toHaveAttribute("aria-checked", "true");
  await estimateWithMirror;
  await expect(page.locator("dt", { hasText: "左右反転" }).locator("xpath=following-sibling::dd[1]")).toHaveText("使う");
  await expect(page.getByText("見積もりは予算内です")).toBeVisible({ timeout: 60_000 });
  await shot(page, "20-generate-mirror");

  const created = page.waitForRequest((request) => request.method() === "POST" && request.url().endsWith(`/api/projects/${projectId}/batches`));
  await page.getByRole("button", { name: "生成開始" }).click();
  expect((await created).postDataJSON().allow_mirror).toBe(true);
  await page.waitForURL(/\/batches\/[0-9a-f]{32}$/, { timeout: 60_000 });
  const batchId = /\/batches\/([0-9a-f]{32})/.exec(page.url())![1];
  const batch = await apiJson<BatchDto>(api, "GET", `/api/batches/${batchId}`);
  expect(batch.settings.allow_mirror).toBe(true);
  await expect(page.locator("dt", { hasText: "左右反転" }).locator("xpath=following-sibling::dd[1]").filter({ visible: true })).toHaveText("使う");
  await waitForBatch(batchId, FINAL_BATCH);

  // The next generate form starts from the latest batch's settings, including the mirror option.
  await page.goto(`/projects/${projectId}/generate`);
  await expect(page.getByRole("switch", { name: "左右反転を使う" })).toHaveAttribute("aria-checked", "true");
  pageLog.assertClean();
});

test("rights reset: replacing the song or changing lyric text asks to confirm the rights again", async ({ page }, testInfo) => {
  const pageLog = watchConsole(page, testInfo);
  expect((await setRights(allRights())).rights_confirmed_at).not.toBeNull();
  const rightsTab = page.getByRole("tab", { name: /権利確認/ });

  await test.step("song replaced → 音源の権利", async () => {
    await openPage(page, `/projects/${projectId}`);
    const panel = page.getByRole("tabpanel", { name: /楽曲解析/ });
    await expect(rightsTab).not.toContainText("要再確認");
    await panel.getByRole("button", { name: "差し替え" }).click();
    await panel.locator('input[type="file"]').setInputFiles(demoFile("demo_song.wav"));
    const notice = panel.getByRole("alert").filter({ hasText: RIGHTS_RESET_TITLE });
    await expect(notice).toBeVisible({ timeout: 120_000 });
    await expect(notice).toContainText("音源の権利");
    await expect(notice.getByRole("link", { name: "権利確認へ" })).toHaveAttribute("href", `/projects/${projectId}?tab=rights`);
    await expect(rightsTab).toContainText("要再確認");
    await expect(page.getByText("権利の再確認が必要").first()).toBeVisible();
    const project = await apiJson<ProjectDto>(api, "GET", `/api/projects/${projectId}`);
    expect(project.rights.audio_rights).toBe(false);
    expect(project.rights.lyrics_rights).toBe(true);
    await shot(page, "21-rights-reset-song");
    await waitForAnalysis();
  });

  await test.step("lyric text changed → 歌詞の権利 (and the import notes stay separate)", async () => {
    await page.getByRole("tab", { name: /歌詞/ }).click();
    const panel = page.getByRole("tabpanel", { name: /歌詞/ });
    await panel.getByRole("button", { name: "編集", exact: true }).click();
    const text = fs.readFileSync(demoFile("demo_lyrics_ja.txt"), "utf8").trimEnd();
    await panel.getByLabel("歌詞テキスト").fill(`${text}\n朝焼けの向こうへ`);
    await panel.getByRole("button", { name: "歌詞行を作成" }).click();
    const notice = panel.getByRole("alert").filter({ hasText: RIGHTS_RESET_TITLE });
    await expect(notice).toBeVisible();
    await expect(notice).toContainText("歌詞の権利");
    await expect(panel.getByRole("alert").filter({ hasText: "読み込み時の注意" })).toHaveCount(0);
    await shot(page, "22-rights-reset-lyrics");
  });

  await test.step("the rights tab lists both items; confirming them clears the notices", async () => {
    await page
      .getByRole("tabpanel", { name: /歌詞/ })
      .getByRole("alert")
      .filter({ hasText: RIGHTS_RESET_TITLE })
      .getByRole("link", { name: "権利確認へ" })
      .click();
    await expect(page).toHaveURL(/[?&]tab=rights/);
    await expect(rightsTab).toHaveAttribute("aria-selected", "true");
    const panel = page.getByRole("tabpanel", { name: /権利確認/ });
    await expect(panel.getByRole("alert").filter({ hasText: "再確認が必要" })).toContainText("音源の権利");
    await expect(panel.getByRole("alert").filter({ hasText: "再確認が必要" })).toContainText("歌詞の権利");
    await expect(panel.getByText("要再確認", { exact: true })).toHaveCount(2);
    await shot(page, "23-rights-reconfirm");
    await panel.getByRole("checkbox", { name: /音源の権利/ }).click();
    await panel.getByRole("checkbox", { name: /歌詞の権利/ }).click();
    await panel.getByRole("button", { name: "確認内容を保存" }).click();
    await expect(page.getByText("権利確認を保存しました").first()).toBeVisible();
    await expect(rightsTab).not.toContainText("要再確認");
    await expect(panel.getByText("要再確認", { exact: true })).toHaveCount(0);
    expect((await apiJson<ProjectDto>(api, "GET", `/api/projects/${projectId}`)).rights_confirmed_at).not.toBeNull();
    await page.getByRole("tab", { name: /楽曲解析/ }).click();
    await expect(page.getByRole("alert").filter({ hasText: RIGHTS_RESET_TITLE })).toHaveCount(0);
  });

  await test.step("editing a line's text in the timing table resets 歌詞の権利 too", async () => {
    await page.getByRole("tab", { name: /歌詞/ }).click();
    const panel = page.getByRole("tabpanel", { name: /歌詞/ });
    await panel.getByLabel("歌詞の表記を編集する").click();
    const line = panel.getByLabel("1行目の歌詞", { exact: true });
    await line.fill("夜明け前の街を ふたりで歩いた");
    await line.press("Enter");
    await panel.getByRole("button", { name: "タイミングを保存" }).click();
    const notice = panel.getByRole("alert").filter({ hasText: RIGHTS_RESET_TITLE });
    await expect(notice).toBeVisible();
    await expect(notice).toContainText("歌詞の権利");
    await expect(rightsTab).toContainText("要再確認");
    // The reset survives a reload (it is remembered for this project until the item is confirmed).
    await page.reload();
    await expect(page.getByRole("tab", { name: /権利確認/ })).toContainText("要再確認");
  });
  await setRights(allRights());
  pageLog.assertClean();
});

test("lyrics: notes for excluded section headings and for LRC line ends estimated before a break", async ({ page }, testInfo) => {
  const pageLog = watchConsole(page, testInfo);
  // Start from the demo lyrics (untimed TXT) with every rights item confirmed.
  await apiJson(api, "PUT", `/api/projects/${projectId}/lyrics`, { text: fs.readFileSync(demoFile("demo_lyrics_ja.txt"), "utf8") });
  await setRights(allRights());
  await openPage(page, `/projects/${projectId}?tab=lyrics`);
  const panel = page.getByRole("tabpanel", { name: /歌詞/ });
  const notes = panel.getByRole("alert").filter({ hasText: "読み込み時の注意" });

  await test.step("TXT with headings", async () => {
    await panel.getByRole("button", { name: "編集", exact: true }).click();
    const lines = fs.readFileSync(demoFile("demo_lyrics_ja.txt"), "utf8").trimEnd().split("\n");
    await panel.getByLabel("歌詞テキスト").fill(["[Aメロ]", ...lines.slice(0, 4), "", "サビ：", ...lines.slice(4)].join("\n"));
    // The lines are untimed, so nothing is lost and no confirmation is asked.
    await panel.getByRole("button", { name: "歌詞行を作成" }).click();
    await expect(notes).toContainText("見出しと思われる行 2 行");
    await expect(notes).toContainText("「[Aメロ]」「サビ：」");
    await expect(panel.getByLabel(/^\d+行目の開始$/)).toHaveCount(lines.length);
    // Text only changed by headings (not sung): the lyric lines are the same, so the rights stay confirmed.
    await expect(panel.getByRole("alert").filter({ hasText: RIGHTS_RESET_TITLE })).toHaveCount(0);
    await shot(page, "24-lyrics-heading-note");
  });

  await test.step("LRC with an instrumental break", async () => {
    await panel.getByLabel("歌詞ファイル（.txt / .lrc / .srt）").setInputFiles(demoFile("demo_lyrics_ja.lrc"));
    await expect(notes).toContainText("間奏中も表示され続けないよう");
    // Line 8 starts at 0:36 and the next one at 0:48: its end was estimated from its length.
    const end = await panel.getByLabel("8行目の終了", { exact: true }).inputValue();
    expect(end).toMatch(/^0:(3[7-9]|4[0-4])\.\d{2}$/);
    // Same sung lines as before (only timing changed): the rights stay confirmed.
    await expect(panel.getByRole("alert").filter({ hasText: RIGHTS_RESET_TITLE })).toHaveCount(0);
  });

  await test.step("LRC with a changed line: the import notes and the rights notice are shown separately", async () => {
    const lrc = fs.readFileSync(demoFile("demo_lyrics_ja.lrc"), "utf8").replace("走れ 遠く遥か先へ", "走れ もっと遠くへ");
    await panel.getByLabel("歌詞ファイル（.txt / .lrc / .srt）").setInputFiles({ name: "changed.lrc", mimeType: "text/plain", buffer: Buffer.from(lrc, "utf8") });
    // Timed lines would be replaced: confirm.
    await page.getByRole("alertdialog").getByRole("button", { name: "作り直す" }).click();
    const notice = panel.getByRole("alert").filter({ hasText: RIGHTS_RESET_TITLE });
    await expect(notice).toContainText("歌詞の権利");
    await expect(notes).toContainText("間奏中も表示され続けないよう");
    await expect(notes).not.toContainText("権利");
  });
  await setRights(allRights());
  pageLog.assertClean();
});

test("storyboard preview: unconfirmed lyric rights use the template; the Claude rate limit is explained", async ({ page }, testInfo) => {
  // Chromium logs the (expected) 429 response as a console error.
  const pageLog = watchConsole(page, testInfo, { ignore: [/status of 429/] });
  await setRights({ ...allRights(), lyrics_rights: false });
  // No ANTHROPIC_API_KEY here: report Claude as configured, as with a key.
  await patchApiJson<{ claude_configured: boolean }>(page, "/api/system/capabilities", (capabilities) => {
    capabilities.claude_configured = true;
  });
  await openPage(page, `/projects/${projectId}/generate`);
  const card = page.locator("[data-slot=card]").filter({ hasText: "絵コンテのプレビュー" });
  await expect(card.getByText(/歌詞の権利.*未確認.*テンプレート/)).toBeVisible();
  await expect(card.getByRole("link", { name: "権利確認へ" })).toHaveAttribute("href", `/projects/${projectId}?tab=rights`);
  await shot(page, "25-storyboard-rights-note");

  await setRights(allRights());
  await page.reload();
  await expect(card.getByText(/歌詞の権利.*未確認/)).toHaveCount(0);
  // The backend allows 10 Claude previews per 10 minutes and user (429 rate_limited); simulate the 11th.
  await page.route(`${API_URL}/api/projects/${projectId}/storyboard`, (route) =>
    route.request().method() !== "POST"
      ? route.fallback()
      : route.fulfill({
          status: 429,
          contentType: "application/json",
          headers: { "Retry-After": "120", "Access-Control-Allow-Origin": "*" },
          json: {
            detail: "Claude による絵コンテのプレビューは 10 分間に 10 回までです。約 120 秒後にもう一度お試しください。",
            code: "rate_limited",
          },
        }),
  );
  await card.getByRole("button", { name: "プレビューを作成" }).click();
  const limit = card.getByRole("alert").filter({ hasText: "回数の上限に達しました" });
  await expect(limit).toBeVisible();
  await expect(limit).toContainText("約 120 秒後");
  await expect(limit).toContainText("生成開始には影響しません");
  await expect(card.getByText("絵コンテを作成できませんでした")).toHaveCount(0);
  await shot(page, "26-storyboard-rate-limit");
  await page.unrouteAll({ behavior: "ignoreErrors" });
  pageLog.assertClean();
});
