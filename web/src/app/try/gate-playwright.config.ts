import { defineConfig } from "@playwright/test";
import path from "node:path";
import base from "../../../playwright.config";
const root = path.resolve(__dirname, "../../..");
export default defineConfig({
  ...base,
  reporter: [
    ["list"],
    [
      "html",
      {
        open: "never",
        outputFolder: path.join(root, "test-results/gate-owner-report"),
      },
    ],
  ],
  testDir: path.join(root, "src/app"),
  testMatch: ["providers/gate-owner.e2e.ts", "try/gate-owner.e2e.ts"],
  globalSetup: path.join(root, "e2e/global-setup.ts"),
  outputDir: path.join(root, "test-results/gate-owner-artifacts"),
  use: {
    ...base.use,
    storageState: path.join(
      root,
      `test-results/auth-${process.env.UI_E2E_PORT ?? "18180"}.json`,
    ),
  },
  webServer: { ...(base.webServer as { command: string }), cwd: root },
});
