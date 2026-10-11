import path from "node:path";

import { defineConfig, devices } from "@playwright/test";

/**
 * Browser E2E against the real stack: the production Next.js build on :3000 and
 * the FastAPI backend (TASK_MODE=inline) on :8000. Both are reused when already
 * running; otherwise Playwright starts them (see `webServer`).
 *
 * Env:
 * - `E2E_BASE_URL` (default http://localhost:3000), `E2E_API_URL` (default http://localhost:8000)
 * - `PW_CHROMIUM_PATH` (default /opt/pw-browsers/chromium). The bundled open-source
 *   Chromium cannot decode H.264/AAC; point this at a Chrome build (e.g. Chrome for
 *   Testing) to play the rendered MP4s for real. See e2e/README.md.
 */
const baseURL = process.env.E2E_BASE_URL ?? "http://localhost:3000";
const apiURL = process.env.E2E_API_URL ?? "http://localhost:8000";
const backendDir = path.join(__dirname, "..", "backend");

export default defineConfig({
  testDir: "./e2e",
  outputDir: "./test-results",
  // One worker: the inline backend renders two videos at a time and the journeys are long.
  workers: 1,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  // Analysis + 3 renders + a re-render take ~3 min on 4 CPUs; leave generous headroom.
  timeout: 15 * 60_000,
  expect: { timeout: 30_000 },
  reporter: [["list"], ["html", { open: "never", outputFolder: "./playwright-report" }]],
  use: {
    baseURL,
    viewport: { width: 1440, height: 900 },
    locale: "ja-JP",
    timezoneId: "Asia/Tokyo",
    colorScheme: "dark",
    acceptDownloads: true,
    actionTimeout: 30_000,
    navigationTimeout: 60_000,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    launchOptions: {
      executablePath: process.env.PW_CHROMIUM_PATH ?? "/opt/pw-browsers/chromium",
      // Chromium derives download file names through the process locale; without a UTF-8
      // locale (LANG unset in this container) every Japanese file name becomes "download".
      env: { ...process.env, LANG: process.env.LANG || "C.UTF-8" },
    },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } }],
  webServer: [
    {
      command: ".venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000",
      cwd: backendDir,
      url: `${apiURL}/api/health`,
      reuseExistingServer: true,
      timeout: 120_000,
      env: { TASK_MODE: "inline", AUTH_MODE: "dev", CORS_ORIGINS: baseURL, PUBLIC_API_BASE_URL: apiURL },
    },
    {
      command: "npx next start -p 3000",
      cwd: __dirname,
      url: baseURL,
      reuseExistingServer: true,
      timeout: 120_000,
    },
  ],
});
