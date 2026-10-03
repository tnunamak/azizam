import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const mediaFixture = {
  assets: [
    {
      asset_id: "asset_image_1",
      kind: "image",
      prompt: "A clean product render of a teal fish gateway on a desk",
      model: "image",
      created_at: 1_760_000_000,
      download_url:
        "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 640 420'%3E%3Crect width='640' height='420' fill='%23e0f2fe'/%3E%3Ccircle cx='300' cy='210' r='110' fill='%230f766e'/%3E%3Cpath d='M365 210l120-80v160z' fill='%2314b8a6'/%3E%3Ccircle cx='265' cy='178' r='16' fill='white'/%3E%3C/svg%3E",
      workflow_download_url:
        "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 640 420'%3E%3Crect width='640' height='420' fill='%23e0f2fe'/%3E%3Ccircle cx='300' cy='210' r='110' fill='%230f766e'/%3E%3Cpath d='M365 210l120-80v160z' fill='%2314b8a6'/%3E%3Ccircle cx='265' cy='178' r='16' fill='white'/%3E%3C/svg%3E",
      workflow: { sampler: "local-test" },
      seed: 42,
      width: 1024,
      height: 768,
      generation_ms: 2500,
    },
    {
      asset_id: "asset_video_1",
      kind: "video",
      prompt: "Gateway status lights moving across a quiet panel",
      model: "video",
      created_at: 1_760_010_000,
      duration_s: 6,
      download_url: "/me/api/media/asset_video_1/download",
    },
    {
      asset_id: "asset_audio_1",
      kind: "audio",
      prompt: "A calm spoken introduction for the gateway",
      model: "voice",
      created_at: 1_760_020_000,
      duration_s: 12,
      download_url: "/me/api/media/asset_audio_1/download",
    },
  ],
  models: ["image", "video", "voice"],
  next_cursor: "cursor_next",
};

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

async function setTheme(page: Page, mode: "light" | "dark", width: 1440 | 390) {
  await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
  await page.emulateMedia({ colorScheme: mode, reducedMotion: "reduce" });
  await page.addInitScript(
    (value) => localStorage.setItem("theme", value),
    mode,
  );
}

test("library loading stays accessible while media is pending", async ({
  page,
}) => {
  await setTheme(page, "light", 390);
  let release: () => void = () => {};
  const ready = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/me/api/media?**", async (route) => {
    await ready;
    await route.fulfill({ json: mediaFixture });
  });
  await page.goto("/ui/library/", { waitUntil: "domcontentloaded" });
  await expect(
    page.getByRole("status", { name: "Loading library" }),
  ).toBeVisible();
  await auditPage(page);
  release();
  await expect(
    page.getByRole("button", { name: /Open A clean product render/ }),
  ).toBeVisible();
});

for (const mode of ["light", "dark"] as const) {
  for (const width of [1440, 390] as const) {
    test(`library seeded ${mode} ${width}`, async ({ page }) => {
      await setTheme(page, mode, width);
      const mediaRequests: string[] = [];
      await page.route("**/me/api/media?**", (route) => {
        mediaRequests.push(route.request().url());
        return route.fulfill({ json: mediaFixture });
      });
      await page.goto("/ui/library/");
      await expect(
        page.getByText("A clean product render", { exact: false }),
      ).toBeVisible();
      await auditPage(page);
      await page.screenshot({
        path: `test-results/screens/library-grid-${mode}-${width}.png`,
        fullPage: true,
      });
      await page.getByRole("button", { name: "Images", exact: true }).click();
      await expect(
        page.getByRole("button", { name: "Images", exact: true }),
      ).toHaveAttribute("aria-pressed", "true");
      expect(mediaRequests.at(-1)).toContain("kind=image");
      await page.getByLabel("Search prompts").fill("desk");
      await expect.poll(() => mediaRequests.at(-1) ?? "").toContain("q=desk");
      await page
        .getByRole("button", { name: /A clean product render/ })
        .click();
      await expect(page.getByRole("dialog")).toContainText("Workflow");
      await expect(
        page.getByRole("link", { name: "Download" }),
      ).toHaveAttribute("href", mediaFixture.assets[0].download_url!);
      await auditPage(page);
      const cards = await page
        .locator("article")
        .evaluateAll((items) =>
          items.map((item) => item.getBoundingClientRect().height),
        );
      expect(Math.max(...cards) - Math.min(...cards)).toBeLessThan(2);
      await page.screenshot({
        path: `test-results/screens/library-sheet-${mode}-${width}.png`,
        fullPage: false,
      });
    });

    test(`library empty ${mode} ${width}`, async ({ page }) => {
      await setTheme(page, mode, width);
      await page.route("**/me/api/media?**", (route) =>
        route.fulfill({ json: { assets: [], models: [], next_cursor: null } }),
      );
      await page.goto("/ui/library/");
      await expect(page.getByText("No media yet")).toBeVisible();
      await auditPage(page);
      await page.screenshot({
        path: `test-results/screens/library-empty-${mode}-${width}.png`,
        fullPage: true,
      });
    });

    test(`library error ${mode} ${width}`, async ({ page }) => {
      await setTheme(page, mode, width);
      let failed = true;
      await page.route("**/me/api/media?**", (route) => {
        if (failed) {
          failed = false;
          return route.fulfill({
            status: 503,
            json: { error: { message: "Unavailable for this test" } },
          });
        }
        return route.fulfill({ json: mediaFixture });
      });
      await page.goto("/ui/library/");
      await expect(
        page.getByText("Your library could not be loaded. Try again."),
      ).toBeVisible();
      await auditPage(page);
      await page.screenshot({
        path: `test-results/screens/library-error-${mode}-${width}.png`,
        fullPage: true,
      });
      await page.getByRole("button", { name: "Try again" }).click();
      await expect(
        page.getByText("A clean product render", { exact: false }),
      ).toBeVisible();
      await page.screenshot({
        path: `test-results/screens/library-error-retry-${mode}-${width}.png`,
        fullPage: true,
      });
    });
  }
}

test("library delete confirms before mutation", async ({ page }) => {
  await setTheme(page, "light", 1440);
  let deleted = false;
  await page.route("**/me/api/media?**", (route) =>
    route.fulfill({ json: mediaFixture }),
  );
  await page.route("**/me/api/media/asset_image_1", (route) => {
    if (route.request().method() !== "DELETE") return route.continue();
    expect(route.request().headers()["x-ai-gateway-user-action"]).toBe("1");
    expect(route.request().headers()["x-ai-gateway-csrf"]).toBeTruthy();
    deleted = true;
    return route.fulfill({ json: { ok: true } });
  });
  await page.goto("/ui/library/");
  await page.getByRole("button", { name: /A clean product render/ }).click();
  await page.getByRole("button", { name: "Delete" }).click();
  await expect(
    page.getByRole("dialog", { name: "Delete this media item?" }),
  ).toBeVisible();
  expect(deleted).toBe(false);
  await page
    .getByRole("dialog", { name: "Delete this media item?" })
    .getByRole("button", { name: "Delete" })
    .click();
  await expect.poll(() => deleted).toBe(true);
});

test("library filters dates and everyone, pages forward and preserves prior items", async ({
  page,
}) => {
  const requests: string[] = [];
  await page.route("**/me/api/media?**", (route) => {
    const url = route.request().url();
    requests.push(url);
    return route.fulfill({
      json: url.includes("cursor=")
        ? {
            assets: [
              {
                ...mediaFixture.assets[0],
                asset_id: "page_two",
                prompt: "Second page image",
              },
            ],
            models: ["image"],
            next_cursor: null,
          }
        : mediaFixture,
    });
  });
  await page.goto("/ui/library/");
  await expect(
    page.getByRole("button", { name: /Open A clean product render/ }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Load more" }).click();
  await expect(
    page.getByRole("button", { name: "Open Second page image" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: /Open A clean product render/ }),
  ).toBeVisible();
  expect(requests.at(-1)).toContain("cursor=cursor_next");
  await page.getByLabel("Date", { exact: true }).click();
  await page.getByRole("option", { name: "Last 7 days" }).click();
  await expect.poll(() => requests.at(-1) ?? "").toContain("start=");
  await page.getByLabel("Owner", { exact: true }).click();
  await page.getByRole("option", { name: "Everyone’s" }).click();
  await expect.poll(() => requests.at(-1) ?? "").toContain("scope=everyone");
  await page
    .getByRole("button", { name: /Open A clean product render/ })
    .click();
  await expect(
    page.getByRole("button", { name: "Delete", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("link", { name: "Open in ComfyUI" }),
  ).toBeVisible();
});

test("library audio plays independently and video uses its recorded poster", async ({
  page,
}) => {
  // A valid one-second WAV verifies the browser player without a live engine.
  const wav = Buffer.alloc(44 + 16000);
  wav.write("RIFF", 0);
  wav.writeUInt32LE(wav.length - 8, 4);
  wav.write("WAVEfmt ", 8);
  wav.writeUInt32LE(16, 16);
  wav.writeUInt16LE(1, 20);
  wav.writeUInt16LE(1, 22);
  wav.writeUInt32LE(8000, 24);
  wav.writeUInt32LE(16000, 28);
  wav.writeUInt16LE(2, 32);
  wav.writeUInt16LE(16, 34);
  wav.write("data", 36);
  wav.writeUInt32LE(16000, 40);
  const posterUrl = mediaFixture.assets[0].download_url;
  const posterRequests: string[] = [];
  await page.route("**/me/api/media?**", (route) =>
    route.fulfill({
      json: {
        ...mediaFixture,
        assets: mediaFixture.assets.map((asset) =>
          asset.kind === "video"
            ? { ...asset, poster_asset_id: "video-poster" }
            : asset.kind === "audio"
              ? { ...asset, download_url: "/test-audio.wav" }
              : asset,
        ),
      },
    }),
  );
  await page.route("**/test-audio.wav", (route) =>
    route.fulfill({ contentType: "audio/wav", body: wav }),
  );
  await page.route("**/me/api/media/video-poster/download*", (route) => {
    posterRequests.push(route.request().url());
    return route.fulfill({ json: { download_url: posterUrl } });
  });
  await page.goto("/ui/library/");
  const audio = page.locator("article audio");
  await expect(audio).toHaveAttribute("controls", "");
  expect(await audio.evaluate((player) => player.closest("button"))).toBeNull();
  await audio.click({ position: { x: 20, y: 20 } });
  await expect
    .poll(() =>
      audio.evaluate((player: HTMLAudioElement) => player.currentTime),
    )
    .toBeGreaterThan(0);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(
    page.getByRole("img", { name: mediaFixture.assets[1].prompt }),
  ).toHaveAttribute("src", posterUrl);
  await expect(page.locator("article video")).toHaveCount(0);
  await page.getByLabel("Owner", { exact: true }).click();
  await page.getByRole("option", { name: "Everyone’s" }).click();
  await expect
    .poll(() => posterRequests.at(-1) ?? "")
    .toContain("scope=everyone");
  await auditPage(page);
});
