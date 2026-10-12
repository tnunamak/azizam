import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { mkdirSync } from "node:fs";
import path from "node:path";

const REVIEW_DIR = "/home/tnunamak/.tmp/gw-product/ui-review/gw-copy-c";
mkdirSync(REVIEW_DIR, { recursive: true });

const asset = {
  asset_id: "asset_image_copy",
  request_id: "req_image_copy",
  kind: "image",
  prompt: "A teal desk lamp on a walnut table",
  model: "image",
  created_at: 1_760_000_000,
  download_url:
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 96 72'%3E%3Crect width='96' height='72' fill='%230f766e'/%3E%3Ccircle cx='42' cy='28' r='10' fill='white'/%3E%3C/svg%3E",
  width: 96,
  height: 72,
  workflow: "workflow.image.lamp",
};

async function setViewportAndTheme(
  page: Page,
  mode: "light" | "dark",
  width: 1440 | 390,
) {
  await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
  await page.emulateMedia({ colorScheme: mode, reducedMotion: "reduce" });
  await page.addInitScript(
    (value) => localStorage.setItem("theme", value),
    mode,
  );
}

async function auditPage(page: Page) {
  const violations = (await new AxeBuilder({ page }).analyze()).violations
    .filter((violation) =>
      ["serious", "critical"].includes(violation.impact ?? ""),
    )
    .map((violation) => violation.id);
  expect(violations).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
}

async function routeLibrary(page: Page, status: "seeded" | "empty" | "error") {
  await page.route("**/me/api/media**", (route) => {
    if (status === "error") {
      return route.fulfill({
        status: 500,
        json: { error: { message: "Library unavailable" } },
      });
    }
    const url = new URL(route.request().url());
    const filtered = Boolean(url.searchParams.get("kind"));
    return route.fulfill({
      json: {
        assets: status === "empty" || filtered ? [] : [asset],
        models: ["image"],
        next_cursor: null,
      },
    });
  });
}

for (const mode of ["light", "dark"] as const) {
  for (const width of [1440, 390] as const) {
    test(`library seeded ${mode} ${width}`, async ({ page }) => {
      await setViewportAndTheme(page, mode, width);
      await routeLibrary(page, "seeded");

      await page.goto("/library/");
      await expect(
        page.getByRole("heading", { name: "Library" }),
      ).toBeVisible();
      if (width === 390) {
        await page.getByText("Filter", { exact: true }).click();
        await expect(
          page.getByRole("button", { name: "Filter kind Images" }),
        ).toBeVisible();
      }
      await page.getByRole("button", { name: `Open ${asset.prompt}` }).click();
      const dialog = page.getByRole("dialog", { name: asset.prompt });
      await expect(
        dialog.getByRole("heading", { name: asset.prompt }),
      ).toBeVisible();
      await dialog.getByText("Details").click();
      await expect(dialog.getByText("workflow.image.lamp")).toBeVisible();
      await auditPage(page);
      await page.screenshot({
        path: `${REVIEW_DIR}/library-seeded-${mode}-${width}.png`,
      });
    });

    test(`library filtered empty ${mode} ${width}`, async ({ page }) => {
      await setViewportAndTheme(page, mode, width);
      await routeLibrary(page, "seeded");

      await page.goto("/library/");
      if (width === 390) {
        await page.getByText("Filter", { exact: true }).click();
        await page.getByRole("button", { name: "Filter kind Video" }).click();
      } else {
        await page.getByRole("button", { name: "Video" }).click();
      }
      await expect(page.getByText("No matching media")).toBeVisible();
      await expect(
        page.getByRole("button", { name: "Clear filters" }).first(),
      ).toBeVisible();
      await auditPage(page);
      await page.screenshot({
        path: `${REVIEW_DIR}/library-filtered-empty-${mode}-${width}.png`,
      });
    });

    test(`library empty ${mode} ${width}`, async ({ page }) => {
      await setViewportAndTheme(page, mode, width);
      await routeLibrary(page, "empty");

      await page.goto("/library/");
      await expect(page.getByText("No media yet")).toBeVisible();
      await expect(
        page.getByRole("link", { name: "Make something" }),
      ).toBeVisible();
      await auditPage(page);
      await page.screenshot({
        path: `${REVIEW_DIR}/library-empty-${mode}-${width}.png`,
      });
    });

    test(`library error ${mode} ${width}`, async ({ page }) => {
      await setViewportAndTheme(page, mode, width);
      await routeLibrary(page, "error");

      await page.goto("/library/");
      await expect(
        page.getByText("Your library could not be loaded. Try again."),
      ).toBeVisible();
      await expect(
        page.getByRole("button", { name: /try again/i }),
      ).toBeVisible();
      await auditPage(page);
      await page.screenshot({
        path: `${REVIEW_DIR}/library-error-${mode}-${width}.png`,
      });
    });

    test(`library loading ${mode} ${width}`, async ({ page }) => {
      await setViewportAndTheme(page, mode, width);
      await page.route("**/me/api/media?**", () => undefined);

      await page.goto("/library/");
      await expect(
        page.getByRole("status", { name: "Loading library" }),
      ).toBeVisible();
      await auditPage(page);
      await page.screenshot({
        path: `${REVIEW_DIR}/library-loading-${mode}-${width}.png`,
      });
    });
  }
}

test("library delete cancel retains filters and successful delete clears the asset link", async ({
  page,
}) => {
  await setViewportAndTheme(page, "light", 1440);
  let deleted = false;
  await page.route("**/me/api/session", (route) =>
    route.fulfill({ json: { csrf_token: "csrf-delete" } }),
  );
  await page.route("**/me/api/media**", (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "DELETE") {
      deleted = true;
      return route.fulfill({ json: { ok: true } });
    }
    if (url.pathname.endsWith(`/${asset.asset_id}`)) {
      return route.fulfill({ json: { asset } });
    }
    return route.fulfill({
      json: {
        assets: deleted ? [] : [asset],
        models: ["image"],
        next_cursor: null,
      },
    });
  });

  await page.goto("/library/");
  await page.getByRole("combobox", { name: "Owner" }).click();
  await page.getByRole("option", { name: "Mine" }).click();
  await page.getByRole("button", { name: "Images" }).click();
  await page.getByRole("button", { name: `Open ${asset.prompt}` }).click();
  await expect(page).toHaveURL(/asset=asset_image_copy/);
  const dialog = page.getByRole("dialog", { name: asset.prompt });
  await dialog
    .getByRole("button", { name: `Actions for ${asset.prompt}` })
    .click();
  await page.getByRole("menuitem", { name: "Delete" }).click();
  await expect(
    page.getByRole("dialog", { name: `Delete ${asset.prompt}?` }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toBeVisible();

  await dialog
    .getByRole("button", { name: `Actions for ${asset.prompt}` })
    .click();
  await page.getByRole("menuitem", { name: "Delete" }).click();
  await page.getByRole("button", { name: "Delete" }).click();
  await expect(dialog).toBeHidden();
  await expect(page).not.toHaveURL(/asset=asset_image_copy/);
  await expect(page.getByText("No matching media")).toBeVisible();
});

test("library missing asset deep link keeps a retryable target", async ({
  page,
}) => {
  await setViewportAndTheme(page, "light", 1440);
  let detailRequests = 0;
  await page.route("**/me/api/media**", (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/asset_recovered")) {
      detailRequests += 1;
      if (detailRequests === 1) {
        return route.fulfill({
          status: 404,
          json: { error: { message: "Asset not found for this account" } },
        });
      }
      return route.fulfill({
        json: {
          asset: {
            ...asset,
            asset_id: "asset_recovered",
            prompt: "Recovered library item",
          },
        },
      });
    }
    return route.fulfill({
      json: { assets: [], models: ["image"], next_cursor: null },
    });
  });

  await page.goto("/library/?asset=asset_recovered");
  await expect(
    page.getByText(
      "The gateway couldn’t open this creation. Try again or return to your library.",
    ),
  ).toBeVisible();
  await expect(page).toHaveURL(/asset=asset_recovered/);
  await page.getByRole("button", { name: /try again/i }).click();
  await expect(
    page.getByRole("dialog", { name: "Recovered library item" }),
  ).toBeVisible();
  await expect(page).toHaveURL(/asset=asset_recovered/);
});

for (const width of [1440, 390] as const) {
  test(`library video without a poster shows a playable frame ${width}`, async ({
    page,
  }) => {
    await setViewportAndTheme(page, width === 390 ? "dark" : "light", width);
    await page.route("**/me/api/media?**", (route) =>
      route.fulfill({
        json: {
          assets: [
            {
              ...asset,
              kind: "video",
              prompt: "A teal launch clip",
              download_url: "/fixture-preview.mp4",
              poster_asset_id: null,
            },
          ],
          models: ["image"],
          next_cursor: null,
        },
      }),
    );
    await page.route("**/fixture-preview.mp4", (route) =>
      route.fulfill({
        path: path.join(__dirname, "preview-test.mp4"),
        contentType: "video/mp4",
      }),
    );
    await page.goto("/library/");
    const preview = page.locator("article video");
    await expect(preview).toBeVisible();
    await expect
      .poll(() =>
        preview.evaluate(
          (element: HTMLVideoElement) =>
            element.readyState >= 2 && element.videoWidth > 0,
        ),
      )
      .toBe(true);
    await expect(preview).not.toHaveAttribute("controls");
    const pixel = await preview.evaluate((element: HTMLVideoElement) => {
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = 1;
      const context = canvas.getContext("2d")!;
      context.drawImage(element, 0, 0, 1, 1);
      return Array.from(context.getImageData(0, 0, 1, 1).data);
    });
    expect(pixel[1]).toBeGreaterThan(pixel[0]);
    expect(pixel[2]).toBeGreaterThan(pixel[0]);
    await auditPage(page);
    await page.screenshot({
      path: `${REVIEW_DIR}/library-video-frame-${width}.png`,
    });
    await page.getByRole("button", { name: "Open A teal launch clip" }).click();
    await expect(
      page.getByRole("dialog").locator("video[controls]"),
    ).toBeVisible();
  });

  test(`library failed image preview offers clickable recovery ${width}`, async ({
    page,
  }) => {
    await setViewportAndTheme(page, "light", width);
    let requests = 0;
    await page.route("**/me/api/media?**", (route) => {
      requests += 1;
      return route.fulfill({
        json: {
          assets: [{ ...asset, download_url: "/broken-preview.png" }],
          models: ["image"],
          next_cursor: null,
        },
      });
    });
    await page.route("**/broken-preview.png", (route) =>
      route.fulfill({ status: 503, body: "Unavailable" }),
    );
    await page.goto("/library/");
    await expect(
      page.getByRole("img", { name: "Image preview unavailable" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Retry" }).click();
    await expect.poll(() => requests).toBeGreaterThan(1);
    await expect(
      page.getByRole("img", { name: "Image preview unavailable" }),
    ).toBeVisible();
    await expect(page.getByRole("dialog")).toHaveCount(0);
  });

  test(`library missing media refresh is clickable ${width}`, async ({
    page,
  }) => {
    await setViewportAndTheme(page, "light", width);
    let requests = 0;
    await page.route("**/me/api/media?**", (route) => {
      requests += 1;
      return route.fulfill({
        json: {
          assets: [{ ...asset, available: false }],
          models: ["image"],
          next_cursor: null,
        },
      });
    });
    await page.goto("/library/");
    await expect(
      page.getByRole("button", { name: "Refresh library" }),
    ).toBeVisible();
    await page.screenshot({
      path: `${REVIEW_DIR}/library-missing-media-light-${width}.png`,
    });
    await page.getByRole("button", { name: "Refresh library" }).click();
    await expect.poll(() => requests).toBeGreaterThan(1);
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page).not.toHaveURL(/asset=/);
  });
}
