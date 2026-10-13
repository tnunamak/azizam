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
    // Default is one worker per core (23 here). The gate runs vitest beside
    // the web build and the 12-worker Python tier, so 23 jsdom workers starved
    // first renders past Testing Library's 1 s wait. 8 workers: 28-29 s wall
    // and ~225 CPU-s for the full suite, against 22 s and ~290 CPU-s at 23.
    maxWorkers: 8,
    clearMocks: true,
    restoreMocks: true,
    // Undo every vi.stubGlobal before the next test, so a stub cannot outlive its test.
    unstubGlobals: true,
  },
});
