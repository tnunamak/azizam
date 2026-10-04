import { createRequire } from "node:module";
import { readFile, mkdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
const root = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../../../..",
);
const require = createRequire(path.join(root, "web/package.json"));
const { chromium, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;
const sessions = JSON.parse(
  await readFile(
    path.join(
      root,
      ".ui-e2e-state/gw-copy-shared-real/runtime/session-cookies.json",
    ),
    "utf8",
  ),
);
const output = "/home/tnunamak/.tmp/gw-product/ui-review/gw-copy-shared";
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
try {
  const context = await browser.newContext({
    baseURL: "http://127.0.0.1:18420",
  });
  await context.addCookies([sessions.owner]);
  const usageResponse = await context.request.get(
    "/me/api/usage/summary?source=local-network",
  );
  expect(usageResponse.status()).toBe(200);
  const usage = await usageResponse.json();
  expect(usage.requests).toBeGreaterThan(0);
  expect(usage.receipts.length).toBeGreaterThan(0);
  expect(
    usage.receipts.some((row) => row.source?.kind === "local-network"),
  ).toBe(true);
  console.log(
    `Copied-data local-network usage: ${usage.requests} requests, ${usage.receipts.length} receipts returned`,
  );
  const page = await context.newPage();
  for (const theme of ["light", "dark"]) {
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      await page.addInitScript(
        (value) => localStorage.setItem("theme", value),
        theme,
      );
      await page.goto("/ui/usage/");
      await expect(
        page.getByRole("heading", { level: 1, name: "Usage", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByText("Local network · no key").first(),
      ).toBeVisible();
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      const violations = (
        await new AxeBuilder({ page }).analyze()
      ).violations.filter((row) =>
        ["serious", "critical"].includes(row.impact),
      );
      expect(violations.map((row) => row.id)).toEqual([]);
      await page.screenshot({
        path: `${output}/real-copy-usage-${theme}-${width}.png`,
        fullPage: true,
      });
      console.log(
        `Copied-data shared shell: ${theme}, ${width}px, visible local-network items, no overflow or serious axe violations`,
      );
    }
  }
  await context.close();
} finally {
  await browser.close();
}
