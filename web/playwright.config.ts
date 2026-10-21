import "./test-priority"; // first: nice 19 + idle I/O for the runner and everything it starts
import { defineConfig } from "@playwright/test";
import { existsSync } from "node:fs";
import path from "node:path";

// One invocation, two fixture gateways (TEST_STRATEGY.md 3.4.7):
// - `chromium` on UI_E2E_PORT runs every test without the @global-state tag,
//   on UI_E2E_WORKERS workers in parallel. These tests only read the shared
//   seed or write under their own namespace (e2e/fixtures/isolation.ts).
// - `chromium-global-state` on UI_E2E_PORT + 10 runs the @global-state tests
//   one at a time, beside the parallel project, on a server of their own.
// - `chromium-first-run` on UI_E2E_PORT + 70 runs first-run.spec.ts against a
//   gateway that no global setup touched: no owner, no engine (the first-run
//   release bar). It is not seeded and has no storage state.
// Each gateway pair owns ten ports: gateway +0, fake engine +1, probes +2..+9.
// The fixture gateways answer a sign-in picture from this file (scripts/ui/fixture_gateway.py).
process.env.UI_E2E_AVATAR_PICTURE ??= path.resolve(
  __dirname,
  "e2e/fixtures/avatar-picture.png",
);
const port = Number(process.env.UI_E2E_PORT ?? "18180");
const globalPort = port + 10;
// A second serial server sits past the pool's extra servers (+40, +50).
const secondGlobalPort = port + 60;
// The fresh gateway for first-run.spec.ts sits after the second global-state one.
const firstRunPort = port + 70;
if (!Number.isInteger(port) || port < 18000 || firstRunPort + 9 > 18998) {
  throw new Error("UI_E2E_PORT must be an integer from 18000 to 18919");
}
// The parallel pool may span 1-3 gateways: one gateway is one Python process,
// and ten workers on it queue behind each other. Workers are spread over the
// servers by parallelIndex (e2e/fixtures/isolation.ts). Extra servers sit at
// +40 and +50 from the pool's port.
const pool = Number(process.env.UI_E2E_POOL ?? "1");
if (!Number.isInteger(pool) || pool < 1 || pool > 3) {
  throw new Error("UI_E2E_POOL must be 1, 2 or 3");
}
const poolPorts = Array.from({ length: pool }, (_, index) => (index === 0 ? port : port + 30 + 10 * index));
// The @global-state tests run one at a time per server. Two servers split them
// by file, so the serial tail is half as long: each file below runs on the
// second server; every other file, including a new one, runs on the first.
const globalServers = Number(process.env.UI_E2E_GLOBAL ?? "1");
if (globalServers !== 1 && globalServers !== 2) {
  throw new Error("UI_E2E_GLOBAL must be 1 or 2");
}
const SECOND_GLOBAL_FILES = [
  /categories\.spec\.ts$/,
  /engines-detect-apply\.spec\.ts$/,
  /receipt-retention\.spec\.ts$/,
  /journeys\.spec\.ts$/,
  /home\.spec\.ts$/,
  /engines-real\.spec\.ts$/,
  /bugfix-actions\.spec\.ts$/,
  /shell\.spec\.ts$/,
  /public-surfaces\.spec\.ts$/,
  /people\.spec\.ts$/,
  /engines-primitives\.spec\.ts$/,
];
const workers = Number(process.env.UI_E2E_WORKERS ?? "2");
if (!Number.isInteger(workers) || workers < 1 || workers > 16) {
  throw new Error("UI_E2E_WORKERS must be an integer from 1 to 16");
}
// Every runner and spec write goes under test-results/, which scripts/ci-local
// links to a per-run directory outside the shared build.
const results = "./test-results";
const localPython = path.resolve(__dirname, "../venv/bin/python");
const python =
  process.env.UI_E2E_PYTHON ??
  (existsSync(localPython) ? localPython : "python3");
// Quote for the webServer shell, including Python paths with spaces.
const quote = (value: string) => `'${value.replaceAll("'", "'\\''")}'`;
// Other configs own these files. A project's own testIgnore replaces this
// list, so every project that sets one repeats it.
const CONFIG_IGNORE = ["**/live-data-*.spec.ts", "**/sweep/**", "**/first-run.spec.ts"];
const GLOBAL_STATE = /@global-state/;
// Files whose cases sign in to Try. The gateway mints one Try key per sign-in
// and revokes that session's older Try keys, and every worker on a server
// shares the lane's sign-in, so two of these files running at once revoke each
// other's key ("Invalid API key"). They run one at a time in their own project.
const TRY_FILES = [
  "**/try.spec.ts",
  "**/workflow-try.spec.ts",
  "**/workflow-try-audit.spec.ts",
  "**/long-jobs.spec.ts",
  "**/bugfix-actions.spec.ts",
];

function server(gatewayPort: number, name: string, extraEnv: Record<string, string> = {}) {
  const strictEnginePorts = Array.from({ length: 8 }, (_, index) => String(gatewayPort + 2 + index));
  return {
    command: `${quote(python)} ../scripts/ui/serve.py`,
    env: {
      ...extraEnv,
      UI_E2E_PORT: String(gatewayPort),
      UI_E2E_ALLOWED_PORTS: [process.env.UI_E2E_ALLOWED_PORTS, ...strictEnginePorts]
        .filter((value) => value !== undefined && value !== "")
        .join(","),
      ...(process.env.UI_E2E_STATE_ROOT
        ? { UI_E2E_STATE_ROOT: path.join(process.env.UI_E2E_STATE_ROOT, name) }
        : {}),
    },
    url: `http://127.0.0.1:${gatewayPort}/health`,
    reuseExistingServer: false,
    timeout: 120_000,
    gracefulShutdown: { signal: "SIGTERM" as const, timeout: 15_000 },
  };
}

export default defineConfig({
  testDir: "./e2e",
  testIgnore: CONFIG_IGNORE,
  globalSetup: "./e2e/global-setup.ts",
  outputDir: `${results}/artifacts-${port}`,
  workers,
  // A first failure stays a failure (TEST_STRATEGY.md 3.1).
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [
    ["list"],
    ["html", { open: "never", outputFolder: `${results}/report-${port}` }],
    // Case receipts for scripts/ci-local; writes only when CASE_RECEIPTS_FILE is set.
    ["./e2e/case-receipts.ts"],
  ],
  use: {
    browserName: "chromium",
    headless: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: [
    ...poolPorts.map((poolPort, index) => server(poolPort, `parallel-${index}`)),
    server(globalPort, "global-state"),
    ...(globalServers === 2 ? [server(secondGlobalPort, "global-state-2")] : []),
    server(firstRunPort, "first-run", { UI_E2E_FIRST_RUN: "1" }),
  ],
  projects: [
    // Listed first: Playwright hands out test groups in project order, so the
    // serial global-state project starts at once and runs beside the pool
    // instead of after it.
    {
      name: "chromium-global-state",
      grep: GLOBAL_STATE,
      ...(globalServers === 2 ? { testIgnore: [...CONFIG_IGNORE, ...SECOND_GLOBAL_FILES] } : {}),
      workers: 1,
      use: {
        baseURL: `http://127.0.0.1:${globalPort}`,
        storageState: `${results}/auth-${globalPort}.json`,
      },
    },
    ...(globalServers === 2
      ? [
          {
            name: "chromium-global-state-2",
            grep: GLOBAL_STATE,
            testMatch: SECOND_GLOBAL_FILES,
            workers: 1,
            use: {
              baseURL: `http://127.0.0.1:${secondGlobalPort}`,
              storageState: `${results}/auth-${secondGlobalPort}.json`,
            },
          },
        ]
      : []),
    // Second: the Try chain is the longest serial path outside global state.
    {
      name: "chromium-try",
      grepInvert: GLOBAL_STATE,
      testMatch: TRY_FILES,
      workers: 1,
      use: {
        baseURL: `http://127.0.0.1:${port}`,
        storageState: `${results}/auth-${port}.json`,
      },
    },
    // Start this long serial file before the pool can leave it as a tail.
    // It keeps its existing in-file order and shares the same seeded gateway.
    {
      name: "chromium-people",
      grepInvert: GLOBAL_STATE,
      testMatch: "**/people.spec.ts",
      workers: 1,
      use: {
        baseURL: `http://127.0.0.1:${port}`,
        storageState: `${results}/auth-${port}.json`,
      },
    },
    {
      name: "chromium",
      grepInvert: GLOBAL_STATE,
      testIgnore: [...CONFIG_IGNORE, ...TRY_FILES, "**/people.spec.ts"],
      // Cases of one file spread over the pool's workers, so one long file does
      // not hold the tail. A file whose cases share a beforeAll or an order
      // says so with test.describe.configure({ mode: "default" | "serial" }).
      fullyParallel: true,
      use: {
        baseURL: `http://127.0.0.1:${port}`,
        storageState: `${results}/auth-${port}.json`,
      },
    },
    // The fresh gateway: global setup skips it (metadata.fresh), so the test
    // starts where a new operator does, at /setup with no owner.
    {
      name: "chromium-first-run",
      testMatch: "**/first-run.spec.ts",
      testIgnore: [],
      metadata: { fresh: true },
      workers: 1,
      use: { baseURL: `http://127.0.0.1:${firstRunPort}` },
    },
    // Seed-only projects for the pool's extra servers: they run no test of their
    // own, but global setup seeds one owner on every project's server.
    ...poolPorts.slice(1).map((poolPort, index) => ({
      name: `chromium-pool-${index + 1}`,
      testMatch: "no-test-runs-here.never",
      use: {
        baseURL: `http://127.0.0.1:${poolPort}`,
        storageState: `${results}/auth-${poolPort}.json`,
      },
    })),
  ],
});
