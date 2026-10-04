import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

const webRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../../..",
);

export default defineConfig({
  esbuild: {
    jsx: "automatic",
  },
  resolve: {
    alias: {
      "@": path.join(webRoot, "src"),
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: [path.join(webRoot, "tests/vitest.setup.ts")],
    include: [
      "src/app/home/audit-tests/**/*.spec.ts",
      "src/app/home/audit-tests/**/*.spec.tsx",
    ],
    clearMocks: true,
    restoreMocks: true,
  },
});
