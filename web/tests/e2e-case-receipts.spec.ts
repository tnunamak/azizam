import {
  existsSync,
  mkdtempSync,
  readFileSync,
  rmdirSync,
  unlinkSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import type { FullConfig } from "@playwright/test/reporter";
import { expect, it } from "vitest";

import CaseReceipts from "../e2e/case-receipts";

it("records configured projects before any case produces a receipt", () => {
  const directory = mkdtempSync(path.join(tmpdir(), "playwright-projects-"));
  const output = path.join(directory, "projects.json");
  const previousOutput = process.env.PLAYWRIGHT_PROJECTS_FILE;

  try {
    process.env.PLAYWRIGHT_PROJECTS_FILE = output;
    const config = {
      configFile: path.join(directory, "playwright.config.ts"),
      projects: [{ name: "chromium" }, { name: "chromium-pool-1" }],
    } as unknown as FullConfig;

    new CaseReceipts().onBegin(config);

    expect(JSON.parse(readFileSync(output, "utf8"))).toEqual([
      "chromium",
      "chromium-pool-1",
    ]);
  } finally {
    if (previousOutput === undefined) delete process.env.PLAYWRIGHT_PROJECTS_FILE;
    else process.env.PLAYWRIGHT_PROJECTS_FILE = previousOutput;
    if (existsSync(output)) unlinkSync(output);
    rmdirSync(directory);
  }
});
