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
      // Tiles are named by their prompt; the caption shows its subject.
      const tile = page.getByRole("button", {
        name: /Open A clean product render/,
      });
      await expect(tile).toBeVisible();
      await auditPage(page);
      await page.screenshot({
        path: `test-results/screens/library-grid-${mode}-${width}.png`,
        fullPage: true,
      });
      // The kind switch is in the row at every width (D1.4).
      const images = page
        .getByRole("group", { name: "Kind" })
        .getByRole("button", { name: "Images", exact: true });
      await images.click();
      await expect(images).toHaveAttribute("aria-pressed", "true");
      expect(mediaRequests.at(-1)).toContain("kind=image");
      await page
        .locator('input[placeholder="Search your creations"]:visible')
        .fill("desk");
      await expect.poll(() => mediaRequests.at(-1) ?? "").toContain("q=desk");
      await tile.click();
      const dialog = page.getByRole("dialog");
      // Download prepares a fresh signed URL on click (page.spec.tsx checks
      // the request); the workflow link sits in Details.
      await expect(
        dialog.getByRole("button", { name: "Download", exact: true }),
      ).toBeVisible();
      await dialog.getByText("Details", { exact: true }).click();
      await expect(dialog).toContainText("Workflow");
      await expect(
        dialog.getByRole("link", { name: "Download ComfyUI workflow" }),
      ).toHaveAttribute("href", mediaFixture.assets[0].workflow_download_url!);
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
      await expect(page.getByText("No creations yet")).toBeVisible();
      // The empty state names the one action that fills it.
      await expect(
        page.getByRole("link", { name: "Make an image" }),
      ).toHaveAttribute("href", "/ui/try/?capability=image");
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
        page.getByText("Couldn’t load your library", { exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("button", { name: "Try again" }),
      ).toBeVisible();
      await auditPage(page);
      await page.screenshot({
        path: `test-results/screens/library-error-${mode}-${width}.png`,
        fullPage: true,
      });
      await page.getByRole("button", { name: "Try again" }).click();
      // Picture tiles carry no text; the tile is named by its prompt.
      await expect(
        page.getByRole("button", { name: /Open A clean product render/ }),
      ).toBeVisible();
      await expect(
        page.getByText("Couldn’t load your library", { exact: true }),
      ).toHaveCount(0);
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
    expect(route.request().headers()["x-azizam-user-action"]).toBe("1");
    expect(route.request().headers()["x-azizam-csrf"]).toBeTruthy();
    deleted = true;
    return route.fulfill({ json: { ok: true } });
  });
  await page.goto("/ui/library/");
  await page.getByRole("combobox", { name: "Person", exact: true }).click();
  await page.getByRole("option", { name: /^(You|.+ \(you\))$/ }).click();
  await page.getByRole("button", { name: /A clean product render/ }).click();
  await page
    .getByRole("button", { name: /Actions for A clean product render/ })
    .click();
  await page.getByRole("menuitem", { name: "Delete" }).click();
  await expect(
    page.getByRole("dialog", { name: /Delete A clean product render/ }),
  ).toBeVisible();
  expect(deleted).toBe(false);
  await page
    .getByRole("dialog", { name: /Delete A clean product render/ })
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
  // Model, date and app live in Filters; the owner filter is "Person".
  await page.getByRole("button", { name: /^Filters/ }).click();
  await page.getByRole("combobox", { name: "Date", exact: true }).click();
  await page.getByRole("option", { name: "Last 7 days" }).click();
  await expect.poll(() => requests.at(-1) ?? "").toContain("start=");
  await page.keyboard.press("Escape");
  const person = page.getByRole("combobox", { name: "Person", exact: true });
  await person.click();
  await page.getByRole("option", { name: /\(you\)$|^You$/ }).click();
  await expect.poll(() => requests.at(-1) ?? "").toContain("scope=me");
  await person.click();
  await page.getByRole("option", { name: "Everyone", exact: true }).click();
  await expect.poll(() => requests.at(-1) ?? "").toContain("scope=everyone");
  await page
    .getByRole("button", { name: /Open A clean product render/ })
    .click();
  await expect(
    page.getByRole("button", { name: "Delete", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("dialog").getByText("Details", { exact: true }).click();
  await expect(
    page.getByRole("link", { name: "Download ComfyUI workflow" }),
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
  await expect(audio).not.toHaveAttribute("controls", "");
  await expect(audio).toHaveAttribute("preload", "none");
  expect(await audio.evaluate((player) => player.closest("button"))).toBeNull();
  const play = page.getByRole("button", {
    name: `Play ${mediaFixture.assets[2].prompt}`,
  });
  const open = page.getByRole("button", {
    name: `Open ${mediaFixture.assets[2].prompt}`,
  });
  expect(
    await open.evaluate((element) =>
      Boolean(element.querySelector('button[aria-label^="Play "]')),
    ),
  ).toBe(false);
  await play.click();
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
  await open.click({ position: { x: 20, y: 20 } });
  const detail = page.getByRole("dialog");
  // The viewer has its own designed player: play, position and time.
  const viewerAudio = detail.getByLabel("Generated audio");
  await expect(viewerAudio).toHaveAttribute("src", "/test-audio.wav");
  await expect(detail.getByRole("slider", { name: "Position" })).toBeVisible();
  await detail
    .getByRole("button", { name: `Play ${mediaFixture.assets[2].prompt}` })
    .click();
  await expect
    .poll(() =>
      viewerAudio.evaluate((player: HTMLAudioElement) => player.currentTime),
    )
    .toBeGreaterThan(0);
  await detail.getByRole("button", { name: "Close", exact: true }).click();
  await expect(page).not.toHaveURL(/asset=/);
  await expect(detail).toBeHidden();
  await expect
    .poll(() => posterRequests.at(-1) ?? "")
    .toContain("scope=everyone");
  await page.getByRole("combobox", { name: "Person", exact: true }).click();
  await page.getByRole("option", { name: /^(You|.+ \(you\))$/ }).click();
  await expect.poll(() => posterRequests.at(-1) ?? "").toContain("scope=me");
  await page.getByRole("combobox", { name: "Person", exact: true }).click();
  await page.getByRole("option", { name: "Everyone", exact: true }).click();
  await expect(
    page.getByRole("combobox", { name: "Person", exact: true }),
  ).toContainText("Everyone");
  // The instance poster is already cached from the default owner view.
  expect(posterRequests.some((url) => url.includes("scope=everyone"))).toBe(
    true,
  );
  await expect(page.locator('[role="listbox"]')).toHaveCount(0);
  await auditPage(page);
});

for (const width of [390, 1440]) {
  test(`Library retains pagination failures and wraps long prompts at ${width}`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 844 });
    const prompt = "Azizam".repeat(90);
    let attempts = 0;
    await page.route("**/me/api/media?**", (route) => {
      if (new URL(route.request().url()).searchParams.has("cursor")) {
        attempts++;
        return attempts === 1
          ? route.fulfill({
              status: 503,
              json: { error: { message: "Page unavailable" } },
            })
          : route.fulfill({
              json: {
                assets: [
                  {
                    ...mediaFixture.assets[0],
                    asset_id: "second",
                    prompt: "Second saved image",
                  },
                ],
                next_cursor: null,
              },
            });
      }
      return route.fulfill({
        json: {
          assets: [{ ...mediaFixture.assets[0], prompt }],
          next_cursor: "next",
        },
      });
    });
    await page.goto("/ui/library/");
    await page.getByRole("button", { name: "Load more", exact: true }).click();
    await expect(
      page.getByRole("alert").filter({
        hasText: "More library items could not be loaded.",
      }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: `Open ${prompt}` }),
    ).toBeVisible();
    await page.getByRole("button", { name: /Load more|Retry/i }).click();
    await expect(
      page.getByRole("button", { name: "Open Second saved image" }),
    ).toBeVisible();
    await page.getByRole("button", { name: `Open ${prompt}` }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    expect(
      await dialog.evaluate(
        (element) => element.scrollWidth <= element.clientWidth,
      ),
    ).toBe(true);
    await auditPage(page);
  });
}

test("Library failed preview retries in place with the same URL", async ({
  page,
}) => {
  let broken = true;
  await page.route("**/me/api/media?**", (route) =>
    route.fulfill({
      json: {
        assets: [
          { ...mediaFixture.assets[0], download_url: "/azizam-retry.png" },
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/me/api/media/asset_image_1/download*", (route) =>
    route.fulfill({ json: { download_url: "/azizam-retry.png" } }),
  );
  await page.route("**/azizam-retry.png", (route) =>
    broken
      ? route.fulfill({ status: 503, body: "Unavailable" })
      : route.fulfill({
          body: '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><rect width="32" height="32" fill="teal"/></svg>',
          contentType: "image/svg+xml",
        }),
  );
  await page.goto("/ui/library/");
  await expect(
    page.getByRole("img", { name: "Image preview unavailable" }),
  ).toBeVisible();
  broken = false;
  await page.getByRole("button", { name: /Retry/i }).click();
  await expect(page.locator("article img")).toBeVisible();
  await expect(
    page.getByRole("img", { name: "Image preview unavailable" }),
  ).toHaveCount(0);
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("Library waits for posters and exposes recovery after both candidates fail", async ({
  page,
}) => {
  let release!: () => void;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  let videoRequests = 0;
  await page.route("**/me/api/media?**", (route) =>
    route.fulfill({
      json: {
        assets: [
          {
            ...mediaFixture.assets[1],
            download_url: "/azizam-broken.mp4",
            poster_asset_id: "azizam-poster",
          },
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/me/api/media/azizam-poster/download*", async (route) => {
    await pending;
    await route.fulfill({
      json: { download_url: "/azizam-broken-poster.png" },
    });
  });
  await page.route("**/azizam-broken-poster.png", (route) =>
    route.fulfill({ status: 404, body: "Unavailable" }),
  );
  await page.route("**/azizam-broken.mp4", (route) => {
    videoRequests++;
    return route.fulfill({ status: 404, body: "Unavailable" });
  });
  await page.goto("/ui/library/", { waitUntil: "domcontentloaded" });
  await expect(
    page.getByRole("button", { name: /Open Gateway status/ }),
  ).toBeVisible();
  expect(videoRequests).toBe(0);
  await expect(page.locator("article video")).toHaveCount(0);
  release();
  await expect(
    page.getByRole("img", { name: "Video preview unavailable" }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: /Retry/i })).toBeVisible();
});
