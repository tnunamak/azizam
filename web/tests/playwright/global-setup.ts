import { access } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import type { FullConfig } from "@playwright/test";

export default async function globalSetup(config: FullConfig) {
  if (!config.configFile) {
    throw new Error("Playwright must load its configuration file.");
  }

  const fixture = path.join(
    path.dirname(config.configFile),
    "tests/playwright/static-smoke.html",
  );

  await access(fixture);
  process.env.VF_UI_STATIC_SMOKE_URL = pathToFileURL(fixture).href;
}
