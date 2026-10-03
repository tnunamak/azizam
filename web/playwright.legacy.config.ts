import { defineConfig } from "@playwright/test";

const channel = process.env.PLAYWRIGHT_BROWSER_CHANNEL;
const port = Number(process.env.PLAYWRIGHT_PORT ?? "18182");

export default defineConfig({
  testDir: "./tests/playwright",
  globalSetup: "./tests/playwright/global-setup.ts",
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    headless: true,
  },
  webServer: {
    command: `npm run dev -- --port ${port}`,
    url: `http://127.0.0.1:${port}/ui/admin/status/p1v-fixture/`,
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
