import { type Page, type APIRequestContext } from "@playwright/test";
import { test, expect } from "./fixtures/isolation";
import AxeBuilder from "@axe-core/playwright";
import type { HomeSnapshot } from "../src/app/_home/home-api";
import { readFile } from "node:fs/promises";
import { fulfilJson, fulfilBytes } from "./fixtures/intercept";

test("the real seeded registry agrees on chat readiness across Home and Models", async ({
  page,
  request,
}) => {
  const home = await (await request.get("/me/api/home")).json();
  const catalog = await (await request.get("/me/api/models/catalog")).json();
  const chat = home.capabilities.find(
    (row: { icon_key?: string; use_names?: string[] }) => row.icon_key === "chat" && row.use_names?.includes("fast"),
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
  // What each permitted capability answers with, which the stub turns into capability rows.
  const aliases: Array<{ id: string; operation: string; available: boolean; status: "ready" | "unavailable"; reason_code: string }> = [
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
    fulfilJson(route, {
        ...home,
        user,
        // Recent chats come from the shared gateway, which earlier tests write to. These tests are about media.
        recent_chats: [],
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
        apps: [
          {
            id: "polish-app",
            name: "Test client",
            kind: "personal",
            created_at: 1,
            disabled_at: null,
            can_mint_keys: true,
            plan: null,
            credentials: ["chat-key", "image-key"].map((id) => ({
              id,
              subject_kind: "app",
              subject_id: "polish-app",
              app_id: "polish-app",
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
        status: {
          state: "unavailable",
          reason_code: "unavailable_disabled",
          affected_capability_ids: ["image", "video"],
          counts: { total: 3, available: 1, affected: 2 },
        },
        receipts: [],
      }),
  );
  await page.route("**/admin/api/setup-progress", (route) =>
    fulfilJson(route, { complete: true, steps: [] }),
  );
  await page.route("**/me/api/media?limit=8", (route) =>
    fulfilJson(route, { assets: [] }),
  );
  return home;
}

test("Home omits greeting when the API display name is an email", async ({
  page,
  request,
}) => {
  await fixture(page, request, "tim@example.test");
  await page.goto("/");
  await expect(page.locator("main")).not.toContainText("Hello,");
});

test("Home recent media carries content while requests stay in receipts", async ({
  page,
  request,
}) => {
  const home = await fixture(page, request);
  await page.route("**/me/api/home", (route) =>
    fulfilJson(route, {
        ...home,
        recent_chats: [],
        receipts: [
          {
            request_id: "polish-image-receipt",
            intent: "image.analyze",
            requested_model: "image.analyze.face-likeness",
            status: "succeeded",
          },
        ],
      }),
  );
  await page.route("**/me/api/media?limit=8", (route) =>
    fulfilJson(route, {
        assets: [
          {
            asset_id: "polish-audio",
            kind: "audio",
            model: "audio.generate.ace-step-1.5-2b-base",
            prompt: "A river moves beneath the willow trees",
            download_url: "/signed/polish-audio",
          },
        ],
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
    fulfilJson(route, {
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
      }),
  );
  await page.route("**/preview-test.mp4", (route) =>
    fulfilBytes(route, { body: videoBytes, contentType: "video/mp4" }),
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
      fulfilJson(route, {
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
        }),
    );
    await page.goto("/");
    // A link is named by what it shows: its kind, and its prompt when it has one.
    await expect(page.locator('main a[href*="asset=empty-prompt"]')).toHaveAccessibleName(/Image/);
    const longCard = page.locator('main a[href*="asset=long-prompt"]');
    await expect(longCard).toHaveAccessibleName(new RegExp(prompt.slice(0, 40)));
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
    fulfilJson(route, {
        assets: [
          {
            asset_id: "broken",
            kind: "image",
            prompt: null,
            download_url: "/azizam-home-broken.png",
          },
        ],
        has_more: false,
      }),
  );
  await page.route("**/azizam-home-broken.png", (route) =>
    fulfilBytes(route, { status: 404, body: "Unavailable" }),
  );
  await page.goto("/");
  await expect(
    page.getByRole("img", { name: "Image preview unavailable" }),
  ).toBeVisible();
  await expect(page.locator('main a[href^="/library/?asset="]').first()).toHaveAccessibleName(/Image/);
});
