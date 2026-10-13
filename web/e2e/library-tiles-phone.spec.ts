import { type Page } from "@playwright/test";
import { expect, test } from "./fixtures/isolation";
import { fulfilJson } from "./fixtures/intercept";

// WP-7: on a phone, a running creation says what it is, a quiet audio tile
// carries no invented decoration, a long household name is not clipped, and an
// unavailable file fits its square tile.
const longName = "Bartholomew Featherstonehaugh-Smythe";
const png = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 8 8'%3E%3Crect width='8' height='8' fill='%23ccd'/%3E%3C/svg%3E";

const assets = [
  { asset_id: "a_img", kind: "image", prompt: "Tomatoes on a bench", model: "image", created_at: 1_760_000_000, download_url: png, owner_name: longName },
  { asset_id: "a_aud", kind: "audio", prompt: "A calm spoken introduction for the gateway", model: "kokoro-tts", created_at: 1_760_020_000, duration_s: 12, download_url: "/me/api/media/a_aud/download", owner_name: "Sam" },
  { asset_id: "a_gone", kind: "image", prompt: "Lost picture", model: "image", created_at: 1_750_000_000, available: false, unavailable_reason: "expired", owner_name: "Sam" },
];

async function open(page: Page, width: 390 | 1440) {
  await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
  await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
  await page.addInitScript(() => localStorage.setItem("theme", "light"));
  await page.route("**/me/api/media?**", (route) => fulfilJson(route, { assets, models: ["image"], next_cursor: null }));
  await page.route("**/me/api/home", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await fulfilJson(route, {
        ...body,
        receipts: [
          { request_id: "run_pic", status: "running", intent: "image.generate", accepted_at_ms: Date.now() - 90_000 },
          { request_id: "run_vid", status: "queued", path: "/openai/v1/videos" },
        ],
      }, { response });
  });
  await page.goto("/library/");
  await expect(page.getByRole("button", { name: "Open Tomatoes on a bench" })).toBeVisible();
}

test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

for (const width of [390, 1440] as const) {
  test(`running creations keep their words at ${width}`, async ({ page }) => {
    await open(page, width);
    for (const words of ["Making your picture", "Making your video"]) {
      const label = page.getByText(words, { exact: true });
      await expect(label).toBeVisible();
      const box = (await label.boundingBox())!;
      expect(box.width).toBeGreaterThan(20);
      const tile = page.locator("div.aspect-square", { has: label }).first();
      const fits = await tile.evaluate((node) => node.scrollHeight <= node.clientHeight + 1 && node.scrollWidth <= node.clientWidth + 1);
      expect(fits, `${words} fits its tile`).toBe(true);
    }
    await page.screenshot({ path: test.info().outputPath(`library-running-${width}.png`), fullPage: false });
  });

  test(`maker key and unavailable tile are not clipped at ${width}`, async ({ page }) => {
    await open(page, width);
    await page.getByText("Show them").click();
    const key = page.getByRole("group", { name: "Creation makers" });
    await expect(key).toBeVisible();
    await expect(key.getByText(longName)).toBeVisible();
    const clipped = await key.evaluate((node) => node.scrollWidth > node.clientWidth + 1);
    expect(clipped, "maker key scrolls sideways").toBe(false);
    const gone = page.locator("article", { has: page.getByRole("img", { name: /preview unavailable/ }) }).first();
    await expect(gone).toBeVisible();
    const fits = await gone.locator("div.overflow-hidden").first().evaluate((node) => {
      const inner = node.firstElementChild as HTMLElement;
      return inner.scrollHeight <= node.clientHeight + 1;
    });
    expect(fits, "unavailable preview fits its tile").toBe(true);
    await page.screenshot({ path: test.info().outputPath(`library-grid-${width}.png`), fullPage: false });
  });
}

test("a quiet audio tile shows play and length, no prompt text and no invented bars", async ({ page }) => {
  await open(page, 390);
  const tile = page.getByRole("button", { name: /^Play / }).locator("xpath=ancestor::article");
  await expect(tile).toBeVisible();
  await expect(tile.getByText("A calm spoken introduction for the gateway")).toHaveCount(0);
  await expect(tile.locator("svg rect")).toHaveCount(0);
  await expect(tile.getByRole("button", { name: /^Play / })).toContainText("0:12");
});

for (const width of [390, 1440] as const) {
  test(`Home running tile keeps "may not finish" visible at ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
    await page.addInitScript(() => localStorage.setItem("theme", "light"));
    await page.route("**/me/api/media?**", (route) => fulfilJson(route, { assets: assets.slice(0, 2), models: [], next_cursor: null }));
    await page.route("**/me/api/home", async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      await fulfilJson(route, {
          ...body,
          capabilities: (body.capabilities ?? []).map((item: { capability: string }) =>
            item.capability === "image" ? { ...item, available: false, status: "unavailable", reason: "Images are paused." } : item,
          ),
          receipts: [{ request_id: "run_pic", status: "running", intent: "image.generate", accepted_at_ms: Date.now() - 90_000 }],
        }, { response });
    });
    await page.goto("/");
    const tile = page.getByText("Making your picture", { exact: true });
    await expect(tile).toBeVisible();
    const card = page.locator("li", { has: tile });
    const warning = card.locator("p.text-warning");
    await expect(warning).toBeVisible();
    await expect(warning).toContainText(/may not finish/i);
    const fits = await card.locator("div.aspect-square").evaluate((node) => node.scrollHeight <= node.clientHeight + 1 && node.scrollWidth <= node.clientWidth + 1);
    expect(fits, "running tile content fits").toBe(true);
    await page.getByRole("heading", { name: "Recent" }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: test.info().outputPath(`home-running-risk-${width}.png`) });
  });
}
