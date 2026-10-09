// Quality sweep: every route and state, 390/1440 px, light/dark, owner/member.
// Run with `npm run sweep` (fixtures) or `npm run sweep:live` (live-data harness).
import { defineConfig } from "@playwright/test";
import { existsSync } from "node:fs";
import path from "node:path";
import { sweepPaths, sweepTarget } from "./e2e/sweep/target";

const target = sweepTarget();
const paths = sweepPaths(target);
const localPython = path.resolve(__dirname, "../venv/bin/python");
const python =
  process.env.UI_E2E_PYTHON ?? (existsSync(localPython) ? localPython : "python3");
const quote = (value: string) => `'${value.replaceAll("'", "'\\''")}'`;

const servers =
  target.name === "fixtures"
    ? [
        {
          // The lived-in household: owner Sam, member Robin, history, media.
          command: `${quote(python)} ../scripts/ui/serve.py`,
          env: {
            UI_E2E_PORT: String(target.port),
            UI_SEED_PROFILE: "household",
            UI_HOUSEHOLD_SCENARIO: process.env.UI_HOUSEHOLD_SCENARIO ?? "default",
            UI_HOUSEHOLD_MANIFEST_PATH: paths.seedManifest,
          },
          url: `${target.baseURL}/health`,
        },
        {
          // A fresh, unclaimed gateway for the first-run Setup screen.
          command: `${quote(python)} ../scripts/ui/serve.py`,
          env: { UI_E2E_PORT: String(target.setupPort) },
          url: `${target.setupBaseURL}/health`,
        },
      ]
    : [
        {
          command: `${quote(python)} ../scripts/ui/live_data_seed.py --serve --port ${target.port}`,
          env: { UI_E2E_PORT: String(target.port), LIVE_DATA_MANIFEST_PATH: paths.seedManifest },
          url: `${target.baseURL}/health`,
        },
      ];

export default defineConfig({
  testDir: "./e2e/sweep",
  testMatch: "sweep.spec.ts",
  globalSetup: "./e2e/sweep/setup.ts",
  outputDir: path.join(paths.dir, "artifacts"),
  workers: Number(process.env.SWEEP_WORKERS ?? "4"),
  fullyParallel: true,
  retries: 0,
  timeout: 240_000,
  expect: { timeout: 10_000 },
  reporter: [["dot"], ["json", { outputFile: path.join(paths.dir, "playwright.json") }]],
  use: {
    baseURL: target.baseURL,
    headless: true,
    trace: "off",
    screenshot: "off",
    browserName: "chromium",
  },
  webServer: servers.map((server) => ({
    ...server,
    reuseExistingServer: false,
    timeout: 120_000,
    gracefulShutdown: { signal: "SIGTERM" as const, timeout: 15_000 },
  })),
});
