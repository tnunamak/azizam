import { defineConfig } from "@playwright/test";

const channel = process.env.PLAYWRIGHT_BROWSER_CHANNEL;

export default defineConfig({
  testDir: "./tests/playwright",
  globalSetup: "./tests/playwright/global-setup.ts",
  use: {
    headless: true,
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
