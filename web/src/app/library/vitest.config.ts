import path from "node:path";
import { defineConfig, mergeConfig } from "vitest/config";
import shared from "../../../vitest.config";

export default mergeConfig(
  shared,
  defineConfig({
    test: { include: [path.resolve(__dirname, "*.spec.tsx")] },
  }),
);
