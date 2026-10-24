import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import AxeBuilder from "@axe-core/playwright";
import { type Page } from "@playwright/test";
import { fulfilBytes, fulfilJson } from "./fixtures/intercept";
import { expect, test } from "./fixtures/isolation";

// A small picture, so a stubbed picture has something to draw.
const PNG = readFileSync(path.resolve(__dirname, "fixtures/avatar-picture.png"));
const SHOTS = process.env.AVATAR_SHOTS_DIR;
const SIGN_IN = "/me/api/profile/avatar/picture?kind=sign-in&v=1760000000";
const MADE = "/me/api/profile/avatar/picture?kind=generated&v=1760000100";

const views = [
  { name: "1440-light", width: 1440, height: 1000, theme: "light" },
  { name: "1440-dark", width: 1440, height: 1000, theme: "dark" },
  { name: "390-light", width: 390, height: 844, theme: "light" },
  { name: "390-dark", width: 390, height: 844, theme: "dark" },
] as const;

type Avatar = { choice: string; picture_url: string | null; sign_in_picture_url: string | null; made_picture_url: string | null };

// What a person sees in the picker. "noroute" is the fixture gateway as it is (no image route); the other two
// stub the route's answer. "failed" presses Make me an avatar: the make goes to the fixture gateway, which
// refuses it, so the refusal is real.
const states: { name: string; avatar: Avatar; route: boolean; press: boolean }[] = [
  {
    name: "noroute",
    avatar: { choice: "auto", picture_url: SIGN_IN, sign_in_picture_url: SIGN_IN, made_picture_url: null },
    route: false,
    press: false,
  },
  {
    name: "offer",
    avatar: { choice: "auto", picture_url: SIGN_IN, sign_in_picture_url: SIGN_IN, made_picture_url: null },
    route: true,
    press: false,
  },
  {
    name: "failed",
    avatar: { choice: "auto", picture_url: SIGN_IN, sign_in_picture_url: SIGN_IN, made_picture_url: null },
    route: true,
    press: true,
  },
  {
    name: "kept",
    avatar: { choice: "generated", picture_url: MADE, sign_in_picture_url: SIGN_IN, made_picture_url: MADE },
    route: true,
    press: false,
  },
];

async function openPicker(page: Page) {
  await page.goto("/");
  await page.getByRole("button", { name: /^Account menu/ }).first().click();
  await page.getByRole("menuitem", { name: "Change avatar" }).click();
  const dialog = page.getByRole("dialog", { name: "Your avatar" });
  await expect(dialog).toBeVisible();
  return dialog;
}

for (const view of views) {
  for (const state of states) {
    test(`the avatar picker shows the ${state.name} state, fits and passes axe at ${view.name}`, async ({ page }) => {
      await page.setViewportSize({ width: view.width, height: view.height });
      await page.emulateMedia({ colorScheme: view.theme, reducedMotion: "reduce" });
      await page.addInitScript((value) => localStorage.setItem("theme", value), view.theme);
      await page.route("**/me/api/session", async (route) => {
        const response = await route.fetch();
        const body = await response.json();
        if (body.user) body.user.avatar = { ...state.avatar, person_id: body.user.id };
        await fulfilJson(route, body, { response });
      });
      if (state.route)
        await page.route("**/me/api/profile/avatar/generator", (route) => fulfilJson(route, { available: true }));
      await page.route("**/me/api/profile/avatar/picture*", (route) =>
        fulfilBytes(route, { contentType: "image/png", body: PNG }),
      );
      const dialog = await openPicker(page);
      if (state.name === "noroute") {
        await expect(dialog.getByRole("button", { name: "Make me an avatar" })).toHaveCount(0);
      } else {
        await expect(dialog.getByRole("button", { name: "Make me an avatar" })).toBeVisible();
      }
      if (state.name === "kept") {
        await expect(dialog.getByRole("radio", { name: "Your generated avatar" })).toBeChecked();
        await expect(dialog.getByRole("radio", { name: "Use my sign-in picture" })).not.toBeChecked();
        await expect(dialog.getByRole("button", { name: "Remove generated avatar" })).toBeVisible();
      }
      if (state.press) {
        await dialog.getByRole("button", { name: "Make me an avatar" }).click();
        await expect(dialog.getByRole("alert")).toHaveText("We couldn't make your avatar. Try again.");
        // Nothing was kept: the sign-in picture is still the one shown, and no kept picture is offered.
        await expect(dialog.getByRole("radio", { name: "Use my sign-in picture" })).toBeChecked();
        await expect(dialog.getByRole("radio", { name: "Your generated avatar" })).toHaveCount(0);
      }
      const box = (await dialog.boundingBox())!;
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width).toBeLessThanOrEqual(view.width);
      expect(box.y + box.height).toBeLessThanOrEqual(view.height);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      if (view.width < 640)
        for (const label of await dialog.locator("label").all()) {
          const size = (await label.boundingBox())!;
          expect(size.height).toBeGreaterThanOrEqual(44);
        }
      const axe = await new AxeBuilder({ page }).include("[role='dialog']").analyze();
      expect(axe.violations.filter((v) => ["serious", "critical"].includes(v.impact ?? ""))).toEqual([]);
      if (SHOTS) {
        mkdirSync(SHOTS, { recursive: true });
        await page.screenshot({ path: path.join(SHOTS, `avatar-${state.name}-${view.name}.png`) });
      }
    });
  }
}
