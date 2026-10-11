import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { expect, type APIRequestContext, type Page, type Route, type TestInfo } from "@playwright/test";

export const API_URL = (process.env.E2E_API_URL ?? "http://localhost:8000").replace(/\/+$/, "");
export const DEMO_DIR = path.resolve(__dirname, "..", "..", "backend", "var", "demo");
export const SCREENSHOT_DIR = path.resolve(__dirname, "screenshots");

export const demoFile = (name: string) => path.join(DEMO_DIR, name);

/** Fails early with a helpful message when the demo assets were not generated. */
export function requireDemoAssets(): void {
  const missing = ["demo_song.mp3", "demo_lyrics_ja.txt", "clip_01.mp4", "clip_02.mp4", "clip_03.mp4"].filter(
    (name) => !fs.existsSync(demoFile(name)),
  );
  if (missing.length > 0) {
    throw new Error(
      `Demo assets missing (${missing.join(", ")}). Run: backend/.venv/bin/python backend/scripts/make_demo_assets.py`,
    );
  }
}

/**
 * Full-page screenshot into e2e/screenshots/<name>.png. The page is scrolled back to the
 * top before capturing (sticky header/sidebar would otherwise be painted mid-page in a
 * full-page capture) and toasts are hidden so they do not cover the content under review.
 */
export async function screenshot(page: Page, name: string): Promise<void> {
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  await page.evaluate(() => document.fonts.ready);
  // Thumbnails are `loading="lazy"`: Chromium only fetches them near the viewport, so scroll
  // each one into view once, then wait until every image has decoded pixels (a full-page
  // capture would otherwise show blank tiles below the fold).
  await page.evaluate(async () => {
    const nextFrame = () => new Promise((resolve) => requestAnimationFrame(() => resolve(null)));
    for (const img of Array.from(document.images)) {
      img.scrollIntoView({ block: "center" });
      await nextFrame();
    }
  });
  await page
    .waitForFunction(
      () => Array.from(document.images).every((img) => !img.getAttribute("src") || (img.complete && img.naturalWidth > 0)),
      undefined,
      { timeout: 10_000 },
    )
    .catch(() => undefined);
  await page.evaluate(() =>
    Promise.race([
      Promise.all(Array.from(document.images).map((img) => img.decode().catch(() => undefined))),
      new Promise((resolve) => setTimeout(resolve, 3_000)),
    ]),
  );
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: path.join(SCREENSHOT_DIR, `${name}.png`),
    fullPage: true,
    animations: "disabled",
    style: '[role="region"][aria-label^="通知"] { display: none !important; }',
  });
}

/** Collects uncaught page errors and console errors; call `assertClean()` at the end of a test. */
export function watchConsole(page: Page, testInfo: TestInfo) {
  const pageErrors: string[] = [];
  const consoleErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(`${error.name}: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(`${message.text()} @ ${message.location().url}`);
  });
  return {
    assertClean() {
      if (consoleErrors.length > 0) {
        testInfo.annotations.push({ type: "console-errors", description: consoleErrors.join("\n") });
      }
      expect(pageErrors, "uncaught page errors").toEqual([]);
      expect(consoleErrors, "console errors").toEqual([]);
    },
  };
}

export interface ProbeResult {
  width: number;
  height: number;
  vcodec: string;
  acodec: string | null;
  duration: number;
  pixFmt: string;
}

/** ffprobe summary of a media file (throws when ffprobe cannot read it). */
export function ffprobe(file: string): ProbeResult {
  const out = execFileSync(
    "ffprobe",
    ["-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,pix_fmt:format=duration", "-of", "json", file],
    { encoding: "utf8" },
  );
  const info = JSON.parse(out) as {
    streams: { codec_type: string; codec_name: string; width?: number; height?: number; pix_fmt?: string }[];
    format: { duration: string };
  };
  const video = info.streams.find((s) => s.codec_type === "video");
  const audio = info.streams.find((s) => s.codec_type === "audio");
  if (!video) throw new Error(`${file}: no video stream`);
  return {
    width: video.width ?? 0,
    height: video.height ?? 0,
    vcodec: video.codec_name,
    pixFmt: video.pix_fmt ?? "",
    acodec: audio?.codec_name ?? null,
    duration: Number(info.format.duration),
  };
}

/** True when the browser can decode the H.264/AAC MP4s the backend renders. */
export async function browserPlaysH264(page: Page): Promise<boolean> {
  return page.evaluate(() => {
    const video = document.createElement("video");
    return video.canPlayType('video/mp4; codecs="avc1.640028, mp4a.40.2"') !== "";
  });
}

/**
 * The open-source Chromium used by default has no H.264/AAC decoder, so a `<video>`
 * on the real MP4 can never load metadata there. As a stand-in, rendered MP4s on
 * signed `/media/` URLs are fetched from the backend (real response, checked with
 * ffprobe) and transcoded to VP9/Opus WebM with the same duration before the
 * browser sees them. Only installed when the browser lacks H.264; with a Chrome
 * build (PW_CHROMIUM_PATH) the real MP4 is played.
 */
export async function transcodeRendersForBrowser(page: Page, testInfo: TestInfo): Promise<void> {
  const cache = new Map<string, Buffer>();
  testInfo.annotations.push({
    type: "codec-workaround",
    description: "Browser lacks H.264/AAC: rendered MP4s were transcoded to WebM in the test harness before playback.",
  });
  await page.route(
    (url) => url.pathname.startsWith("/media/") && url.pathname.endsWith(".mp4") && !url.searchParams.has("download"),
    async (route: Route) => {
      const key = new URL(route.request().url()).pathname;
      let body = cache.get(key);
      if (!body) {
        const headers = { ...route.request().headers() };
        delete headers.range;
        const response = await route.fetch({ headers });
        expect(response.status(), `media fetch ${key}`).toBeLessThan(300);
        const dir = fs.mkdtempSync(path.join(os.tmpdir(), "mvf-e2e-"));
        const source = path.join(dir, "source.mp4");
        const target = path.join(dir, "preview.webm");
        fs.writeFileSync(source, await response.body());
        ffprobe(source); // the real response must be a readable video
        execFileSync("ffmpeg", [
          "-v", "error", "-y", "-i", source,
          "-vf", "scale=270:480", "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-b:v", "300k",
          "-c:a", "libopus", "-b:a", "64k", target,
        ]);
        body = fs.readFileSync(target);
        cache.set(key, body);
        fs.rmSync(dir, { recursive: true, force: true });
      }
      await route.fulfill({ status: 200, contentType: "video/webm", body, headers: { "accept-ranges": "none" } });
    },
  );
}

/** Waits until a `<video>` element has metadata and returns its duration in seconds. */
export async function videoDuration(page: Page, selector: string, timeout = 60_000): Promise<number> {
  const handle = page.locator(selector).first();
  await expect(handle).toBeAttached({ timeout });
  await page.waitForFunction(
    (sel) => {
      const element = document.querySelector<HTMLVideoElement>(sel);
      return !!element && element.readyState >= 1 && Number.isFinite(element.duration);
    },
    selector,
    { timeout },
  );
  return handle.evaluate((element: HTMLVideoElement) => element.duration);
}

/**
 * Rewrites the JSON body of the backend's `GET {apiPath}` before the page sees it. Only for
 * state this environment cannot produce for real (no Runway API key: a configured Runway
 * provider, a batch generated with it); the rest of the response is the real one.
 */
export async function patchApiJson<T>(page: Page, apiPath: string, patch: (body: T) => void): Promise<void> {
  await page.route(
    (url) => url.pathname === apiPath,
    async (route: Route) => {
      if (route.request().method() !== "GET") return route.fallback();
      const response = await route.fetch();
      const body = (await response.json()) as T;
      patch(body);
      // The body changes size and is no longer encoded: drop the original framing headers.
      const headers = Object.fromEntries(
        Object.entries(response.headers()).filter(([name]) => !["content-length", "content-encoding"].includes(name.toLowerCase())),
      );
      await route.fulfill({ status: response.status(), headers, json: body });
    },
  );
}

// --- direct API helpers (used to prepare the library test project) -------------------------------

export async function apiJson<T>(request: APIRequestContext, method: string, url: string, data?: unknown): Promise<T> {
  const response = await request.fetch(`${API_URL}${url}`, { method, data });
  expect(response.ok(), `${method} ${url} → ${response.status()} ${await response.text()}`).toBeTruthy();
  return (response.status() === 204 ? null : await response.json()) as T;
}

export async function waitFor<T>(fn: () => Promise<T | null | undefined | false>, { timeout = 120_000, interval = 1_000, message = "condition" } = {}): Promise<T> {
  const deadline = Date.now() + timeout;
  for (;;) {
    const value = await fn();
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`Timed out waiting for ${message}`);
    await new Promise((resolve) => setTimeout(resolve, interval));
  }
}

export const RIGHTS_KEYS = ["audio_rights", "lyrics_rights", "no_real_person_likeness", "model_terms_checked", "platform_terms"] as const;
