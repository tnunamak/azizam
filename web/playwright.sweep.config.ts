// Quality sweep: every route and state, 390/1440 px, light/dark, owner/member.
// `npm run sweep` (household fixture), `npm run sweep:live` (live-data harness),
// `npm run sweep:copy` (live-data harness on a copy of production data).
import "./test-priority"; // first: nice 19 + idle I/O for the runner and everything it starts
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

type Server = { command: string; env: Record<string, string>; url: string };
const servers: Server[] =
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
    : target.name === "copy"
      ? [
          {
            // Production data, copied read-only into one scratch directory that is deleted on exit.
            command: `${quote(python)} ../scripts/ui/live_data_seed.py --serve --port ${target.port} --copy-from ${quote(
              process.env.SWEEP_COPY_FROM ?? path.join(process.env.HOME ?? "", "applications", "azizam"),
            )}${process.env.SWEEP_DATA_STATE === "restarted" ? " --restarted" : ""}`,
            env: { UI_E2E_PORT: String(target.port), LIVE_DATA_MANIFEST_PATH: paths.seedManifest },
            url: `${target.baseURL}/health`,
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
  // The production copy has months of history; more than two browsers starve its gateway.
  workers: Number(process.env.SWEEP_WORKERS ?? (target.name === "copy" ? "2" : "4")),
  fullyParallel: true,
  retries: 0,
  timeout: 600_000,
  expect: { timeout: 10_000 },
  reporter: [
    ["dot"],
    ["json", { outputFile: path.join(paths.dir, "playwright.json") }],
    // Case receipts for scripts/test-scheduled; writes only when CASE_RECEIPTS_FILE is set.
    ["./e2e/case-receipts.ts"],
  ],
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
    timeout: 240_000,
    gracefulShutdown: { signal: "SIGTERM" as const, timeout: 15_000 },
  })),
});
