import path from "node:path";
import { defineConfig, devices } from "@playwright/test";
import baseConfig from "../../../playwright.config";

const here = __dirname;
const webRoot = path.resolve(here, "../../..");

export default defineConfig({
  ...baseConfig,
  testDir: here,
  webServer: {
    ...(Array.isArray(baseConfig.webServer)
      ? baseConfig.webServer[0]
      : baseConfig.webServer)!,
    cwd: webRoot,
  },
  use: {
    ...baseConfig.use,
    storageState: path.join(
      webRoot,
      `test-results/auth-${process.env.UI_E2E_PORT ?? "18180"}.json`,
    ),
  },
  outputDir: path.join(webRoot, "test-results/activity-artifacts"),
  reporter: [
    ["list"],
    [
      "html",
      {
        open: "never",
        outputFolder: path.join(webRoot, "test-results/activity-report"),
      },
    ],
  ],
  testMatch: "**/*.e2e.ts",
  globalSetup: path.join(webRoot, "e2e/global-setup.ts"),
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], browserName: "chromium" },
    },
  ],
});
