// Run through web/e2e/seed-review.mjs; use only its isolated seeded gateway.
import { createRequire } from "node:module";
import { mkdir, stat, unlink } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(path.resolve(here, "../../../web/package.json"));
const { chromium, expect } = require("@playwright/test");
const baseURL = process.env.APP_URL;
const url = new URL(baseURL);
if (
  url.hostname !== "127.0.0.1" ||
  Number(url.port) < 18000 ||
  Number(url.port) > 18998
) {
  throw new Error("Capture requires an isolated gateway on ports 18000–18998");
}
const browser = await chromium.launch();
const product = path.resolve(here, "../../../web/test-results/product");
try {
  await mkdir(product, { recursive: true });
  for (const theme of ["light", "dark"]) {
    const context = await browser.newContext({
      baseURL,
      storageState: `test-results/auth-${url.port}.json`,
      viewport: { width: 1440, height: 1000 },
      deviceScaleFactor: 1,
      colorScheme: theme,
      reducedMotion: "reduce",
    });
    await context.addInitScript(
      (value) => localStorage.setItem("theme", value),
      theme,
    );
    const page = await context.newPage();
    await mkdir(here, { recursive: true });
    for (const route of [
      "home",
      "models",
      "workflows",
      "try",
      "providers",
      "library",
      "status",
      "people",
    ]) {
      await page.setViewportSize({ width: 1440, height: 960 });
      const response = await page.goto(route === "home" ? "/" : `/${route}/`);
      expect(response.status()).toBe(200);
      await page.waitForLoadState("networkidle");
      await page.locator("main").waitFor();
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
      await page.evaluate(() => document.fonts.ready);
      if (
        await page.getByText("Something went wrong", { exact: true }).count()
      ) {
        throw new Error(`${route} shows an error`);
      }
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      const outputs = [
        { directory: product, name: `${route}-${theme}`, height: 960 },
      ];
      if (theme === "light" && ["home", "models", "people"].includes(route)) {
        outputs.push({
          directory: here,
          name: `${route}-light-1440`,
          height: 1000,
        });
      }
      for (const output of outputs) {
        await page.setViewportSize({ width: 1440, height: output.height });
        const png = path.join(output.directory, `${output.name}.png`);
        const webp = path.join(output.directory, `${output.name}.webp`);
        await page.screenshot({ path: png, animations: "disabled" });
        execFileSync(path.resolve(here, "../../../venv/bin/python"), [
          "-c",
          'from PIL import Image; import sys; Image.open(sys.argv[1]).save(sys.argv[2], "WEBP", quality=88, method=6)',
          png,
          webp,
        ]);
        await unlink(png);
        const bytes = (await stat(webp)).size;
        expect(bytes, `${output.name} must stay under 300 KB`).toBeLessThan(
          300_000,
        );
        console.log(
          `Captured ${output.name}: 1440 × ${output.height}, ${bytes} bytes`,
        );
      }
    }
    await context.close();
  }
} finally {
  await browser.close();
}
