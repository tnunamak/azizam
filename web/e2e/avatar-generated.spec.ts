import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import AxeBuilder from "@axe-core/playwright";
import { type Page } from "@playwright/test";
import { fulfilBytes, fulfilJson } from "./fixtures/intercept";
import { expect, test } from "./fixtures/isolation";

// The sign-in picture the stub provider shares, and the picture the gateway made. They differ, so a capture
// shows which one is on screen. The made one is the static style sample the offer shows beside its button.
const SIGN_IN_PNG = readFileSync(path.resolve(__dirname, "fixtures/avatar-picture.png"));
const MADE_SVG = readFileSync(path.resolve(__dirname, "../public/avatar-style-sample.svg"));
const SHOTS = process.env.AVATAR_SHOTS_DIR;
const SIGN_IN = "/me/api/profile/avatar/picture?kind=sign-in&v=1760000000";
const MADE = "/me/api/profile/avatar/picture?kind=generated&v=1760000100";

const views = [
  { name: "1440-light", width: 1440, height: 1000, theme: "light" },
  { name: "1440-dark", width: 1440, height: 1000, theme: "dark" },
  { name: "390-light", width: 390, height: 844, theme: "light" },
  { name: "390-dark", width: 390, height: 844, theme: "dark" },
] as const;

type Avatar = {
  choice: string;
  picture_url: string | null;
  sign_in_picture_url: string | null;
  made_picture_url: string | null;
  generating?: boolean;
};

const NO_PICTURE: Avatar = { choice: "auto", picture_url: null, sign_in_picture_url: null, made_picture_url: null };
const SIGN_IN_ONLY: Avatar = { choice: "auto", picture_url: SIGN_IN, sign_in_picture_url: SIGN_IN, made_picture_url: null };
const KEPT: Avatar = { choice: "generated", picture_url: MADE, sign_in_picture_url: null, made_picture_url: MADE };

// What a person sees. "noroute" is the fixture gateway as it is (no image route). "offer" has a route and no
// picture of their own. "generating" is the automatic picture while it is made. "failed" presses Make an avatar:
// the make goes to the fixture gateway, which refuses it, so the refusal is real.
const states: { name: string; avatar: Avatar; route: boolean; open: boolean; press?: "fail" }[] = [
  { name: "initials", avatar: NO_PICTURE, route: true, open: false },
  { name: "noroute", avatar: NO_PICTURE, route: false, open: true },
  { name: "offer", avatar: NO_PICTURE, route: true, open: true },
  { name: "generating", avatar: { ...NO_PICTURE, generating: true }, route: true, open: true },
  { name: "kept", avatar: KEPT, route: true, open: true },
  { name: "failed", avatar: NO_PICTURE, route: true, open: true, press: "fail" },
  { name: "signin", avatar: SIGN_IN_ONLY, route: true, open: true },
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
        // The automatic picture's request reads the session first, so such a read can still be in flight when the test
        // ends. Its response is then disposed, and nothing is waiting for the answer.
        const response = await route.fetch().catch(() => undefined);
        const body = await response?.json().catch(() => undefined);
        if (!response || !body) return;
        if (body.user) body.user.avatar = { ...state.avatar, person_id: body.user.id };
        await fulfilJson(route, body, { response }).catch(() => undefined);
      });
      if (state.route)
        await page.route("**/me/api/profile/avatar/generator", (route) => fulfilJson(route, { available: true }));
      // The page reads the avatar while a picture is made. Refusing that read keeps the state the capture shows.
      if (state.avatar.generating) await page.route("**/me/api/profile/avatar", (route) => route.abort());
      await page.route("**/me/api/profile/avatar/picture*", (route) =>
        route.request().url().includes("kind=generated")
          ? fulfilBytes(route, { contentType: "image/svg+xml", body: MADE_SVG })
          : fulfilBytes(route, { contentType: "image/png", body: SIGN_IN_PNG }),
      );

      if (!state.open) {
        // The default before the automatic picture: the account button shows the initial, with no picture.
        await page.goto("/");
        const trigger = page.getByRole("button", { name: /^Account menu/ }).first();
        await expect(trigger.locator("[data-avatar-selection]").first()).toHaveAttribute("data-avatar-selection", "generated");
        if (SHOTS) {
          mkdirSync(SHOTS, { recursive: true });
          await page.screenshot({ path: path.join(SHOTS, `avatar-${state.name}-${view.name}.png`) });
        }
        return;
      }

      const dialog = await openPicker(page);
      const generatedRow = dialog.getByRole("radio", { name: "Generated" });
      const initialsRow = dialog.getByRole("radio", { name: "Initials" });
      if (state.name === "noroute") {
        await expect(dialog.getByRole("button", { name: "Make an avatar" })).toHaveCount(0);
        await expect(generatedRow).toHaveCount(0);
        await expect(initialsRow).toHaveAttribute("aria-checked", "true");
      }
      if (state.name === "offer") {
        await expect(dialog.getByRole("button", { name: "Make an avatar" })).toBeVisible();
        await expect(dialog.getByText("Takes about 20 seconds. Your current picture stays until you pick a new one.")).toBeVisible();
        await expect(dialog.locator("img[src='/avatar-style-sample.svg']")).toBeVisible();
      }
      if (state.name === "generating") {
        await expect(dialog.getByRole("status")).toHaveText("Making your avatar…");
        await expect(generatedRow).toBeDisabled();
      }
      if (state.name === "kept") {
        await expect(generatedRow).toHaveAttribute("aria-checked", "true");
        await expect(initialsRow).toHaveAttribute("aria-checked", "false");
        await expect(dialog.getByRole("button", { name: "Make a new one" })).toBeVisible();
        await expect(dialog.getByRole("button", { name: "Delete generated avatar" })).toBeVisible();
      }
      if (state.name === "signin") {
        await expect(dialog.getByRole("radio", { name: /Sign-in picture/ })).toHaveAttribute("aria-checked", "true");
        await expect(dialog.getByText("Azizam gets this picture from your sign-in provider.")).toBeVisible();
      }
      if (state.press) {
        await dialog.getByRole("button", { name: "Make an avatar" }).click();
        await expect(dialog.getByText("Couldn't make an avatar just now. Your current one is unchanged.")).toBeVisible();
        await expect(dialog.getByRole("button", { name: "Try again" })).toBeVisible();
        // Nothing was kept: the initials are still the avatar, and no made picture is offered.
        await expect(initialsRow).toHaveAttribute("aria-checked", "true");
        await expect(generatedRow).toBeDisabled();
      }
      await expect(dialog.getByText("People on this gateway see the picture you choose.")).toBeVisible();
      const box = (await dialog.boundingBox())!;
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width).toBeLessThanOrEqual(view.width);
      expect(box.y + box.height).toBeLessThanOrEqual(view.height);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      if (view.width < 640)
        for (const row of await dialog.getByRole("radio").all()) {
          const size = (await row.boundingBox())!;
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
