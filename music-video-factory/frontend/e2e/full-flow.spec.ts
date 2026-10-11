import fs from "node:fs";
import path from "node:path";

import { expect, test, type Locator, type Page } from "@playwright/test";

import {
  API_URL,
  apiJson,
  browserPlaysH264,
  demoFile,
  ffprobe,
  patchApiJson,
  requireDemoAssets,
  RIGHTS_KEYS,
  screenshot,
  transcodeRendersForBrowser,
  videoDuration,
  waitFor,
  watchConsole,
} from "./support";

interface LyricLineDto {
  id: string;
  text: string;
  start: number | null;
  end: number | null;
  source: string;
}
interface ShotDto {
  clip_id: string | null;
}
interface VideoDto {
  id: string;
  batch_id: string;
  status: string;
  render_version: number;
  url: string | null;
  plan: { lyric_style: string; color_grade: string; start: number; shots: ShotDto[]; lyric_offsets?: Record<string, { start: number; end: number }> };
}
interface ClipDto {
  id: string;
  source: string;
  status: string;
}

const RENDER_TIMEOUT = 8 * 60_000;
/** Label of `lyric_style: "auto"` (the preset's styles rotated per video). */
const AUTO_LYRIC_STYLE = "自動（動画ごとに変える）";
const POWERED_BY_RUNWAY = "Powered by Runway";

test.beforeAll(() => requireDemoAssets());

/** Parses the `m:ss.cc` shown by the time fields. */
function parseClock(value: string): number {
  const match = /^(\d+):(\d{2})\.(\d{2})$/.exec(value.trim());
  if (!match) throw new Error(`unexpected time text: ${value}`);
  return Number(match[1]) * 60 + Number(match[2]) + Number(match[3]) / 100;
}

/** Same format as the UI's `formatTimePrecise` (m:ss.cc). */
function formatClock(seconds: number): string {
  const centis = Math.round(seconds * 100);
  return `${Math.floor(centis / 6000)}:${String(Math.floor((centis % 6000) / 100)).padStart(2, "0")}.${String(centis % 100).padStart(2, "0")}`;
}

function toast(page: Page, text: string | RegExp): Locator {
  return page.getByText(text).first();
}

/**
 * The value cell of a visible `<dt>label</dt><dd>value</dd>` settings / summary row (the
 * previous route stays mounted but hidden after a client-side navigation).
 */
function settingValue(page: Page, label: string): Locator {
  return page.locator(`xpath=//dt[normalize-space()="${label}"]/following-sibling::dd[1]`).filter({ visible: true });
}

/** The project id from a `/projects/{id}` URL. */
function projectIdFrom(page: Page): string {
  const match = /\/projects\/([0-9a-f]{32})/.exec(page.url());
  if (!match) throw new Error(`not a project URL: ${page.url()}`);
  return match[1];
}

test("full journey: mock provider, 3 × 15 s, preview, download, edit and re-render", async ({ page, request }, testInfo) => {
  const pageLog = watchConsole(page, testInfo);
  const title = `夜明けのうた E2E ${new Date().toISOString().slice(11, 19)}`;

  await test.step("dashboard", async () => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "ダッシュボード", level: 1 })).toBeVisible();
    await expect(page.getByText("バックエンド未接続")).toHaveCount(0);
    await screenshot(page, "01-dashboard");
    await page.getByRole("link", { name: "新規プロジェクト" }).first().click();
    await expect(page).toHaveURL(/\/projects\/new$/);
  });

  await test.step("create project and upload the song", async () => {
    await page.locator("#title").fill(title);
    await page.locator("#artist").fill("デモアーティスト");
    // Other spellings of the artist name are kept out of every generation prompt (likeness guard).
    await page.locator("#aliases").fill("Demo Artist, でもあーてぃすと");
    await page.locator("#mood").fill("切ない、夜明け前の静けさ");
    await page.getByRole("radio", { name: /都会の夜/ }).click();
    await expect(page.getByRole("radio", { name: /都会の夜/ })).toHaveAttribute("aria-checked", "true");
    await page.locator('input[type="file"]').setInputFiles(demoFile("demo_song.mp3"));
    await expect(page.getByLabel("選択した音源のプレビュー")).toBeVisible();
    await expect(page.getByText("1分00秒").first()).toBeVisible();
    await screenshot(page, "02-new-project");
    await page.getByRole("button", { name: "作成して音源をアップロード" }).click();
    await page.waitForURL(/\/projects\/[0-9a-f]{32}$/, { timeout: 120_000 });
  });

  const projectId = projectIdFrom(page);

  await test.step("analysis: waveform and a suggested 15 s segment", async () => {
    await expect(page.getByRole("tab", { name: /楽曲解析/ })).toHaveAttribute("aria-selected", "true");
    const waveform = page.getByRole("group", { name: /波形/ });
    await expect(waveform).toBeVisible({ timeout: 120_000 });
    const canvasBox = await waveform.locator("canvas").first().boundingBox();
    expect(canvasBox?.width ?? 0).toBeGreaterThan(600);
    await expect(page.getByText("おすすめ区間")).toBeVisible();

    // Pick another suggestion first so choosing the 15 s one is a real change that must be saved.
    const pick30 = page.getByRole("button", { name: /^30秒/ });
    if ((await pick30.count()) > 0) {
      await pick30.click();
      await page.getByRole("button", { name: "区間を保存" }).click();
      await expect(toast(page, "区間を保存しました")).toBeVisible();
    }
    const pick15 = page.getByRole("button", { name: /^15秒/ });
    await pick15.click();
    await expect(pick15).toHaveAttribute("aria-pressed", "true");
    await page.getByRole("button", { name: "区間を保存" }).click();
    await expect(page.getByRole("button", { name: "区間を保存" })).toBeDisabled();

    const analysis = await apiJson<{ status: string; segment: { start: number; end: number } | null; analysis: { suggested_segments: { start: number; end: number; duration: number }[] } }>(
      request,
      "GET",
      `/api/projects/${projectId}/analysis`,
    );
    expect(analysis.status).toBe("done");
    const suggested15 = analysis.analysis.suggested_segments.find((s) => s.duration === 15);
    expect(suggested15).toBeTruthy();
    expect(analysis.segment?.start).toBeCloseTo(suggested15!.start, 1);
    expect(analysis.segment!.end - analysis.segment!.start).toBeCloseTo(15, 0);

    // 感情・世界観推定 from the audio mood, the lyrics and the project's mood ("切ない").
    const estimateCard = page.getByText("感情・世界観の推定", { exact: true });
    await expect(estimateCard).toBeVisible();
    await expect(page.getByText("推定される感情")).toBeVisible();
    const estimate = await apiJson<{ emotions: string[]; recommended_presets: string[]; sources: { user: string[] }; analysis_used: boolean }>(
      request,
      "GET",
      `/api/projects/${projectId}/world-estimate`,
    );
    expect(estimate.analysis_used).toBe(true);
    expect(estimate.sources.user).toContain("melancholic");
    expect(estimate.recommended_presets.length).toBeGreaterThan(0);
    const created = await apiJson<{ artist_aliases: string[] }>(request, "GET", `/api/projects/${projectId}`);
    expect(created.artist_aliases).toEqual(["Demo Artist", "でもあーてぃすと"]);
    await screenshot(page, "03-workspace-analysis");
  });

  await test.step("lyrics: paste, auto-sync, nudge one line, save", async () => {
    await page.getByRole("tab", { name: /歌詞/ }).click();
    await expect(page).toHaveURL(/tab=lyrics/);
    const text = fs.readFileSync(demoFile("demo_lyrics_ja.txt"), "utf8");
    await page.getByLabel("歌詞テキスト").fill(text);
    await page.getByRole("button", { name: "歌詞行を作成" }).click();
    await expect(toast(page, "10行の歌詞を読み込みました")).toBeVisible();

    await page.getByRole("button", { name: "推定で合わせる" }).click();
    await expect(toast(page, "タイミングを自動で推定しました")).toBeVisible({ timeout: 60_000 });
    const firstStart = page.getByLabel("1行目の開始", { exact: true });
    await expect(firstStart).not.toHaveValue("");
    const before = parseClock(await firstStart.inputValue());
    expect(before).toBeGreaterThan(6);
    expect(before).toBeLessThan(10);

    await page.getByRole("button", { name: "1行目の開始を0.1秒遅く" }).click();
    await expect(firstStart).toHaveValue(formatClock(before + 0.1));
    await page.getByRole("button", { name: "タイミングを保存" }).click();
    await expect(toast(page, "タイミングを保存しました")).toBeVisible();

    const lyrics = await apiJson<{ lines: LyricLineDto[] }>(request, "GET", `/api/projects/${projectId}/lyrics`);
    expect(lyrics.lines).toHaveLength(10);
    expect(lyrics.lines.every((line) => line.start !== null)).toBeTruthy();
    expect(lyrics.lines[0].start).toBeCloseTo(before + 0.1, 2);
    expect(lyrics.lines[0].source).toBe("manual");
    expect(lyrics.lines[0].text).toBe("夜明け前の街を ひとりで歩いた");
    await screenshot(page, "04-workspace-lyrics");
  });

  await test.step("materials: upload three clips", async () => {
    await page.getByRole("tab", { name: /素材/ }).click();
    const panel = page.getByRole("tabpanel", { name: /素材/ });
    await panel.locator('input[type="file"]').first().setInputFiles([demoFile("clip_01.mp4"), demoFile("clip_02.mp4"), demoFile("clip_03.mp4")]);
    await expect(toast(page, "3本の素材を追加しました")).toBeVisible({ timeout: 120_000 });
    const clips = await apiJson<ClipDto[]>(request, "GET", `/api/projects/${projectId}/clips`);
    expect(clips.filter((clip) => clip.source === "upload" && clip.status === "ready")).toHaveLength(3);
    await expect(panel.getByRole("button", { name: /をプレビュー$/ })).toHaveCount(3);
    await screenshot(page, "05-workspace-materials");
  });

  await test.step("rights: confirm all items", async () => {
    await page.getByRole("tab", { name: /権利確認/ }).click();
    const panel = page.getByRole("tabpanel", { name: /権利確認/ });
    const boxes = panel.getByRole("checkbox");
    await expect(boxes).toHaveCount(5);
    for (let i = 0; i < 5; i += 1) await boxes.nth(i).click();
    await panel.getByRole("button", { name: "確認内容を保存" }).click();
    await expect(toast(page, "権利確認を保存しました")).toBeVisible();
    const project = await apiJson<{ rights_confirmed_at: string | null }>(request, "GET", `/api/projects/${projectId}`);
    expect(project.rights_confirmed_at).not.toBeNull();
    await screenshot(page, "06-workspace-rights");
  });

  await test.step("generate: mock provider, 15 s, 3 videos", async () => {
    await page.getByRole("link", { name: /生成設定へ/ }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${projectId}/generate$`));
    // The world estimate is offered next to the presets; the explicit project preset (都会の夜) stays selected.
    await expect(page.getByText("感情・世界観の推定", { exact: false }).first()).toBeVisible();
    await expect(page.getByRole("radio", { name: /都会の夜/ })).toHaveAttribute("aria-checked", "true");
    await page.getByRole("radio", { name: /モック/ }).click();
    await page.getByRole("radiogroup", { name: "1 本の長さ" }).getByRole("radio", { name: "15秒" }).click();
    await page.getByRole("radiogroup", { name: "本数" }).getByRole("radio", { name: "3本" }).click();
    await expect(page.getByText("合計（概算）")).toBeVisible({ timeout: 60_000 });
    // Wait for the debounced estimate of these exact settings (readiness turns green, start enabled).
    await expect(page.getByText("見積もりは予算内です")).toBeVisible({ timeout: 60_000 });
    await expect(page.getByRole("button", { name: "生成開始" })).toBeEnabled();
    await expect(page.getByText("予算内").first()).toBeVisible();
    await screenshot(page, "07-generate");
    await page.getByRole("button", { name: "生成開始" }).click();
    await page.waitForURL(/\/batches\/[0-9a-f]{32}$/, { timeout: 60_000 });
  });

  const batchId = /\/batches\/([0-9a-f]{32})/.exec(page.url())![1];

  await test.step("batch page shows progress until completion", async () => {
    await expect(page.getByRole("list", { name: "処理の段階" })).toBeVisible();
    await screenshot(page, "08-batch-progress");
    await expect(page.getByText("3 本の動画が完成しました")).toBeVisible({ timeout: RENDER_TIMEOUT });
    const batch = await apiJson<{ status: string; counts: { done: number; total: number }; settings: { provider: string; duration_sec: number; count: number } }>(
      request,
      "GET",
      `/api/batches/${batchId}`,
    );
    expect(batch.status).toBe("completed");
    expect(batch.counts).toMatchObject({ done: 3, total: 3 });
    expect(batch.settings).toMatchObject({ provider: "mock", duration_sec: 15, count: 3 });
    await screenshot(page, "09-batch-complete");
  });

  await test.step("gallery: 3 videos, preview loads metadata", async () => {
    await page.getByRole("link", { name: "動画一覧へ" }).first().click();
    await expect(page).toHaveURL(new RegExp(`/projects/${projectId}/videos`));
    const cards = page.getByRole("button", { name: /をプレビュー$/ });
    await expect(cards).toHaveCount(3);
    const videos = await apiJson<VideoDto[]>(request, "GET", `/api/projects/${projectId}/videos`);
    expect(videos).toHaveLength(3);
    expect(videos.every((video) => video.status === "done")).toBeTruthy();
    await screenshot(page, "10-gallery");

    if (!(await browserPlaysH264(page))) await transcodeRendersForBrowser(page, testInfo);
    await cards.first().click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    const duration = await videoDuration(page, '[role="dialog"] video');
    expect(duration).toBeGreaterThan(14.8);
    expect(duration).toBeLessThan(15.2);
    await expect(dialog.locator("video").first()).toHaveJSProperty("error", null);
    await screenshot(page, "11-preview-dialog");
  });

  await test.step("download one MP4 and verify it with ffprobe", async () => {
    const dialog = page.getByRole("dialog");
    const [download] = await Promise.all([page.waitForEvent("download", { timeout: 60_000 }), dialog.getByRole("button", { name: "ダウンロード" }).click()]);
    // {artist}_{title}_{NN}.mp4 with NN = index + 1 (ARCHITECTURE §5.9); ":" is sanitized.
    expect(download.suggestedFilename()).toBe(`デモアーティスト_${title.replaceAll(":", "_")}_01.mp4`);
    const target = testInfo.outputPath(download.suggestedFilename());
    await download.saveAs(target);
    const probe = ffprobe(target);
    expect(probe).toMatchObject({ width: 1080, height: 1920, vcodec: "h264", acodec: "aac", pixFmt: "yuv420p" });
    expect(probe.duration).toBeGreaterThan(14.9);
    expect(probe.duration).toBeLessThan(15.15);
    testInfo.annotations.push({ type: "download", description: `${download.suggestedFilename()} ${fs.statSync(target).size} bytes` });
  });

  await test.step("editor: change lyric style, nudge a line, swap a clip, re-render", async () => {
    const dialog = page.getByRole("dialog");
    await dialog.getByRole("link", { name: "編集" }).click();
    await expect(page).toHaveURL(/\/videos\/[0-9a-f]{32}\/edit$/);
    const videoId = /\/videos\/([0-9a-f]{32})\/edit/.exec(page.url())![1];
    const before = await apiJson<VideoDto & { available_clips: ClipDto[] }>(request, "GET", `/api/videos/${videoId}`);
    expect(before.render_version).toBe(1);
    await expect(page.getByText("変更はありません")).toBeVisible();

    // 1) lyric style
    await page.getByRole("tab", { name: "歌詞スタイル" }).click();
    const newStyle = before.plan.lyric_style === "karaoke" ? "フェード" : "カラオケ";
    const newStyleId = newStyle === "カラオケ" ? "karaoke" : "fade";
    await page.getByRole("radiogroup", { name: "歌詞スタイル" }).getByRole("radio", { name: new RegExp(newStyle) }).click();

    // 2) nudge the first lyric line of this video by +0.1 s
    await page.getByRole("tab", { name: "歌詞タイミング" }).click();
    await page.getByRole("button", { name: "行全体を 0.1 秒遅らせる" }).first().click();

    // 3) swap shot 1 for an uploaded clip that is not used there
    await page.getByRole("tab", { name: "ショット" }).click();
    await page.getByRole("button", { name: "ショット 1 の素材を差し替え", exact: true }).click();
    const picker = page.getByRole("dialog");
    await expect(picker).toBeVisible();
    const uploads = before.available_clips.filter((clip) => clip.source === "upload" && clip.id !== before.plan.shots[0].clip_id);
    expect(uploads.length).toBeGreaterThan(0);
    // Pick an uploaded clip (labelled 「アップロード」) for shot 1.
    const uploadCandidate = picker.getByRole("list", { name: "素材" }).getByRole("button").filter({ hasText: "アップロード" }).first();
    await uploadCandidate.click();
    await expect(uploadCandidate).toHaveAttribute("aria-pressed", "true");
    await picker.getByRole("button", { name: "この素材に差し替える" }).click();
    await expect(page.getByText("未適用の変更 3 件")).toBeVisible();
    await screenshot(page, "12-editor");

    await page.getByRole("button", { name: "再レンダリング" }).click();
    await expect(toast(page, "再レンダリングを開始しました")).toBeVisible();
    await expect(page.getByTitle("レンダリングのバージョン")).toHaveText("v2");

    const after = await waitFor(
      async () => {
        const video = await apiJson<VideoDto & { available_clips: ClipDto[]; lyric_lines: LyricLineDto[] }>(request, "GET", `/api/videos/${videoId}`);
        if (video.status === "failed") throw new Error(`re-render failed: ${JSON.stringify(video)}`);
        return video.status === "done" && video.render_version === 2 ? video : null;
      },
      { timeout: RENDER_TIMEOUT, interval: 2_000, message: "re-render v2" },
    );
    expect(after.plan.lyric_style).toBe(newStyleId);
    expect(after.plan.shots[0].clip_id).not.toBe(before.plan.shots[0].clip_id);
    expect(uploads.map((clip) => clip.id)).toContain(after.plan.shots[0].clip_id);
    expect(Object.keys(after.plan.lyric_offsets ?? {})).toHaveLength(1);
    await expect(page.getByText("変更はありません")).toBeVisible({ timeout: 60_000 });
    await expect(page.locator("aside").getByText("完成", { exact: true })).toBeVisible({ timeout: 60_000 });
    await screenshot(page, "13-editor-rerendered");
  });

  await test.step("settings page", async () => {
    await page.goto("/settings");
    await expect(page.getByRole("heading", { name: "設定", level: 1 })).toBeVisible();
    await expect(page.getByText("モック（ダミー映像）").first()).toBeVisible();
    await screenshot(page, "14-settings");
    await page.goto("/");
    await expect(page.getByText(title).first()).toBeVisible();
    await screenshot(page, "15-dashboard-after");
  });

  pageLog.assertClean();
});

test("library provider: 3 videos from uploaded clips only, lyric style 自動 varies per video; Runway attribution", async ({ page, request }, testInfo) => {
  const pageLog = watchConsole(page, testInfo);

  // Prepare a ready project directly through the API (the UI path is covered by the journey above).
  const project = await apiJson<{ id: string }>(request, "POST", "/api/projects", {
    title: `ライブラリ E2E ${new Date().toISOString().slice(11, 19)}`,
    artist_name: "デモアーティスト",
    world_preset: "nostalgic",
  });
  const audio = await request.post(`${API_URL}/api/projects/${project.id}/audio`, {
    multipart: { file: { name: "demo_song.mp3", mimeType: "audio/mpeg", buffer: fs.readFileSync(demoFile("demo_song.mp3")) } },
  });
  expect(audio.status()).toBe(201);
  await waitFor(
    async () => (await apiJson<{ status: string }>(request, "GET", `/api/projects/${project.id}/analysis`)).status === "done",
    { message: "analysis", timeout: 180_000 },
  );
  await apiJson(request, "PUT", `/api/projects/${project.id}/lyrics`, { text: fs.readFileSync(demoFile("demo_lyrics_ja.txt"), "utf8") });
  await apiJson(request, "POST", `/api/projects/${project.id}/lyrics/auto-time`, { method: "heuristic" });
  const form = new FormData();
  for (const name of ["clip_01.mp4", "clip_02.mp4", "clip_03.mp4"]) {
    form.append("files", new Blob([fs.readFileSync(demoFile(name))], { type: "video/mp4" }), name);
  }
  const upload = await request.post(`${API_URL}/api/projects/${project.id}/clips/upload`, { multipart: form });
  expect(upload.ok()).toBeTruthy();
  const uploadIds = new Set(((await upload.json()) as ClipDto[]).map((clip) => clip.id));
  expect(uploadIds.size).toBe(3);
  await apiJson(request, "PUT", `/api/projects/${project.id}/rights`, { items: Object.fromEntries(RIGHTS_KEYS.map((key) => [key, true])) });

  await page.goto(`/projects/${project.id}/generate`);
  await page.getByRole("radio", { name: /アップロード素材/ }).click();
  await page.getByRole("radiogroup", { name: "1 本の長さ" }).getByRole("radio", { name: "15秒" }).click();
  await page.getByRole("radiogroup", { name: "本数" }).getByRole("radio", { name: "3本" }).click();
  // 自動 = `lyric_style: "auto"`: the planner rotates the preset's recommended styles per video.
  const autoStyle = page.getByRole("radiogroup", { name: "歌詞スタイル" }).getByRole("radio", { name: /^自動/ });
  await expect(page.getByRole("radiogroup", { name: "歌詞スタイル" }).getByRole("radio", { name: /^シンプル/ })).toHaveAttribute("aria-checked", "true");
  await autoStyle.click();
  await expect(autoStyle).toHaveAttribute("aria-checked", "true");
  await expect(settingValue(page, "歌詞スタイル")).toHaveText(AUTO_LYRIC_STYLE);
  await expect(page.getByText("合計（概算）")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByText("見積もりは予算内です")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByRole("button", { name: "生成開始" })).toBeEnabled();
  await expect(page.getByText("$0.00").first()).toBeVisible();
  await screenshot(page, "16-generate-library");
  await page.getByRole("button", { name: "生成開始" }).click();
  await page.waitForURL(/\/batches\/[0-9a-f]{32}$/, { timeout: 60_000 });
  const batchId = /\/batches\/([0-9a-f]{32})/.exec(page.url())![1];

  await expect(page.getByText("3 本の動画が完成しました")).toBeVisible({ timeout: RENDER_TIMEOUT });
  await screenshot(page, "17-batch-library-complete");

  await test.step("lyric style 自動: shown as such, and the videos got different styles", async () => {
    await expect(settingValue(page, "歌詞スタイル")).toHaveText(AUTO_LYRIC_STYLE);
    const batch = await apiJson<{ settings: { lyric_style: string; preset: string }; videos: VideoDto[] }>(request, "GET", `/api/batches/${batchId}`);
    expect(batch.settings.lyric_style).toBe("auto");
    const styles = batch.videos.map((video) => video.plan.lyric_style);
    expect(styles).toHaveLength(3);
    expect(new Set(styles).size, `per-video styles ${styles.join(", ")}`).toBeGreaterThan(1);
  });

  await page.getByRole("link", { name: "動画一覧へ" }).first().click();
  await expect(page.getByRole("button", { name: /をプレビュー$/ })).toHaveCount(3);

  const videos = await apiJson<VideoDto[]>(request, "GET", `/api/projects/${project.id}/videos?batch_id=${batchId}`);
  expect(videos).toHaveLength(3);
  for (const video of videos) {
    expect(video.status).toBe("done");
    expect(video.plan.shots.length).toBeGreaterThan(0);
    for (const shot of video.plan.shots) expect(uploadIds.has(shot.clip_id ?? "")).toBeTruthy();
  }
  const clips = await apiJson<ClipDto[]>(request, "GET", `/api/projects/${project.id}/clips`);
  expect(clips.every((clip) => clip.source === "upload")).toBeTruthy();

  await test.step("reopening the generate page starts from 自動 again", async () => {
    await page.goto(`/projects/${project.id}/generate`);
    const styles = page.getByRole("radiogroup", { name: "歌詞スタイル" });
    await expect(styles.getByRole("radio", { name: /^自動/ })).toHaveAttribute("aria-checked", "true");
    await expect(styles.getByRole("radio", { checked: true })).toHaveCount(1);
    await expect(settingValue(page, "歌詞スタイル")).toHaveText(AUTO_LYRIC_STYLE);
  });

  await test.step("Runway attribution: settings, provider picker, batch, gallery and preview", async () => {
    const attribution = page.getByRole("link", { name: POWERED_BY_RUNWAY });
    // The settings page lists Runway (configured or not) with its attribution.
    await page.goto("/settings");
    await expect(page.getByRole("heading", { name: "設定", level: 1 })).toBeVisible();
    await expect(attribution).toHaveCount(1);
    await expect(attribution).toHaveAttribute("href", /^https:\/\/runwayml\.com\/?$/);
    await expect(attribution).toHaveAttribute("target", "_blank");

    // No RUNWAY_API_KEY here: report the provider as configured so its card can be selected.
    await patchApiJson<{ providers: { id: string; configured: boolean; available: boolean; reason: string | null }[] }>(
      page,
      "/api/system/capabilities",
      (capabilities) => {
        for (const provider of capabilities.providers) {
          if (provider.id === "runway") Object.assign(provider, { configured: true, available: true, reason: null });
        }
      },
    );
    await page.goto(`/projects/${project.id}/generate`);
    const runwayCard = page.getByRole("radio", { name: /^Runway/ });
    // The card itself is a radio button: the mark is plain text there, the link appears once selected.
    await expect(runwayCard).toContainText(POWERED_BY_RUNWAY);
    await expect(attribution).toHaveCount(0);
    await runwayCard.click();
    await expect(runwayCard).toHaveAttribute("aria-checked", "true");
    await expect(attribution).toBeVisible();
    await expect(attribution).toHaveAttribute("href", /^https:\/\/runwayml\.com\/?$/);
    await expect(page.getByText("見積もりは予算内です")).toBeVisible({ timeout: 60_000 });

    // A batch generated with Runway (provider patched on this batch) carries the attribution.
    type BatchSettingsDto = { id: string; settings: { provider: string } };
    await patchApiJson<BatchSettingsDto>(page, `/api/batches/${batchId}`, (batch) => {
      batch.settings.provider = "runway";
    });
    await patchApiJson<BatchSettingsDto[]>(page, `/api/projects/${project.id}/batches`, (batches) => {
      for (const batch of batches) if (batch.id === batchId) batch.settings.provider = "runway";
    });
    await page.goto(`/batches/${batchId}`);
    await expect(settingValue(page, "生成エンジン")).toContainText("Runway");
    await expect(attribution).toBeVisible();

    await page.goto(`/projects/${project.id}/videos`);
    await expect(page.getByRole("button", { name: /をプレビュー$/ })).toHaveCount(3);
    await expect(attribution).toHaveCount(1);
    if (!(await browserPlaysH264(page))) await transcodeRendersForBrowser(page, testInfo);
    await page.getByRole("button", { name: /をプレビュー$/ }).first().click();
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByRole("link", { name: POWERED_BY_RUNWAY })).toBeVisible();
    await page.keyboard.press("Escape");
    await page.unrouteAll({ behavior: "ignoreErrors" });
  });

  pageLog.assertClean();
  testInfo.annotations.push({ type: "project", description: path.basename(page.url()) });
});
