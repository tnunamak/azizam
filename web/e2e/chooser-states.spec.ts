// Every state of the sign-in page's chooser and person screens, at a phone and a desktop width (account
// switcher W4; DESIGN.md 4.5-4.9 and 10). The states come from real journeys on the fixture gateway, the same
// ones the chooser tests run, so each capture is a real screen. Each capture is saved to test-results/ under
// w4-shots/ and is asserted on its key line first, so a missing state fails the run.
//
// Two states are not captured here, and the page tests cover both. The single sign-on opening state needs a
// provider row, which the fixture gateway cannot make. The plain form with no name box for an owner who remembers
// nobody needs the owner's "On shared computers" choice, which the fixture gateway does not let a test set; W5
// adds that choice to Settings and its capture belongs with it.
import type { Page } from "@playwright/test";
import { expect, GLOBAL_STATE_TAG, ownerApi, test } from "./fixtures/isolation";
import { CHOOSER_HEADING, twoPeopleOnOneBrowser } from "./chooser-journey";

const WIDTHS = [
  { name: "390", width: 390, height: 844 },
  { name: "1440", width: 1440, height: 900 },
];

/** Saves the page as the state's capture, after its key line has shown. */
async function capture(page: Page, name: string, id: string) {
  await page.screenshot({ path: test.info().outputPath(`w4-shots/${name}-${id}.png`), fullPage: true });
}

/** The gateway's identifier for a remembered name, read from the same browser the page is in. */
async function tileOf(page: Page, name: string): Promise<string> {
  const list = (await (await page.request.get("/auth/api/remembered")).json()) as {
    people: { tile: string; name: string }[];
  };
  const person = list.people.find((entry) => entry.name === name);
  expect(person, `${name} is remembered on this browser`).toBeDefined();
  return person!.tile;
}

/** The status lines and lists that come from the sign-in page's address, on a browser that has signed people in. */
async function endingStates(page: Page, name: string, robinName: string) {
  await page.goto("/sign-in/");
  await expect(page.getByRole("heading", { level: 1, name: CHOOSER_HEADING })).toBeVisible();
  await capture(page, name, "list");

  // Switch person's record: the person who just left is named, and goes last.
  const robinTile = await tileOf(page, robinName);
  await page.evaluate(
    (value) => sessionStorage.setItem("azizam.handoff.last", value),
    JSON.stringify({ tile: robinTile }),
  );
  await page.goto("/sign-in/?after=switch");
  await expect(page.getByRole("status")).toHaveText(`${robinName} is signed out. To come back, sign in again.`);
  await capture(page, name, "list-after-switch");

  await page.goto("/sign-in/?after=provider-sign-out");
  // The fixture gateway names its provider "single sign-on", the label the owner sets when none is set.
  await expect(page.getByRole("status")).toContainText("You’re signed out of single sign-on.");
  await capture(page, name, "list-after-provider-sign-out");
}

for (const viewport of WIDTHS) {
  test.describe(`at ${viewport.name} px`, () => {
    test(`the list, its status lines, and the forget mode are captured`, async ({ isolated }) => {
      test.setTimeout(240_000);
      const { page, robinName, leaName } = await twoPeopleOnOneBrowser(isolated);
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      const name = viewport.name;

      await expect(page.getByRole("status")).toContainText("You’re signed out.");
      await capture(page, name, "list-after-sign-out");
      await endingStates(page, name, robinName);

      await page.goto("/sign-in/");
      await page.getByRole("button", { name: "Someone else" }).click();
      await expect(page.getByRole("button", { name: "Back to the list" })).toBeVisible();
      await capture(page, name, "form-from-list");

      await page.getByRole("button", { name: "Back to the list" }).click();
      await page.getByRole("button", { name: "Forget someone on this browser" }).click();
      await expect(page.getByRole("heading", { level: 1, name: "Forget someone on this browser" })).toBeVisible();
      await capture(page, name, "forget");

      await page.getByRole("button", { name: `Forget ${robinName} on this browser` }).click();
      await expect(page.getByRole("status")).toContainText(`${robinName} is forgotten on this browser.`);
      await capture(page, name, "forget-after-one");

      await page.getByRole("button", { name: `Forget ${leaName} on this browser` }).click();
      await expect(page.getByRole("status")).toHaveText("Everyone is forgotten on this browser.");
      await capture(page, name, "form-everyone-forgotten");
    });

    test(`the password screens are captured: wrong, no answer, too many tries, and a name the list no longer has`, async ({ isolated }) => {
      test.setTimeout(240_000);
      const { page, leaName, lea } = await twoPeopleOnOneBrowser(isolated);
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      const name = viewport.name;
      const alert = page.getByRole("alert").filter({ hasText: /\S/ });
      const submit = page.getByRole("button", { name: "Sign in" });

      await page.getByRole("button", { name: leaName, exact: true }).click();
      await expect(page.getByLabel(`Password for ${leaName}`)).toBeFocused();
      await capture(page, name, "password");

      await page.getByLabel(`Password for ${leaName}`).fill("not the password");
      await submit.click();
      await expect(alert).toHaveText("That password isn’t right. Try again.");
      await capture(page, name, "password-wrong");

      await page.route("**/auth/api/login", (route) => route.abort());
      await submit.click();
      await expect(alert).toHaveText("Couldn’t reach the gateway. Try again in a moment.");
      await capture(page, name, "password-no-answer");
      await page.unroute("**/auth/api/login");

      for (let attempt = 0; attempt < 15; attempt += 1) {
        await page.getByLabel(`Password for ${leaName}`).fill("not the password");
        const answered = page.waitForResponse((response) => response.url().endsWith("/auth/api/login"));
        await submit.click();
        if ((await answered).status() === 429) break;
      }
      await expect(alert).toHaveText("Too many tries. Wait 15 minutes, then try again.");
      await capture(page, name, "password-too-many");

      // Léa's name is removed on the gateway while this page still shows it: the next sign-in finds no such name.
      await page.goto("/sign-in/");
      await expect(page.getByRole("button", { name: leaName, exact: true })).toBeVisible();
      const leaTile = await tileOf(page, leaName);
      expect((await page.request.delete(`/auth/api/remembered/${encodeURIComponent(leaTile)}`)).ok()).toBe(true);
      await page.getByRole("button", { name: leaName, exact: true }).click();
      await page.getByLabel(`Password for ${leaName}`).fill(lea.password);
      await submit.click();
      await expect(page.getByRole("heading", { level: 1, name: `${leaName} can’t sign in from this list` })).toBeVisible();
      await capture(page, name, "password-gone");
    });

    test(`the list cannot be read, the plain form for a new browser, and the reset form are captured`, async ({ browser, baseURL }) => {
      test.setTimeout(120_000);
      const name = viewport.name;
      // A browser with no sign-in of its own: the plain form, with the name box on, and the reset form.
      const fresh = await browser.newContext({ baseURL, viewport: { width: viewport.width, height: viewport.height } });
      const page = await fresh.newPage();
      await page.goto("/sign-in/");
      await expect(page.getByRole("checkbox", { name: "Show my name on this browser next time" })).toBeChecked();
      await capture(page, name, "form");

      await page.route("**/auth/api/remembered", (route) => route.abort());
      await page.goto("/sign-in/");
      await expect(page.getByRole("alert").filter({ hasText: /\S/ })).toHaveText("Couldn’t reach the gateway.");
      await capture(page, name, "list-unavailable");
      await page.unroute("**/auth/api/remembered");

      // A new address, so the page loads again: the reset token is read when the page first loads.
      await page.goto("/sign-in/?view=reset#reset=capture-token");
      await expect(page.getByRole("heading", { level: 1, name: "Choose a new password" })).toBeVisible();
      await capture(page, name, "reset-password");
      await fresh.close();
    });
  });
}

