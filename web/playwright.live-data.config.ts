import "./test-priority"; // first: nice 19 + idle I/O for the runner and everything it starts
import { defineConfig, firefox } from "@playwright/test";
import { existsSync } from "node:fs";
import path from "node:path";

const port = Number(process.env.UI_E2E_PORT ?? "18182");
if (!Number.isInteger(port) || port < 18000 || port > 18998) {
  throw new Error("UI_E2E_PORT must be an integer between 18000 and 18998");
}

const localPython = path.resolve(__dirname, "../venv/bin/python");
const python =
  process.env.UI_E2E_PYTHON ??
  (existsSync(localPython) ? localPython : "python3");
const quote = (value: string) => `'${value.replaceAll("'", "'\\''")}'`;
// Legacy setup/spec consumers use these filenames. ci-local gives this config
// a dedicated browser-${port} worktree, isolating its auth and manifest too.
const manifestPath = path.resolve(
  __dirname,
  "test-results/live-data-manifest.json",
);

export default defineConfig({
  testDir: "./e2e",
  testMatch: ["live-data-scoping.spec.ts", "live-data-audio.spec.ts"],
  globalSetup: "./e2e/live-data-global-setup.ts",
  outputDir: `./test-results/live-data-artifacts-${port}`,
  workers: 1,
  // A first failure stays a failure (TEST_STRATEGY.md 3.1).
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: [
    ["list"],
    ["html", { open: "never", outputFolder: `test-results/live-data-report-${port}` }],
    // Case receipts for scripts/ci-local; writes only when CASE_RECEIPTS_FILE is set.
    ["./e2e/case-receipts.ts"],
  ],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    storageState: "./test-results/live-data-owner.json",
    headless: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: `${quote(python)} ../scripts/ui/live_data_seed.py --serve --port ${port}`,
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
            testMatch: "live-data-audio.spec.ts",
          },
        ]
      : []),
  ],
});
