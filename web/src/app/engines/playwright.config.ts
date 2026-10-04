import { defineConfig } from "@playwright/test";
import path from "node:path";
import shared from "../../../playwright.config";

const enginesDir = __dirname;
const webRoot = path.resolve(enginesDir, "../../..");
const servers = shared.webServer
  ? Array.isArray(shared.webServer)
    ? shared.webServer
    : [shared.webServer]
  : [];

export default defineConfig({
  ...shared,
  testDir: enginesDir,
  testMatch: "*.e2e.ts",
  globalSetup: path.join(webRoot, "e2e/global-setup.ts"),
  outputDir: path.join(webRoot, "test-results/engines-artifacts"),
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
