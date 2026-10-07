import { defineConfig } from "vitest/config";
import base from "../../../vitest.config";

export default defineConfig({
  ...base,
  test: {
    ...base.test,
    include: [
      "src/app/status/**/*.spec.tsx",
      "src/app/engines/**/*.spec.tsx",
      "src/app/activity/**/*.spec.tsx",
      "src/components/shell/owner-alert.spec.ts",
    ],
  },
});
