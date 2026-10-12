import { type Page } from "@playwright/test";
import { expect, test } from "./fixtures/isolation";
import AxeBuilder from "@axe-core/playwright";
import { mkdir, copyFile } from "node:fs/promises";
import { workflowsPolishCatalog } from "./fixtures/workflows-polish";
import { fulfilJson, fulfilBytes } from "./fixtures/intercept";

// Screenshots go to the test's own output path inside the run directory. Only
// an explicit UI_POLISH_SCREEN_DIR publishes copies outside it.
const finalScreens = process.env.UI_POLISH_SCREEN_DIR;

// The existing Library test's deterministic media fixtures exercise presentation.
// Usage, activity, availability and model timing still come from the seeded API.
const image =
  "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 640 420'%3E%3Crect width='640' height='420' fill='%23e0f2fe'/%3E%3Ccircle cx='300' cy='210' r='110' fill='%230f766e'/%3E%3Cpath d='M365 210l120-80v160z' fill='%2314b8a6'/%3E%3Ccircle cx='265' cy='178' r='16' fill='white'/%3E%3C/svg%3E";
// One second of real, decodable PCM silence for the audio player.
const wav = Buffer.alloc(44 + 16_000);
wav.write("RIFF", 0);
wav.writeUInt32LE(wav.length - 8, 4);
wav.write("WAVEfmt ", 8);
wav.writeUInt32LE(16, 16);
wav.writeUInt16LE(1, 20);
wav.writeUInt16LE(1, 22);
wav.writeUInt32LE(8_000, 24);
wav.writeUInt32LE(16_000, 28);
wav.writeUInt16LE(2, 32);
wav.writeUInt16LE(16, 34);
wav.write("data", 36);
wav.writeUInt32LE(16_000, 40);
const media = {
  assets: [
    {
      asset_id: "polish_image",
      kind: "image",
      prompt: "A clean product render of a teal fish gateway on a desk",
      model: "image",
      created_at: 1_760_000_000,
      download_url: image,
      width: 1024,
      height: 768,
    },
    {
      asset_id: "polish_video",
      kind: "video",
      prompt: "Gateway status lights moving across a quiet panel",
      model: "video",
      created_at: 1_760_010_000,
      duration_s: 6,
    },
    {
      asset_id: "polish_audio",
      kind: "audio",
      prompt: "A calm spoken introduction for the gateway",
      model: "voice",
      created_at: 1_760_020_000,
      duration_s: 1,
      download_url: "/test-polish-audio.wav",
    },
  ],
  models: ["image", "video", "voice"],
  next_cursor: null,
};

async function inspect(page: Page, name: string) {
  await expect(
    name.startsWith("workflows-detail-")
      ? page.getByRole("dialog", { name: "Product render" })
      : name.startsWith("home-")
        ? page.getByRole("heading", { name: "Connect an app", level: 2 })
        : page.getByRole("heading", { level: 1 }),
  ).toBeVisible();
  await expect(page.locator('[aria-busy="true"]:visible')).toHaveCount(0);
  await expect(page.locator('[data-slot="skeleton"]:visible')).toHaveCount(0);
  await page.evaluate(async () => {
    await document.fonts.ready;
    await Promise.all(
      Array.from(document.images, (image) => image.decode().catch(() => {})),
    );
  });
  await expect
    .poll(
      () =>
        page.evaluate(() =>
          Array.from(document.images).every(
            (image) => image.complete && image.naturalWidth > 0,
          ),
        ),
      { message: `${name} images must decode` },
    )
    .toBe(true);
  if (name.startsWith("library-")) {
    // Exercise visible custom controls before the settled capture.
    const player = page.locator("article audio").first();
    await expect(player).toBeAttached();
    await expect(player).toHaveAttribute("preload", "none");
    const controls = player.locator("..");
    // Craft round 9: play is on the tile at every width (a 44 px target on a phone).
    await controls.getByRole("button", { name: /^Play / }).click();
    await expect
      .poll(() =>
        player.evaluate((audio) => (audio as HTMLAudioElement).duration),
      )
      .toBe(1);
    await controls.getByRole("button", { name: /^Pause / }).click();
    await expect(
      controls.getByRole("button", { name: /^Play / }),
    ).toBeVisible();
    await player.evaluate((audio) => {
      (audio as HTMLAudioElement).currentTime = 0;
    });
  }
  await expect
    .poll(
      () =>
        page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
      { message: `${name} must fit the viewport` },
    )
    .toBe(true);
  const serious = (await new AxeBuilder({ page }).analyze()).violations.filter(
    (violation) => ["serious", "critical"].includes(violation.impact ?? ""),
  );
  expect(serious, `${name} accessibility`).toEqual([]);
  const file = test.info().outputPath(`${name}.png`);
  await page.screenshot({
    path: file,
    fullPage: !name.startsWith("workflows-detail-"),
    animations: "disabled",
  });
  if (finalScreens) await copyFile(file, `${finalScreens}/${name}.png`);
}

test.beforeAll(async ({ request }) => {
  if (finalScreens) await mkdir(finalScreens, { recursive: true });
  const session = await (await request.get("/admin/api/session")).json();
  const profiles = await (await request.get("/admin/api/profiles")).json();
  const profile = profiles.profiles.find(
    (plan: { name: string }) => plan.name === "Local chat",
  );
  expect(profile).toBeTruthy();
  const result = await request.post("/admin/api/invites", {
    headers: { "x-azizam-csrf": session.actor.csrf_token },
    data: {
      profile_id: profile.id,
      email: `polish-tour+${process.env.UI_E2E_RUN_ID ?? "local"}-${process.pid}@example.test`,
      expires_in: 86400,
    },
  });
  expect(result.ok()).toBe(true);
});

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390] as const) {
    test(`settled product tour: ${theme} ${width}`, async ({ page }) => {
      test.setTimeout(180_000);
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      await page.addInitScript(
        (value) => localStorage.setItem("theme", value),
        theme,
      );
      await page.route("**/me/api/media?**", (route) =>
        fulfilJson(
          route,
          new URL(route.request().url()).searchParams.has("workflow")
            ? { assets: [], models: [], next_cursor: null }
            : media,
        ),
      );
      await page.route("**/me/api/workflows", (route) =>
        fulfilJson(route, workflowsPolishCatalog),
      );
      await page.route("**/test-polish-audio.wav", (route) =>
        fulfilBytes(route, { contentType: "audio/wav", body: wav }),
      );
      // Show the established Home presentation rather than the first-run
      // guide. Home tests separately exercise genuine setup-progress states.
      await page.route("**/admin/api/setup-progress", (route) =>
        fulfilJson(route, { complete: true, steps: [] }),
      );
      // Providers are optional in this build. This existing API fixture has no
      // spend or measurements, so the screenshot retains honest unknown states.
      await page.route("**/admin/api/providers", (route) =>
        fulfilJson(route, {
          providers: [
            {
              id: "openrouter",
              name: "OpenRouter",
              enabled: true,
              reachable: null,
              key_status: { state: "set" },
              spend: { status: "unknown" },
              caps: {},
            },
          ],
        }),
      );
      for (const name of [
        "home",
        "models",
        "workflows",
        "try",
        "library",
        "usage",
        "apps",
        "status",
        "engines",
        "providers",
        "settings",
      ]) {
        // Home is the root page; there is no /home/ page.
        await page.goto(name === "home" ? "/" : `/${name}/`);
        await inspect(page, `${name}-seeded-${theme}-${width}`);
      }
      await page.goto("/workflows/");
      await page.getByRole("button", { name: "Details" }).first().click();
      await inspect(page, `workflows-detail-seeded-${theme}-${width}`);
      await page.goto("/people/");
      for (const [name, tab] of [
        ["people", "People"],
        ["people-invites", "Invites"],
        ["people-plans", "Access plans"],
      ]) {
        await page.getByRole("tab", { name: tab, exact: true }).click();
        await inspect(page, `${name}-seeded-${theme}-${width}`);
      }
      await page.goto("/activity/");
      await page.getByRole("tab", { name: "Changes", exact: true }).click();
      await inspect(page, `activity-audit-seeded-${theme}-${width}`);
      await page.getByRole("tab", { name: "Requests", exact: true }).click();
      await inspect(page, `activity-requests-seeded-${theme}-${width}`);
    });
  }
}
