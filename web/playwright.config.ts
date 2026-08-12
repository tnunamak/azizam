import { defineConfig } from "@playwright/test";

const channel = process.env.PLAYWRIGHT_BROWSER_CHANNEL;

export default defineConfig({
  testDir: "./tests/playwright",
  globalSetup: "./tests/playwright/global-setup.ts",
  use: {
    baseURL: "http://127.0.0.1:3417",
    headless: true,
  },
  webServer: {
    command: "npm run dev -- --port 3417",
    url: "http://127.0.0.1:3417/ui/admin/status/p1v-fixture/",
    reuseExistingServer: false,
    timeout: 120_000,
    gracefulShutdown: { signal: "SIGTERM", timeout: 5_000 },
  },
  projects: [
    {
      name: "chromium",
      use: {
        browserName: "chromium",
        ...(channel ? { channel } : {}),
      },
    },
  ],
});
