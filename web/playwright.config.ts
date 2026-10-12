import { defineConfig } from "@playwright/test";
import { existsSync } from "node:fs";
import path from "node:path";

const port = Number(process.env.UI_E2E_PORT ?? "18180");
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
// Quote for the webServer shell, including Python paths with spaces.
const quote = (value: string) => `'${value.replaceAll("'", "'\\''")}'`;
const declaredFixturePorts = [process.env.UI_E2E_ALLOWED_PORTS, strictEnginePorts]
  .filter((value) => value !== undefined && value !== "")
  .join(",");
export default defineConfig({
  testDir: "./e2e",
  testIgnore: ["**/live-data-*.spec.ts", "**/sweep/**"],
  globalSetup: "./e2e/global-setup.ts",
  outputDir: `./test-results/artifacts-${port}`,
  workers: 1,
  // No retries: a first failure stays a failure (TEST_STRATEGY.md 3.1).
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [
    ["list"],
    ["html", { open: "never", outputFolder: `test-results/report-${port}` }],
    // Case receipts: scripts/ci-local merges this into cases.jsonl.
    ["json", { outputFile: `test-results/cases-${port}.json` }],
  ],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    storageState: `./test-results/auth-${port}.json`,
    headless: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: `${quote(python)} ../scripts/ui/serve.py`,
    env: {
      UI_E2E_PORT: String(port),
      UI_E2E_ALLOWED_PORTS: declaredFixturePorts,
    },
    url: `http://127.0.0.1:${port}/health`,
    reuseExistingServer: false,
    timeout: 120_000,
    gracefulShutdown: { signal: "SIGTERM", timeout: 15_000 },
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
