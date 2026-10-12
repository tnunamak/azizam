import { defineConfig } from "vitest/config";
import rootConfig from "../../../vitest.config";

export default defineConfig({
  ...rootConfig,
  test: {
    ...rootConfig.test,
    include: ["src/app/connect/**/*.spec.tsx"],
  },
});
