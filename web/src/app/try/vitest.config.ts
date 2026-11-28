import base from "../../../vitest.config";
import { defineConfig } from "vitest/config";
export default defineConfig({
  ...base,
  test: { ...base.test, include: ["src/app/try/*.spec.tsx"] },
});
