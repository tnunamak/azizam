import base from "../../../vitest.config";
import { defineConfig } from "vitest/config";

export default defineConfig({
  ...base,
  test: {
    ...base.test,
    include: [
      "src/app/try/gate-owner.spec.tsx",
      "src/app/providers/gate-owner.spec.tsx",
    ],
  },
});
