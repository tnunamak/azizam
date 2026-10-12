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
// Each gateway pair owns ten ports: gateway +0, fake engine +1, probes +2..+9.
const port = Number(process.env.UI_E2E_PORT ?? "18180");
const globalPort = port + 10;
if (!Number.isInteger(port) || port < 18000 || globalPort + 59 > 18998) {
  throw new Error("UI_E2E_PORT must be an integer from 18000 to 18979");
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
const GLOBAL_STATE = /@global-state/;

function server(gatewayPort: number, name: string) {
  const strictEnginePorts = Array.from({ length: 8 }, (_, index) => String(gatewayPort + 2 + index));
  return {
    command: `${quote(python)} ../scripts/ui/serve.py`,
    env: {
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
  testIgnore: ["**/live-data-*.spec.ts", "**/sweep/**"],
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
  ],
  projects: [
    {
      name: "chromium",
      grepInvert: GLOBAL_STATE,
      use: {
        baseURL: `http://127.0.0.1:${port}`,
        storageState: `${results}/auth-${port}.json`,
      },
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
    {
      name: "chromium-global-state",
      grep: GLOBAL_STATE,
      workers: 1,
      use: {
        baseURL: `http://127.0.0.1:${globalPort}`,
        storageState: `${results}/auth-${globalPort}.json`,
      },
    },
  ],
});
