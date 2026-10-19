import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import AxeBuilder from "@axe-core/playwright";
import { type Page } from "@playwright/test";
import { fulfilBytes, fulfilJson } from "./fixtures/intercept";
import { expect, test } from "./fixtures/isolation";

// A small picture, so the stubbed sign-in picture has something to draw.
const PNG = readFileSync(path.resolve(__dirname, "fixtures/avatar-picture.png"));
const SHOTS = process.env.AVATAR_SHOTS_DIR;

// The picker, at the sizes and themes the page ships in. The account button sits in the sidebar on a wide
// screen and in the phone header on a narrow one.
const views = [
  { name: "1440-light", width: 1440, height: 1000, theme: "light" },
  { name: "1440-dark", width: 1440, height: 1000, theme: "dark" },
  { name: "390-light", width: 390, height: 844, theme: "light" },
  { name: "390-dark", width: 390, height: 844, theme: "dark" },
] as const;

async function openPicker(page: Page) {
  await page.goto("/");
  await page.getByRole("button", { name: /^Account menu/ }).first().click();
  await page.getByRole("menuitem", { name: "Change avatar" }).click();
  const dialog = page.getByRole("dialog", { name: "Your avatar" });
  await expect(dialog).toBeVisible();
  return dialog;
}

const discOf = (scope: ReturnType<Page["locator"]>) =>
  scope.locator("[data-avatar-selection] > span").first();

test("a person changes their avatar from the account menu and it sticks", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const before = await page.request.get("/me/api/profile/avatar");
  expect(before.ok()).toBe(true);
  const start = await before.json();
  expect(start.person_id).toMatch(/^usr_/);
  try {
    const dialog = await openPicker(page);
    const chosen = dialog.getByRole("radio", { name: /Plum/ });
    await dialog.getByText("Plum", { exact: true }).click();
    await expect(chosen).toBeChecked();
    await expect(page.getByText("We couldn't save your avatar")).toHaveCount(0);
    await expect
      .poll(async () => (await (await page.request.get("/me/api/profile/avatar")).json()).choice)
      .toBe("v7");
    await dialog.getByRole("button", { name: "Done" }).click();
    await expect(dialog).toBeHidden();
    // Focus returns to the account button.
    await expect(page.getByRole("button", { name: /^Account menu/ }).first()).toBeFocused();
    // It is the same on a fresh load, in the sidebar and on People.
    await page.reload();
    const trigger = page.getByRole("button", { name: /^Account menu/ }).first();
    await expect(discOf(trigger)).toHaveCSS("background-image", /#5C3F61|rgb\(92, 63, 97\)/);
    await page.goto("/people/");
    await expect(page.getByRole("heading", { name: "People" })).toBeVisible();
    await expect(
      page.locator("[data-slot='person-mark'] > span").first(),
    ).toHaveCSS("background-image", /#5C3F61|rgb\(92, 63, 97\)/);
  } finally {
    // The lane owner is shared by the other tests of this server: put the default back.
    const session = await (await page.request.get("/me/api/session")).json();
    await page.request.put("/me/api/profile/avatar", {
      data: { choice: "auto" },
      headers: { "x-azizam-user-action": "1", "x-azizam-csrf": session.csrf_token },
    });
  }
});

test("the avatar write refuses anything but one of the listed words", async ({ page }) => {
  await page.goto("/");
  const session = await (await page.request.get("/me/api/session")).json();
  const headers = { "x-azizam-user-action": "1", "x-azizam-csrf": session.csrf_token };
  const put = (data: unknown, extra: Record<string, string> = headers) =>
    page.request.put("/me/api/profile/avatar", { data, headers: extra });
  expect((await put({ choice: "v9" })).status()).toBe(400);
  expect((await put({ choice: "https://evil.example/x.png" })).status()).toBe(400);
  expect((await put({ choice: "v1", picture_url: "https://evil.example/x.png" })).status()).toBe(400);
  expect((await put({ choice: "v1" }, {})).status()).toBe(403);
  expect((await put({ choice: "picture" })).status()).toBe(409);
});

for (const view of views) {
  test(`the avatar picker fits and passes axe at ${view.name}`, async ({ page }) => {
    await page.setViewportSize({ width: view.width, height: view.height });
    await page.emulateMedia({ colorScheme: view.theme, reducedMotion: "reduce" });
    await page.addInitScript((value) => localStorage.setItem("theme", value), view.theme);
    // The fixture owner signed in with a password, so the provider shared no picture. Stub one
    // (and its bytes) to show the picture option the way an identity-provider sign-in does.
    await page.route("**/me/api/session", async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      if (body.user)
        body.user.avatar = {
          choice: "auto",
          person_id: body.user.id,
          picture_url: "/me/api/profile/avatar/picture?v=1760000000",
        };
      await fulfilJson(route, body, { response });
    });
    await page.route("**/me/api/profile/avatar/picture*", (route) =>
      fulfilBytes(route, { contentType: "image/png", body: PNG }),
    );
    const dialog = await openPicker(page);
    await expect(dialog.getByRole("radio")).toHaveCount(9);
    const box = (await dialog.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(view.width);
    expect(box.y + box.height).toBeLessThanOrEqual(view.height);
    // No sideways scroll, and every choice is a touch target of 44 or more on a phone.
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
      await page.screenshot({ path: path.join(SHOTS, `avatar-picker-${view.name}.png`) });
    }
  });
}
