import { defineConfig } from "@playwright/test";
import path from "node:path";
import shared from "../../../playwright.config";

const webRoot = path.resolve(__dirname, "../../..");
const servers = shared.webServer
  ? Array.isArray(shared.webServer)
    ? shared.webServer
    : [shared.webServer]
  : [];

// Keep tests in this lane's directory and reuse the portable gateway harness.
export default defineConfig({
  ...shared,
  testDir: __dirname,
  testMatch: /.*\.e2e\.spec\.ts/,
  globalSetup: path.join(webRoot, "e2e/global-setup.ts"),
  outputDir: path.join(webRoot, "test-results/library-artifacts"),
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
