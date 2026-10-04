import path from "node:path";
import { defineConfig } from "@playwright/test";
import shared from "../../../playwright.config";

const web = path.resolve(__dirname, "../../..");
const port = Number(process.env.UI_E2E_PORT ?? "18180");
const server = shared.webServer;
if (!server || Array.isArray(server))
  throw new Error("Models tests require the shared gateway server");
export default defineConfig({
  ...shared,
  testDir: __dirname,
  testMatch: "catalog.e2e.ts",
  globalSetup: path.join(web, "e2e/global-setup.ts"),
  outputDir: path.join(web, "test-results/models-audit"),
  reporter: [["list"]],
  use: {
    ...shared.use,
    storageState: path.join(web, `test-results/auth-${port}.json`),
  },
  webServer: {
    ...server,
    cwd: web,
  },
});
