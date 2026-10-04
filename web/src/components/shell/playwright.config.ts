import path from "node:path";
import { defineConfig } from "@playwright/test";
import rootConfig from "../../../playwright.config";

const shellDir = __dirname;
const webRoot = path.resolve(shellDir, "../../..");
const webServer = rootConfig.webServer;
if (!webServer || Array.isArray(webServer)) {
  throw new Error("Shell tests require the shared isolated gateway server");
}

export default defineConfig({
  ...rootConfig,
  testDir: shellDir,
  testMatch: "app-shell.playwright.spec.ts",
  webServer: {
    ...webServer,
    cwd: webRoot,
  },
  globalSetup: path.join(webRoot, "e2e/global-setup.ts"),
  outputDir: path.join(webRoot, "test-results/shell-artifacts"),
  reporter: [
    ["list"],
    [
      "html",
      {
        open: "never",
        outputFolder: path.join(webRoot, "test-results/shell-report"),
      },
    ],
  ],
  use: {
    ...rootConfig.use,
    storageState: path.join(
      webRoot,
      `test-results/auth-${process.env.UI_E2E_PORT ?? "18180"}.json`,
    ),
  },
});
