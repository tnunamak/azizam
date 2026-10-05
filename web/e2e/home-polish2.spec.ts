import {
  test,
  expect,
  type Page,
  type APIRequestContext,
} from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import type { HomeSnapshot } from "../src/app/home/home-api";

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
  await page.goto("/ui/models/");
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
      reason: "Chat is available.",
    },
    {
      id: "image",
      operation: "images",
      available: false,
      status: "unavailable",
      reason: "The image service is disabled.",
    },
    {
      id: "video",
      operation: "videos",
      available: false,
      status: "unavailable",
      reason: "The video service is unavailable.",
    },
    {
      id: "tts",
      operation: "speech",
      available: true,
      status: "ready",
      reason: "Voice is available.",
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
            reason: alias.reason,
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
          reason:
            "Images are unavailable: the service is disabled, with 1 other capability affected.",
        },
        receipts: [],
      },
    }),
  );
  await page.route("**/admin/api/setup-progress", (route) =>
    route.fulfill({ json: { complete: true, steps: [] } }),
  );
  await page.route("**/me/api/media?limit=4", (route) =>
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
      await page.goto("/ui/home/");
      await expect(page.locator("main")).not.toContainText("Hello,");
      await expect(
        page.getByText(
          "Images are unavailable: the service is disabled, with 1 other capability affected.",
          { exact: true },
        ),
      ).toBeVisible();
      await expect(
        page.getByText("Some services are unavailable or not measured yet.", {
          exact: true,
        }),
      ).toHaveCount(0);
      await expect(
        page.getByText("Here’s how to use your AI.", { exact: true }),
      ).toHaveCount(0);
      await expect(page.locator('a[href*="/models/?capability="]')).toHaveCount(
        4,
      );
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
  await page.goto("/ui/home/");
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
  await page.goto("/ui/home/");
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
  await page.route("**/me/api/media?limit=4", (route) =>
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
  await page.goto("/ui/home/");
  await expect(
    page.getByRole("heading", { name: "Recent", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Recent requests", { exact: true })).toHaveCount(
    0,
  );
  await expect(page.locator('a[href="/r/polish-image-receipt"]')).toHaveCount(
    0,
  );
  await expect(
    page.getByText("A river moves beneath the willow trees"),
  ).toBeVisible();
  await expect(
    page.getByLabel("Play A river moves beneath the willow trees"),
  ).toHaveAttribute("controls");
  await expect(page.locator("main")).not.toContainText(
    "audio.generate.ace-step-1.5-2b-base",
  );
});
