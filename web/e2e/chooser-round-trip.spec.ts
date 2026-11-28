// The switch round trip with several people signed in on one browser (account switcher W4; DESIGN.md sections 4.5,
// 4.7 and 4.10). A signs in, adds B with Add another person, and switches back to A from the account menu. A stays
// signed in while B signs in, so the switch back needs no password. Each step is a real sign-in on the fixture gateway.
import type { Page } from "@playwright/test";
import { expect, test } from "./fixtures/isolation";
import { signInWithForm, signOut } from "./chooser-journey";

const WIDTHS = [
  { name: "390", width: 390, height: 844 },
  { name: "1440", width: 1440, height: 900 },
];

/** The account menu's Add another person, from any app page. */
async function switchPerson(page: Page) {
  await page.getByRole("button", { name: /^Account menu:/ }).click();
  await page.getByRole("menuitem", { name: "Add another person" }).click();
}

async function capture(page: Page, name: string, id: string) {
  await page.screenshot({ path: test.info().outputPath(`switch-round-trip/${name}-${id}.png`), fullPage: true });
}

for (const viewport of WIDTHS) {
  test.describe(`switching from B back to A at ${viewport.name} px`, () => {
    test(`the account menu lists A and B, and choosing A needs no password`, async ({ isolated }) => {
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

      // A adds B. A stays signed in, so the sign-in page names nobody as signed out; it shows the plain form.
      await switchPerson(device);
      await expect(device.getByLabel("Email")).toBeVisible();
      await expect(device.getByText(`${aName} is signed out.`)).toHaveCount(0);
      await capture(device, viewport.name, "form-after-add");
      await signInWithForm(device, b.email, b.password);
      await expect.poll(() => new URL(device.url()).pathname).toBe("/");

      // The account menu lists both people: B is current, A is one choice away.
      await device.getByRole("button", { name: /^Account menu:/ }).click();
      await expect(device.getByRole("menuitem", { name: `${bName} · Current` })).toBeDisabled();
      const aItem = device.getByRole("menuitem", { name: aName, exact: true });
      await expect(aItem).toBeVisible();
      await capture(device, viewport.name, "menu-with-a-and-b");

      // Choosing A switches back with no password: A's sign-in is still live.
      await aItem.click();
      await expect.poll(() => new URL(device.url()).pathname).toBe("/");
      await expect(device.getByRole("button", { name: `Account menu: ${aName}` })).toBeVisible();
    });
  });
}
