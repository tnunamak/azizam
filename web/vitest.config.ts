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
    clearMocks: true,
    restoreMocks: true,
  },
});
