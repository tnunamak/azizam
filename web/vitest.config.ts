import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

const root = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  esbuild: {
    jsx: "automatic",
  },
  resolve: {
    alias: {
      "@": path.join(root, "src"),
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./tests/vitest.setup.ts"],
    // src/** holds per-page unit specs too; the *.e2e.* and *.playwright.* files there are Playwright.
    include: [
      "tests/**/*.spec.ts",
      "tests/**/*.spec.tsx",
      "src/**/*.spec.ts",
      "src/**/*.spec.tsx",
    ],
    exclude: ["tests/playwright/**", "src/**/*.e2e.*", "src/**/*.playwright.*", "node_modules/**"],
    // A first failure stays a failure (TEST_STRATEGY.md 3.1).
    retry: 0,
    clearMocks: true,
    restoreMocks: true,
  },
});
