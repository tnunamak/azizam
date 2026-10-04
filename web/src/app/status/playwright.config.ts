import { defineConfig } from "@playwright/test";
import path from "node:path";
import shared from "../../../playwright.config";

const statusDir = __dirname;
const webRoot = path.resolve(statusDir, "../../..");
const servers = shared.webServer
  ? Array.isArray(shared.webServer)
    ? shared.webServer
    : [shared.webServer]
  : [];

export default defineConfig({
  ...shared,
  testDir: statusDir,
  testMatch:
    process.env.STATUS_REAL_DATA === "1"
      ? "real-data-status.e2e.ts"
      : "member-status.e2e.ts",
  globalSetup: path.join(webRoot, "e2e/global-setup.ts"),
  outputDir: path.join(webRoot, "test-results/status-artifacts"),
  reporter: [["list"]],
  use: {
    ...shared.use,
    storageState: path.join(
      webRoot,
      `test-results/auth-${process.env.UI_E2E_PORT ?? "18180"}.json`,
    ),
  },
  webServer: servers.map((server) => ({ ...server, cwd: webRoot })),
});
