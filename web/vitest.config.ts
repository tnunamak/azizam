import "./test-priority"; // first: nice 19 + idle I/O for the runner and everything it starts
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
    include: [
      "tests/**/*.spec.ts",
      "tests/**/*.spec.tsx",
      "src/**/*.spec.ts",
      "src/**/*.spec.tsx",
    ],
    exclude: ["tests/playwright/**", "node_modules/**"],
    // A first failure stays a failure (TEST_STRATEGY.md 3.1).
    retry: 0,
    clearMocks: true,
    restoreMocks: true,
    // Undo every vi.stubGlobal before the next test, so a stub cannot outlive its test.
    unstubGlobals: true,
  },
});
