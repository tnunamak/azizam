import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

const shellDir = path.dirname(fileURLToPath(import.meta.url));
const webRoot = path.resolve(shellDir, "../../..");

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
    include: [
      path.join(shellDir, "account-avatar.spec.tsx"),
      path.join(shellDir, "app-shell.regression.spec.tsx"),
      path.join(shellDir, "owner-alert.spec.ts"),
    ],
    setupFiles: [path.join(webRoot, "tests/vitest.setup.ts")],
    clearMocks: true,
    restoreMocks: true,
  },
});
