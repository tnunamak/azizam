import "./test-priority"; // first: nice 19 + idle I/O for the runner and everything it starts
import { defineConfig } from "@playwright/test";
import { existsSync } from "node:fs";
import path from "node:path";

// Browser tests that need the lived-in household (scripts/ui/household_seed.py):
// several people with passwords, and creations made by different makers.
// The default suite's fixture has one owner and no media.
const port = Number(process.env.UI_E2E_PORT ?? "18184");
if (!Number.isInteger(port) || port < 18000 || port > 18998) {
  throw new Error("UI_E2E_PORT must be an integer between 18000 and 18998");
}
const strictEnginePorts = Array.from({ length: Math.min(8, 18997 - port) }, (_, index) =>
  String(port + 2 + index),
).join(",");
const localPython = path.resolve(__dirname, "../venv/bin/python");
const python =
  process.env.UI_E2E_PYTHON ??
  (existsSync(localPython) ? localPython : "python3");
const quote = (value: string) => `'${value.replaceAll("'", "'\\''")}'`;
const manifestPath = path.resolve(
  __dirname,
  `test-results/household-manifest-${port}.json`,
);

export default defineConfig({
  testDir: "./e2e",
  testMatch: "*.household.ts",
  outputDir: `./test-results/household-artifacts-${port}`,
  workers: 1,
  // A first failure stays a failure (TEST_STRATEGY.md 3.1).
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: [
    ["list"],
    ["html", { open: "never", outputFolder: `test-results/household-report-${port}` }],
    // Case receipts for scripts/ci-local; writes only when CASE_RECEIPTS_FILE is set.
    ["./e2e/case-receipts.ts"],
  ],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    headless: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: `${quote(python)} ../scripts/ui/serve.py`,
    env: {
      UI_E2E_PORT: String(port),
      UI_SEED_PROFILE: "household",
      UI_HOUSEHOLD_MANIFEST_PATH: manifestPath,
      UI_E2E_ALLOWED_PORTS: [
        process.env.UI_E2E_ALLOWED_PORTS,
        strictEnginePorts,
      ]
        .filter((value) => value !== undefined && value !== "")
        .join(","),
    },
    url: `http://127.0.0.1:${port}/health`,
    reuseExistingServer: false,
    timeout: 120_000,
    gracefulShutdown: { signal: "SIGTERM", timeout: 15_000 },
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
