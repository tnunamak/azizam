import { type Page, type APIRequestContext } from "@playwright/test";
import { test, expect } from "./fixtures/isolation";
import AxeBuilder from "@axe-core/playwright";
import type { HomeSnapshot } from "../src/app/_home/home-api";
import { readFile } from "node:fs/promises";

test("the real seeded registry agrees on chat readiness across Home and Models", async ({
  page,
  request,
}) => {
  const home = await (await request.get("/me/api/home")).json();
  const catalog = await (await request.get("/me/api/models/catalog")).json();
  const chat = home.aliases.find(
    (alias: { id: string }) => alias.id === "fast",
  );
  const model = catalog.aliases.find(
    (alias: { id: string }) => alias.id === "fast",
  );
  expect(chat.status).toBe("ready");
  expect(model.availability.measured).toBe(true);
  expect(model.availability.available).toBe(true);
  await page.goto("/models/");
  await expect(
    page.getByText("Health endpoint did not respond", { exact: true }),
  ).toHaveCount(0);
});

async function fixture(
  page: Page,
  request: APIRequestContext,
  name = "Tim Nunamak",
) {
  const home = (await (
    await request.get("/me/api/home")
  ).json()) as HomeSnapshot;
  const user = { ...home.user, name };
  const aliases: HomeSnapshot["aliases"] = [
    {
      id: "default",
      operation: "chat",
      available: true,
      status: "ready",
      reason_code: "ready",
    },
    {
      id: "image",
      operation: "images",
      available: false,
      status: "unavailable",
      reason_code: "unavailable_disabled",
    },
    {
      id: "video",
      operation: "videos",
      available: false,
      status: "unavailable",
      reason_code: "unavailable",
    },
    {
      id: "tts",
      operation: "speech",
      available: true,
      status: "ready",
      reason_code: "ready",
    },
  ];
  await page.route("**/me/api/home", (route) =>
    route.fulfill({
      json: {
        ...home,
        user,
        aliases,
        // The server chooses one recommendation per permitted capability.
        capabilities: aliases.map((alias) => {
          const presentation = {
            chat: {
              capability: "chat",
              capability_title: "Chat",
              icon_key: "chat",
              recommended_model: "Demo Chat",
              intent: "chat.completions",
              summary: "Talk with Demo Chat.",
            },
            images: {
              capability: "image",
              capability_title: "Images",
              icon_key: "image",
              recommended_model: "Flux",
              intent: "image.generate",
              summary: "Generate images with Flux.",
            },
            videos: {
              capability: "video",
              capability_title: "Video",
              icon_key: "video",
              recommended_model: "LTX Video",
              intent: "video.generate",
              summary: "Generate video with LTX Video.",
            },
            speech: {
              capability: "speech",
              capability_title: "Speech",
              icon_key: "speech",
              recommended_model: "Speech model",
              intent: "speech.synthesize",
              summary: "Turn text into spoken audio.",
            },
          }[alias.operation as "chat" | "images" | "videos" | "speech"];
          return {
            ...presentation,
            title: presentation.recommended_model,
            model: alias.id,
            workflow: null,
            use_name: alias.id,
            use_names: [alias.id],
            available: alias.available,
            status: alias.status,
            reason_code: alias.reason_code,
          };
        }),
        new_app_aliases: aliases.filter((alias) => alias.operation === "chat"),
        apps: [
          {
            id: "polish-app",
            name: "Test client",
            kind: "personal",
            created_at: 1,
            disabled_at: null,
            can_mint_keys: true,
            profile: null,
            credentials: ["chat-key", "image-key"].map((id) => ({
              id,
              subject_kind: "app",
              subject_id: "polish-app",
              app_id: "polish-app",
              profile_id: null,
              prefix: "fixture",
              display_name: id === "chat-key" ? "Chat access" : "Images only",
              scopes: [],
              created_at: 1,
              expires_at: null,
              revoked_at: null,
              last_used_at: null,
            })),
          },
        ],
        credential_aliases: {
          "chat-key": aliases.filter((alias) => alias.operation === "chat"),
          "image-key": aliases.filter((alias) => alias.operation === "images"),
        },
        status: {
          state: "unavailable",
          reason_code: "unavailable_disabled",
          affected_capability_ids: ["image", "video"],
          counts: { total: 3, available: 1, affected: 2 },
        },
        receipts: [],
      },
    }),
  );
  await page.route("**/admin/api/setup-progress", (route) =>
    route.fulfill({ json: { complete: true, steps: [] } }),
  );
  await page.route("**/me/api/media?limit=50", (route) =>
    route.fulfill({ json: { assets: [] } }),
  );
  return home;
}

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390]) {
    test(`Home content truth: ${theme} at ${width}px`, async ({
      page,
      request,
    }) => {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      await page.addInitScript(
        (value) => localStorage.setItem("theme", value),
        theme,
      );
      await fixture(page, request);
      await page.goto("/");
      await expect(page.locator("main")).not.toContainText("Hello,");
      // D1.1: each task row says its own state. The disabled and unavailable
      // capabilities are named on their rows and are not offered as links.
      const tasks = page.getByRole("region", { name: "Make something" });
      const picture = tasks.getByRole("listitem").filter({
        hasText: "Make a picture",
      });
      await expect(picture).toContainText("Off");
      await expect(picture).toContainText("Turned off by the owner.");
      await expect(picture.getByRole("link")).toHaveCount(0);
      const video = tasks.getByRole("listitem").filter({
        hasText: "Make a video",
      });
      await expect(video).toContainText("Down");
      await expect(video.getByRole("link")).toHaveCount(0);
      await expect(page.getByRole("status").first()).not.toContainText(
        /everything works/i,
      );
      await expect(
        page.getByText("Some services are unavailable or not measured yet.", {
          exact: true,
        }),
      ).toHaveCount(0);
      await expect(
        page.getByText("Here’s how to use your AI.", { exact: true }),
      ).toHaveCount(0);
      // Only the usable tasks (chat, speech) open Try.
      await expect(page.locator('a[href*="/try/?capability="]')).toHaveCount(2);
      await expect(
        page.getByText("Unavailable right now", { exact: true }),
      ).toHaveCount(0);
      await expect(page.locator("main")).not.toContainText("face-likeness");
      await expect(page.locator("main")).not.toContainText("bernini-r-remix");
      await expect(page.getByRole("combobox", { name: "API key" })).toHaveCount(
        0,
      );
      await page.getByRole("button", { name: "Open WebUI" }).click();
      await expect(page.getByRole("combobox", { name: "API key" })).toHaveCount(
        1,
      );
      await expect(
        page.getByRole("button", { name: "Setup notes", exact: true }),
      ).toBeVisible();
      await expect(page.locator("main > div > details")).toHaveCount(0);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      const violations = (
        await new AxeBuilder({ page }).analyze()
      ).violations.filter((v) =>
        ["serious", "critical"].includes(v.impact ?? ""),
      );
      expect(violations).toEqual([]);
      await page.screenshot({
        path: `test-results/screens/home/polish2-${theme}-${width}.png`,
        fullPage: true,
      });
    });
  }
}

test("Home omits greeting when the API display name is an email", async ({
  page,
  request,
}) => {
  await fixture(page, request, "tim@example.test");
  await page.goto("/");
  await expect(page.locator("main")).not.toContainText("Hello,");
});

test("one selected and pasted app key scopes all recipes and the gateway check", async ({
  page,
  request,
}) => {
  await fixture(page, request);
  const checks: unknown[] = [];
  await page.route("**/me/api/connection-test", (route) => {
    checks.push(route.request().postDataJSON());
    return route.fulfill({
      json: {
        ok: true,
        latency_ms: 25,
        reason: "The selected key reached the gateway.",
        path: "/v1/models",
        model: null,
        request_id: null,
      },
    });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Open WebUI", exact: true }).click();
  const select = page.getByRole("combobox", { name: "API key" });
  await select.click();
  await page
    .getByRole("option", { name: "Test client · Chat access", exact: true })
    .click();
  const paste = page.getByLabel(/^Paste your /);
  await expect(paste).toHaveCount(1);
  await paste.fill("fixture-chat-secret");
  await expect(
    page.getByRole("textbox", { name: "Model", exact: true }),
  ).toHaveValue("default");
  await page.getByRole("button", { name: "Setup notes", exact: true }).click();
  await expect(page.locator("pre code:visible")).toContainText("default");
  await page.keyboard.press("Escape");
  await expect(paste).toHaveValue("fixture-chat-secret");
  await page
    .getByRole("button", { name: "Check connection", exact: true })
    .click();
  await expect(
    page.getByText(/^Gateway responded with this key · 25 ms$/),
  ).toBeVisible();
  expect(checks).toEqual([
    { api_key: "fixture-chat-secret", credential_id: "chat-key" },
  ]);
  await select.click();
  await page
    .getByRole("option", { name: "Test client · Images only", exact: true })
    .click();
  await expect(paste).toHaveValue("");
  await expect(
    page.getByRole("textbox", { name: "Model", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Check connection", exact: true }),
  ).toBeDisabled();
  await paste.fill("fixture-image-secret");
  await page
    .getByRole("button", { name: "Check connection", exact: true })
    .click();
  await expect.poll(() => checks.length).toBe(2);
  expect(checks[1]).toEqual({
    api_key: "fixture-image-secret",
    credential_id: "image-key",
  });
  expect(
    await page.evaluate(() =>
      [...Object.values(localStorage), ...Object.values(sessionStorage)].some(
        (value) => value.includes("fixture-image-secret"),
      ),
    ),
  ).toBe(false);
  await page.reload();
  await expect(page.getByLabel(/^Paste your /)).toHaveCount(0);
});

test("Home recent media carries content while requests stay in receipts", async ({
  page,
  request,
}) => {
  const home = await fixture(page, request);
  await page.route("**/me/api/home", (route) =>
    route.fulfill({
      json: {
        ...home,
        receipts: [
          {
            request_id: "polish-image-receipt",
            intent: "image.analyze",
            requested_model: "image.analyze.face-likeness",
            status: "succeeded",
          },
        ],
      },
    }),
  );
  await page.route("**/me/api/media?limit=50", (route) =>
    route.fulfill({
      json: {
        assets: [
          {
            asset_id: "polish-audio",
            kind: "audio",
            model: "audio.generate.ace-step-1.5-2b-base",
            prompt: "A river moves beneath the willow trees",
            download_url: "/signed/polish-audio",
          },
        ],
      },
    }),
  );
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Recent", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Recent requests", { exact: true })).toHaveCount(
    0,
  );
  await expect(page.locator('a[href="/usage/?receipt=polish-image-receipt"]')).toHaveCount(
    0,
  );
  // D1.5: the audio tile shows a waveform and a play badge; its words are
  // the link's name, not a caption.
  await expect(
    page.getByRole("link", { name: /A river moves beneath the willow trees/ }),
  ).toBeAttached();
  await expect(
    page.getByRole("button", {
      name: "Play A river moves beneath the willow trees",
    }),
  ).toBeVisible();
  await expect(
    page.locator('audio[src="/signed/polish-audio"]'),
  ).toHaveAttribute("preload", "none");
  await expect(page.locator("main")).not.toContainText(
    "audio.generate.ace-step-1.5-2b-base",
  );
});

test("Home and Library render a frame from posterless video", async ({
  page,
}) => {
  const videoBytes = await readFile("e2e/fixtures/preview-test.mp4");
  await page.addInitScript(() => {
    const currentTime = Object.getOwnPropertyDescriptor(
      HTMLMediaElement.prototype,
      "currentTime",
    );
    if (!currentTime?.get || !currentTime.set) return;
    Object.defineProperty(HTMLMediaElement.prototype, "currentTime", {
      configurable: true,
      get() {
        return currentTime.get!.call(this);
      },
      set(value: number) {
        (window as Window & { lastPreviewSeek?: number }).lastPreviewSeek =
          value;
        currentTime.set!.call(this, value);
      },
    });
  });
  await page.route("**/me/api/media?**", (route) =>
    route.fulfill({
      json: {
        assets: [
          {
            asset_id: "posterless-video",
            kind: "video",
            prompt: "A short test clip",
            model: "local-video",
            created_at: 1_760_000_000,
            download_url: "/preview-test.mp4",
            poster_asset_id: null,
            duration_s: 1,
            width: 160,
            height: 120,
          },
        ],
        models: ["local-video"],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/preview-test.mp4", (route) =>
    route.fulfill({ body: videoBytes, contentType: "video/mp4" }),
  );

  for (const route of ["home", "library"]) {
    // Home is the root page; there is no /home/ page.
    await page.goto(route === "home" ? "/" : `/${route}/`);
    const video = page.locator("main video");
    await expect(video).toBeVisible();
    // Home's tile preloads nothing until it plays; Library's posterless tile
    // loads metadata only (D1.1, library page.spec). Both seek to 0.1 s.
    await expect(video).toHaveAttribute(
      "preload",
      route === "home" ? "none" : "metadata",
    );
    await expect(video).toHaveJSProperty("muted", true);
    await expect
      .poll(() =>
        page.evaluate(
          () =>
            (window as Window & { lastPreviewSeek?: number }).lastPreviewSeek,
        ),
      )
      .toBe(0.1);
    await expect
      .poll(() =>
        video.evaluate((element) => (element as HTMLVideoElement).videoWidth),
      )
      .toBeGreaterThan(0);
    await expect
      .poll(() =>
        video.evaluate((element) => (element as HTMLVideoElement).readyState),
      )
      .toBeGreaterThanOrEqual(2);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    const violations = (
      await new AxeBuilder({ page }).analyze()
    ).violations.filter((violation) =>
      ["serious", "critical"].includes(violation.impact ?? ""),
    );
    expect(violations).toEqual([]);
  }
});

for (const width of [390, 1440]) {
  test(`Home names promptless links and wraps long prompts at ${width}`, async ({
    page,
    request,
  }) => {
    await page.setViewportSize({ width, height: 844 });
    await fixture(page, request);
    const prompt = "Azizam".repeat(90);
    const imageUrl =
      "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='32' height='32'%3E%3Crect width='32' height='32' fill='teal'/%3E%3C/svg%3E";
    await page.route("**/me/api/media?**", (route) =>
      route.fulfill({
        json: {
          assets: [
            {
              asset_id: "empty-prompt",
              kind: "image",
              prompt: "  ",
              download_url: imageUrl,
            },
            {
              asset_id: "long-prompt",
              kind: "image",
              prompt,
              download_url: imageUrl,
            },
          ],
          has_more: false,
        },
      }),
    );
    await page.goto("/");
    await expect(
      page.getByRole("link", { name: "Open Image in Library", exact: true }),
    ).toBeVisible();
    const longCard = page.getByRole("link", {
      name: `Open Image in Library: ${prompt}`,
    });
    await expect(longCard).toBeVisible();
    expect(
      await longCard.evaluate(
        (element) => element.scrollWidth <= element.clientWidth,
      ),
    ).toBe(true);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  });
}

test("Home failed images show a named unavailable preview", async ({
  page,
  request,
}) => {
  await fixture(page, request);
  await page.route("**/me/api/media?**", (route) =>
    route.fulfill({
      json: {
        assets: [
          {
            asset_id: "broken",
            kind: "image",
            prompt: null,
            download_url: "/azizam-home-broken.png",
          },
        ],
        has_more: false,
      },
    }),
  );
  await page.route("**/azizam-home-broken.png", (route) =>
    route.fulfill({ status: 404, body: "Unavailable" }),
  );
  await page.goto("/");
  await expect(
    page.getByRole("img", { name: "Image preview unavailable" }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Open Image in Library", exact: true }),
  ).toBeVisible();
});
