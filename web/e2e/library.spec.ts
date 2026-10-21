import { type Page } from "@playwright/test";
import { expect, test } from "./fixtures/isolation";
import AxeBuilder from "@axe-core/playwright";
import path from "node:path";
import { fulfilJson, fulfilBytes } from "./fixtures/intercept";

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
    await fulfilJson(route, mediaFixture);
  });
  await page.goto("/library/", { waitUntil: "domcontentloaded" });
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
        return fulfilJson(route, mediaFixture);
      });
      await page.route("**/me/api/media/asset_image_1/download?*", (route) => fulfilJson(route, { download_url: mediaFixture.assets[0].download_url }));
      await page.goto("/library/");
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
      await expect.poll(() => mediaRequests.at(-1) ?? "").toContain("kind=image");
      // On a phone the search field opens from a button in the row (D1.5).
      if (width === 390)
        await page.getByRole("button", { name: "Search", exact: true }).click();
      await page
        .locator('input[placeholder="Search your creations"]:visible')
        .fill("desk");
      await expect.poll(() => mediaRequests.at(-1) ?? "").toContain("q=desk");
      await tile.click();
      const dialog = page.getByRole("dialog");
      // Download prepares a fresh signed URL on click (page.spec.tsx checks
      // the request); the maker's file with details is in the actions menu.
      await expect(
        dialog.getByRole("button", { name: "Download", exact: true }),
      ).toBeVisible();
      await dialog.getByText("Details", { exact: true }).click();
      await expect(dialog).toContainText("Workflow");
      await dialog.getByRole("button", { name: /^Actions for / }).click();
      await expect(
        page.getByRole("menuitem", { name: "Download with details" }),
      ).toBeVisible();
      await page.keyboard.press("Escape");
      await auditPage(page);
      const cards = await page
        .locator("article")
        .evaluateAll((items) =>
          items.map((item) => item.getBoundingClientRect().height),
        );
      expect(Math.max(...cards) - Math.min(...cards)).toBeLessThan(2);
      await expect(page.getByText(/Playback link expires|Reopen to renew/)).toHaveCount(0);
      if (mode === "light") await page.screenshot({ path: `test-results/screens/uxfb/library-viewer-light-${width}.png` });
      await page.screenshot({
        path: `test-results/screens/library-sheet-${mode}-${width}.png`,
        fullPage: false,
      });
    });

    test(`library empty ${mode} ${width}`, async ({ page }) => {
      await setTheme(page, mode, width);
      await page.route("**/me/api/media?**", (route) =>
        fulfilJson(route, { assets: [], models: [], next_cursor: null }),
      );
      await page.goto("/library/");
      await expect(page.getByText("No creations yet")).toBeVisible();
      // The empty state names the one action that fills it.
      await expect(
        page.getByRole("link", { name: "Make an image" }),
      ).toHaveAttribute("href", "/try/?capability=image");
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
          return fulfilJson(route, { error: { message: "Unavailable for this test" } }, { status: 503 });
        }
        return fulfilJson(route, mediaFixture);
      });
      await page.goto("/library/");
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
    fulfilJson(route, deleted
        ? {
            ...mediaFixture,
            assets: mediaFixture.assets.filter(
              (asset) => asset.asset_id !== "asset_image_1",
            ),
          }
        : mediaFixture),
  );
  await page.route("**/me/api/media/asset_image_1", (route) => {
    if (route.request().method() !== "DELETE") return route.continue();
    expect(route.request().headers()["x-azizam-user-action"]).toBe("1");
    expect(route.request().headers()["x-azizam-csrf"]).toBeTruthy();
    deleted = true;
    return fulfilJson(route, { ok: true });
  });
  await page.goto("/library/");
  await page.getByRole("combobox", { name: "Person", exact: true }).click();
  await page.getByRole("option", { name: "Mine", exact: true }).click();
  await page.getByRole("button", { name: /A clean product render/ }).click();
  const viewer = page.getByRole("dialog", {
    name: "A clean product render of a teal fish gateway on a desk",
    exact: true,
  });
  await expect(page).toHaveURL(/asset=asset_image_1/);
  await page
    .getByRole("button", { name: /Actions for A clean product render/ })
    .click();
  await page.getByRole("menuitem", { name: "Delete" }).click();
  const confirm = page.getByRole("dialog", {
    name: /Delete A clean product render/,
  });
  await expect(confirm).toBeVisible();
  expect(deleted).toBe(false);
  // Cancel leaves the creation open and nothing deleted.
  await confirm.getByRole("button", { name: "Cancel" }).click();
  await expect(confirm).toBeHidden();
  await expect(viewer).toBeVisible();
  expect(deleted).toBe(false);

  await page
    .getByRole("button", { name: /Actions for A clean product render/ })
    .click();
  await page.getByRole("menuitem", { name: "Delete" }).click();
  await confirm.getByRole("button", { name: "Delete" }).click();
  await expect.poll(() => deleted).toBe(true);
  // A deleted creation leaves the viewer and the address bar.
  await expect(viewer).toBeHidden();
  await expect(page).not.toHaveURL(/asset=/);
});

for (const lifecycle of ["first load", "reload", "clear filters", "Mine", "deep link"] as const) {
  test(`owner Library keeps explicit own scope after ${lifecycle}`, async ({ page }) => {
    const paths: string[] = [];
    const signedRequests: string[] = [];
    const returningToMine = lifecycle === "clear filters" || lifecycle === "Mine";
    let everyoneSeen = false;
    if (returningToMine) await page.clock.install();
    const own = {
      ...mediaFixture.assets[0], asset_id: "owner-image", prompt: "Owner image",
      owner_name: "UI owner", workflow: "image.generate",
      source: { id: "app:owner", kind: "app", label: "Owner app", app_id: "owner" },
      thumbnail_url: "/library-fixture/owner-thumb",
    };
    const detail = { ...own, asset_id: "owner-detail", prompt: "Owner detail", thumbnail_url: null };
    const other = { ...own, asset_id: "other-image", prompt: "Other image", owner_name: "Other", source: { id: "app:other", kind: "app", label: "Other app", app_id: "other" }, thumbnail_url: "/library-fixture/other-thumb" };
    await page.addInitScript(() => {
      Object.defineProperty(navigator, "share", { configurable: true, value: async () => { (window as unknown as { shared: boolean }).shared = true; } });
      Object.defineProperty(navigator, "canShare", { configurable: true, value: () => true });
    });
    await page.route("**/library-fixture/**", (route) => {
      signedRequests.push(route.request().url());
      return fulfilBytes(route, { contentType: "image/svg+xml", body: Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"/>') });
    });
    await page.route("**/me/api/media{,/**}?**", (route) => {
      const url = new URL(route.request().url());
      paths.push(url.toString());
      if (url.pathname === "/me/api/media") {
        if (url.searchParams.get("scope") === "everyone") everyoneSeen = true;
        return fulfilJson(route, {
        assets: url.searchParams.get("scope") === "me" ? (returningToMine && !everyoneSeen ? [] : [own, {
          ...mediaFixture.assets[1], asset_id: "owner-video", prompt: "Owner video", source: own.source, poster_asset_id: "owner-poster",
        }]) : [other], models: [], next_cursor: null,
        });
      }
      if (url.pathname.endsWith("/download")) return fulfilJson(route, {
        download_url: `/library-fixture/${url.pathname.split("/")[4]}`,
      });
      return fulfilJson(route, { asset: detail });
    });
    await page.goto(lifecycle === "deep link" ? "/library/?asset=owner-detail" : "/library/");
    if (returningToMine) await expect(page.getByRole("region", { name: "No creations yet" })).toBeVisible();
    else await expect(page.getByRole("button", { name: /^Open Owner image,/, includeHidden: true })).toBeAttached();
    if (lifecycle === "reload") {
      paths.length = 0;
      signedRequests.length = 0;
      await page.reload();
      await expect(page.getByRole("button", { name: /^Open Owner image,/ })).toBeVisible();
    }
    if (lifecycle === "clear filters" || lifecycle === "Mine") {
      await page.getByRole("combobox", { name: "Person", exact: true }).click();
      await page.getByRole("option", { name: "Everyone", exact: true }).click();
      await expect(page.getByRole("button", { name: /^Open Other image,/ })).toBeVisible();
      // Expire the 30-second query cache so Clear filters also exercises a new list request.
      await page.clock.fastForward(31_000);
      paths.length = 0;
      signedRequests.length = 0;
      if (lifecycle === "Mine") {
        await page.getByRole("combobox", { name: "Person", exact: true }).click();
        await page.getByRole("option", { name: "Mine", exact: true }).click();
      } else await page.getByRole("button", { name: "Clear filters", exact: true }).click();
      await expect(page.getByRole("button", { name: /^Open Owner image,/ })).toBeVisible();
    }
    await expect(page.getByRole("button", { name: /^Open Other image,/ })).toHaveCount(0);
    await expect.poll(() => paths.some((url) => url.includes("/owner-poster/download?"))).toBe(true);
    await expect.poll(() => signedRequests.some((url) => url.endsWith("/owner-thumb"))).toBe(true);
    expect(signedRequests.some((url) => url.includes("/other-thumb"))).toBe(false);
    if (lifecycle !== "deep link") {
      await page.evaluate(() => window.history.pushState(null, "", "?asset=owner-detail"));
    }
    const dialog = page.getByRole("dialog", { name: "Owner detail", exact: true });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Download", exact: true })).toBeEnabled();
    await dialog.getByRole("button", { name: "Download", exact: true }).click();
    await dialog.getByRole("button", { name: "Share", exact: true }).click();
    await expect.poll(() => page.evaluate(() => (window as unknown as { shared: boolean }).shared)).toBe(true);
    expect(paths.some((url) => new URL(url).pathname === "/me/api/media")).toBe(true);
    expect(paths.some((url) => new URL(url).pathname === "/me/api/media/owner-detail")).toBe(true);
    expect(paths.filter((url) => url.includes("/owner-detail/download?")).length).toBeGreaterThanOrEqual(3);
    for (const path of paths) {
      const params = new URL(path).searchParams;
      expect(params.getAll("scope"), path).toEqual(["me"]);
      expect(params.has("person"), path).toBe(false);
    }
  });
}

test("empty Everyone to Mine offers creation instead of clearing identical filters", async ({ page }) => {
  await page.route("**/me/api/media?**", (route) => fulfilJson(route, { assets: [], models: [], next_cursor: null }));
  await page.goto("/library/");
  await page.getByRole("combobox", { name: "Person", exact: true }).click();
  await page.getByRole("option", { name: "Everyone", exact: true }).click();
  await expect(page.getByRole("region", { name: "No matching creations" })).toBeVisible();
  await page.getByRole("combobox", { name: "Person", exact: true }).click();
  await page.getByRole("option", { name: "Mine", exact: true }).click();
  const empty = page.getByRole("region", { name: "No creations yet" });
  await expect(empty).toHaveAccessibleDescription("Images, videos and audio you create appear here.");
  await expect(empty.getByRole("link", { name: "Make an image" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Clear filters", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /^Filters/ })).not.toContainText("1");
});

test("library filters dates and everyone, pages forward and preserves prior items", async ({
  page,
}) => {
  const requests: string[] = [];
  await page.route("**/me/api/media?**", (route) => {
    const url = route.request().url();
    requests.push(url);
    return fulfilJson(route, url.includes("cursor=")
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
        : mediaFixture);
  });
  await page.goto("/library/");
  await expect(
    page.getByRole("button", { name: /Open A clean product render/ }),
  ).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Person", exact: true })).toContainText("Mine");
  expect(new URL(requests[0]).searchParams.get("scope")).toBe("me");
  expect(new URL(requests[0]).searchParams.has("person")).toBe(false);
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
  await page.getByRole("combobox", { name: "Date created", exact: true }).click();
  await page.getByRole("option", { name: "Last 7 days" }).click();
  await expect.poll(() => requests.at(-1) ?? "").toContain("start=");
  await page.keyboard.press("Escape");
  const person = page.getByRole("combobox", { name: "Person", exact: true });
  await person.click();
  await page.getByRole("option", { name: "Everyone", exact: true }).click();
  await expect.poll(() => requests.at(-1) ?? "").toContain("scope=everyone");
  await person.click();
  await page.getByRole("option", { name: "Mine", exact: true }).click();
  await expect.poll(() => requests.at(-1) ?? "").toContain("scope=me");
  await person.click();
  await page.getByRole("option", { name: "Everyone", exact: true }).click();
  await expect(person).toContainText("Everyone");
  expect(requests.some((url) => url.includes("scope=everyone"))).toBe(true);
  await page
    .getByRole("button", { name: /Open A clean product render/ })
    .click();
  await expect(
    page.getByRole("button", { name: "Delete", exact: true }),
  ).toHaveCount(0);
  // The gateway sent this item's details download, so the viewer is its
  // maker's: the file with details is offered, Delete is not.
  await page
    .getByRole("dialog")
    .getByRole("button", { name: /^Actions for / })
    .click();
  await expect(
    page.getByRole("menuitem", { name: "Download with details" }),
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
  const audioRenewals: string[] = [];
  const renewedAudioUrl = "/test-audio.wav?renewed=1";
  await page.route("**/me/api/media?**", (route) =>
    fulfilJson(route, {
        ...mediaFixture,
        assets: mediaFixture.assets.map((asset) =>
          asset.kind === "video"
            ? { ...asset, poster_asset_id: "video-poster" }
            : asset.kind === "audio"
              ? { ...asset, download_url: "/test-audio.wav" }
              : asset,
        ),
      }),
  );
  await page.route("**/test-audio.wav*", (route) =>
    fulfilBytes(route, { contentType: "audio/wav", body: wav }),
  );
  // Opening the viewer renews playback independently of the tile's URL.
  await page.route("**/me/api/media/asset_audio_1/download?*", (route) => {
    audioRenewals.push(route.request().url());
    return fulfilJson(route, { download_url: renewedAudioUrl });
  });
  await page.route("**/me/api/media/video-poster/download*", (route) => {
    posterRequests.push(route.request().url());
    return fulfilJson(route, { download_url: posterUrl });
  });
  await page.goto("/library/");
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
  // The tile video holds no source until it is played, so the poster is what loads.
  await expect(page.locator("article video[src]")).toHaveCount(0);
  await open.click({ position: { x: 20, y: 20 } });
  const detail = page.getByRole("dialog");
  // The viewer has its own designed player: play, position and time.
  const viewerAudio = detail.getByLabel("Generated audio");
  await expect(viewerAudio).toHaveAttribute("src", renewedAudioUrl);
  expect(audioRenewals).toHaveLength(1);
  expect(new URL(audioRenewals[0]).searchParams.get("scope")).toBe("me");
  await expect(audio).toHaveAttribute("src", "/test-audio.wav");
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
    .toContain("scope=me");
  await page.getByRole("combobox", { name: "Person", exact: true }).click();
  await page.getByRole("option", { name: "Mine", exact: true }).click();
  await expect.poll(() => posterRequests.at(-1) ?? "").toContain("scope=me");
  await page.getByRole("combobox", { name: "Person", exact: true }).click();
  await page.getByRole("option", { name: "Everyone", exact: true }).click();
  await expect(
    page.getByRole("combobox", { name: "Person", exact: true }),
  ).toContainText("Everyone");
  await expect.poll(() => posterRequests.at(-1) ?? "").toContain("scope=everyone");
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
          ? fulfilJson(route, { error: { message: "Page unavailable" } }, { status: 503 })
          : fulfilJson(route, {
                assets: [
                  {
                    ...mediaFixture.assets[0],
                    asset_id: "second",
                    prompt: "Second saved image",
                  },
                ],
                next_cursor: null,
              });
      }
      return fulfilJson(route, {
          assets: [{ ...mediaFixture.assets[0], prompt }],
          next_cursor: "next",
        });
    });
    await page.goto("/library/");
    await page.getByRole("button", { name: "Load more", exact: true }).click();
    await expect(
      page.getByRole("alert").filter({
        hasText: "We couldn’t load more library items. Try again.",
      }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: `Open ${prompt}` }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Retry loading more", exact: true }).click();
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
    fulfilJson(route, {
        assets: [
          { ...mediaFixture.assets[0], download_url: "/azizam-retry.png" },
        ],
        next_cursor: null,
      }),
  );
  await page.route("**/me/api/media/asset_image_1/download*", (route) =>
    fulfilJson(route, { download_url: "/azizam-retry.png" }),
  );
  await page.route("**/azizam-retry.png", (route) =>
    broken
      ? fulfilBytes(route, { status: 503, body: "Unavailable" })
      : fulfilBytes(route, {
          body: '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><rect width="32" height="32" fill="teal"/></svg>',
          contentType: "image/svg+xml",
        }),
  );
  await page.goto("/library/");
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
    fulfilJson(route, {
        assets: [
          {
            ...mediaFixture.assets[1],
            download_url: "/azizam-broken.mp4",
            poster_asset_id: "azizam-poster",
          },
        ],
        next_cursor: null,
      }),
  );
  await page.route("**/me/api/media/azizam-poster/download*", async (route) => {
    await pending;
    await fulfilJson(route, { download_url: "/azizam-broken-poster.png" });
  });
  await page.route("**/azizam-broken-poster.png", (route) =>
    fulfilBytes(route, { status: 404, body: "Unavailable" }),
  );
  await page.route("**/azizam-broken.mp4", (route) => {
    videoRequests++;
    return fulfilBytes(route, { status: 404, body: "Unavailable" });
  });
  await page.goto("/library/", { waitUntil: "domcontentloaded" });
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

for (const [mode, width] of [
  ["light", 1440],
  ["dark", 390],
] as const) {
  test(`library filtered empty ${mode} ${width}`, async ({ page }) => {
    await setTheme(page, mode, width);
    await page.route("**/me/api/media?**", (route) =>
      fulfilJson(route, new URL(route.request().url()).searchParams.get("kind")
          ? { assets: [], models: [], next_cursor: null }
          : mediaFixture),
    );
    await page.goto("/library/");
    await expect(
      page.getByRole("button", { name: /Open A clean product render/ }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Video", exact: true }).click();
    await expect(page.getByText("No matching creations")).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Clear filters" }).last(),
    ).toBeVisible();
    await auditPage(page);
  });
}

test("library video without a poster decodes a real first frame", async ({
  page,
}) => {
  await setTheme(page, "dark", 390);
  await page.route("**/me/api/media?**", (route) =>
    fulfilJson(route, {
        assets: [
          {
            asset_id: "asset_video_noposter",
            kind: "video",
            prompt: "A teal launch clip",
            model: "video",
            created_at: 1_760_010_000,
            download_url: "/fixture-preview.mp4",
            poster_asset_id: null,
          },
        ],
        models: ["video"],
        next_cursor: null,
      }),
  );
  await page.route("**/fixture-preview.mp4", (route) =>
    fulfilBytes(route, {
      path: path.join(__dirname, "fixtures", "preview-test.mp4"),
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
  // The tile is a still frame, not a player.
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
});

test("library audio tile play control is at least 44 px on a phone", async ({
  page,
}) => {
  await setTheme(page, "light", 390);
  await page.route("**/me/api/media?**", (route) =>
    fulfilJson(route, mediaFixture),
  );
  await page.goto("/library/");
  const play = page.getByRole("button", {
    name: /^Play A calm spoken introduction/,
  });
  await expect(play).toBeVisible();
  const box = await play.boundingBox();
  expect(box?.width).toBeGreaterThanOrEqual(44);
  expect(box?.height).toBeGreaterThanOrEqual(44);
});
