import { type Page } from "@playwright/test";
import { expect, test } from "./fixtures/isolation";
import { fulfilJson } from "./fixtures/intercept";

// The viewer's full screen in a real browser: the Fullscreen API on the picture, and, where the browser has no
// such API (phones, for a picture), the stage covering the window. Escape leaves either without closing the viewer.

const picture =
  "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 640 420'%3E%3Crect width='640' height='420' fill='%23e0f2fe'/%3E%3C/svg%3E";

const mediaFixture = {
  assets: [
    {
      asset_id: "asset_fullscreen_1",
      kind: "image",
      prompt: "A picture to fill the screen",
      model: "image",
      created_at: 1_760_000_000,
      download_url: picture,
      width: 640,
      height: 420,
    },
  ],
  models: ["image"],
  next_cursor: null,
};

async function openPicture(page: Page, width: 1440 | 390) {
  await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
  await page.route("**/me/api/media?**", (route) => fulfilJson(route, mediaFixture));
  await page.route("**/me/api/media/asset_fullscreen_1/download?*", (route) =>
    fulfilJson(route, { download_url: picture }),
  );
  await page.goto("/library/");
  await page.getByRole("button", { name: /Open A picture to fill the screen/ }).click();
  return page.getByRole("dialog");
}

for (const width of [1440, 390] as const) {
  test(`the picture goes full screen through the browser at ${width}px, and a native exit keeps the viewer open`, async ({
    page,
  }) => {
    const dialog = await openPicture(page, width);

    await dialog.getByRole("button", { name: "Full screen", exact: true }).click();

    await expect
      .poll(() => page.evaluate(() => document.fullscreenElement?.tagName ?? null))
      .toBe("IMG");
    await page.evaluate(() => document.exitFullscreen());
    await expect(dialog.getByRole("button", { name: "Full screen", exact: true })).toBeVisible();
    await expect(dialog).toBeVisible();
  });

  test(`where the browser has no full screen, the stage fills the window at ${width}px, and Escape leaves it`, async ({
    page,
  }) => {
    await page.addInitScript(() => {
      Object.defineProperty(document, "fullscreenEnabled", { get: () => false });
    });
    const dialog = await openPicture(page, width);
    const stage = dialog.locator("[data-fullscreen]");

    await dialog.getByRole("button", { name: "Full screen", exact: true }).click();

    await expect(stage).toHaveAttribute("data-fullscreen", "fallback");
    const box = await stage.boundingBox();
    expect(box).toEqual(expect.objectContaining({ x: 0, y: 0, width, height: width === 390 ? 844 : 1000 }));
    await page.keyboard.press("Escape");
    await expect(stage).toHaveAttribute("data-fullscreen", "off");
    await expect(dialog).toBeVisible();
  });
}
