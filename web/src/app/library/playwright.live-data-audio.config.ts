import { defineConfig, firefox } from "@playwright/test";
import { existsSync } from "node:fs";
import path from "node:path";

const webRoot = path.resolve(__dirname, "../../..");
const port = Number(process.env.UI_E2E_PORT ?? "18382");
if (!Number.isInteger(port) || port < 18000 || port > 18998) {
  throw new Error("UI_E2E_PORT must be an integer between 18000 and 18998");
}

const localPython = path.resolve(webRoot, "../venv/bin/python");
const python =
  process.env.UI_E2E_PYTHON ??
  (existsSync(localPython) ? localPython : "python3");
const quote = (value: string) => `'${value.replaceAll("'", "'\\''")}'`;
const manifestPath = path.join(
  webRoot,
  "test-results/library-live-data-manifest.json",
);

export default defineConfig({
  testDir: __dirname,
  testMatch: "live-data-audio.e2e.spec.ts",
  globalSetup: path.join(webRoot, "e2e/live-data-global-setup.ts"),
  outputDir: path.join(webRoot, "test-results/library-live-data-artifacts"),
  workers: 1,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    storageState: path.join(webRoot, "test-results/live-data-owner.json"),
    headless: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: `${quote(python)} ../scripts/ui/live_data_seed.py --serve --port ${port}`,
    cwd: webRoot,
    env: {
      UI_E2E_PORT: String(port),
      LIVE_DATA_MANIFEST_PATH: manifestPath,
    },
    url: `http://127.0.0.1:${port}/health`,
    reuseExistingServer: false,
    timeout: 120_000,
    gracefulShutdown: { signal: "SIGTERM", timeout: 15_000 },
  },
  projects: [
    { name: "chromium", use: { browserName: "chromium" } },
    ...(existsSync(firefox.executablePath())
      ? [
          {
            name: "firefox",
            use: { browserName: "firefox" as const },
          },
        ]
      : []),
  ],
});
