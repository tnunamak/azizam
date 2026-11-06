import path from "node:path";
import { defineConfig } from "vitest/config";

export default defineConfig({
  esbuild: { jsx: "automatic" },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "../.."),
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: [path.resolve(__dirname, "../../../tests/vitest.setup.ts")],
    include: [path.resolve(__dirname, "*.spec.tsx")],
  },
});
