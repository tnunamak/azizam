// The switch round trip (account switcher W4; DESIGN.md sections 4.5, 4.7 and 4.10). A signs in on a browser, switches
// to B, then switches back to A. The browser remembers A, so the chooser lists A with a name and an avatar only, and
// choosing A asks for A's own password. Each step is a real sign-in on the fixture gateway, not a seeded list.
import type { Page } from "@playwright/test";
import { expect, test } from "./fixtures/isolation";
import { CHOOSER_HEADING, rows, signInWithForm, signOut } from "./chooser-journey";

const WIDTHS = [
  { name: "390", width: 390, height: 844 },
  { name: "1440", width: 1440, height: 900 },
];

/** The account menu's Switch person, from any app page. */
async function switchPerson(page: Page) {
  await page.getByRole("button", { name: /^Account menu:/ }).click();
  await page.getByRole("menuitem", { name: "Switch person" }).click();
}

async function capture(page: Page, name: string, id: string) {
  await page.screenshot({ path: test.info().outputPath(`switch-round-trip/${name}-${id}.png`), fullPage: true });
}

for (const viewport of WIDTHS) {
  test.describe(`switching from B back to A at ${viewport.name} px`, () => {
    test(`the chooser lists A with a name and an avatar, and A's password opens the account`, async ({ isolated }) => {
      const a = await isolated.member({ name: "Ana" });
      const b = await isolated.member({ name: "Ben" });
      const aName = isolated.name("Ana");
      const bName = isolated.name("Ben");
      const device = a.page;
      await device.setViewportSize({ width: viewport.width, height: viewport.height });

      // A's browser starts signed in (isolated.member). A signs out and signs in on the form, so this browser remembers
      // A the way a sign-in on the page does. Nobody is remembered yet, so the plain form is the way in.
      await device.goto("/");
      await signOut(device);
      await signInWithForm(device, a.email, a.password);

      // A switches to B. A is the one who just left, so the sign-in page says so.
      await switchPerson(device);
      await expect(device.getByRole("status")).toContainText(`${aName} is signed out.`);
      await expect(device.getByLabel("Email")).toBeVisible();
      await capture(device, viewport.name, "a-signed-out-after-switch");
      await signInWithForm(device, b.email, b.password);

      // B switches back to A. Two people are remembered now, so the chooser lists them: A first, B last.
      await switchPerson(device);
      await expect(device.getByRole("heading", { level: 1, name: CHOOSER_HEADING })).toBeVisible();
      await expect(rows(device)).toHaveCount(3);
      await expect(rows(device).nth(0)).toContainText(aName);
      await expect(rows(device).nth(1)).toContainText(bName);
      await expect(rows(device).nth(2)).toContainText("Someone else");

      // A is a password person: the row has a name and an avatar, and no second line (no provider, no address).
      const aRow = rows(device).nth(0);
      await expect(aRow.locator("[data-avatar-selection]")).toHaveCount(1);
      await expect(aRow).not.toContainText("single sign-on");
      await expect(aRow).not.toContainText("…");
      await capture(device, viewport.name, "chooser-with-a");

      // Choosing A asks for A's password, and that opens the account on Home.
      await device.getByRole("button", { name: aName, exact: true }).click();
      await expect(device.getByRole("heading", { level: 1, name: aName })).toBeVisible();
      await capture(device, viewport.name, "password-for-a");
      await device.getByLabel(`Password for ${aName}`).fill(a.password);
      await device.getByRole("button", { name: "Sign in" }).click();
      await expect.poll(() => new URL(device.url()).pathname).toBe("/");
      await expect(device.getByRole("button", { name: /^Account menu:/ })).toBeVisible();
    });
  });
}
