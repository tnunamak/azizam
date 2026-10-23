// Shared steps for the sign-in chooser's e2e tests (account switcher W4). Everything here is a real journey on
// the fixture gateway: members sign in and out of one browser, so the device's name list is what the gateway
// keeps, not a seed or a stub.
import type { Locator, Page } from "@playwright/test";
import { expect, type Isolated, type Member } from "./fixtures/isolation";

export const CHOOSER_HEADING = "Who’s signing in?";

/** The account menu's Sign out, from any app page. */
export async function signOut(page: Page) {
  await page.getByRole("button", { name: /^Account menu:/ }).click();
  await page.getByRole("menuitem", { name: "Sign out" }).click();
}

/** The plain form (S9): the way in while the browser remembers fewer than two people. */
export async function signInWithForm(page: Page, email: string, password: string) {
  await expect(page.getByLabel("Email")).toBeVisible();
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  // Home is exactly "/"; a pattern on the trailing slash would also match /sign-in/.
  await expect.poll(() => new URL(page.url()).pathname).toBe("/");
}

/**
 * The chooser is on the page, with its list. The gateway answers 60 list reads a minute per address, shared by every
 * test on its server, and a refused read shows the plain form. So the first look is at the page as it is, and a
 * refused list is read again from `address` (the sign-in address the page arrived at, so its status line comes back)
 * until the minute turns. Remove this wait once the base has AZIZAM_REMEMBERED_LIST_READS_PER_MINUTE (gateway/accounts.py
 * reads it; scripts/ui/serve.py sets it to 600 for the UI gateways), because no read is then refused.
 */
export async function expectChooser(page: Page, address = "/sign-in/") {
  let looks = 0;
  await expect(async () => {
    if (looks++ > 0) await page.goto(address);
    await expect(page.getByRole("heading", { level: 1, name: CHOOSER_HEADING })).toBeVisible({ timeout: 4_000 });
  }).toPass({ timeout: 90_000, intervals: [5_000] });
}

/** The chooser's rows, in order: each is a list item whose first button names the person. */
export function rows(page: Page): Locator {
  return page.getByRole("list").getByRole("listitem");
}

export type TwoPeople = {
  page: Page;
  robin: Member;
  lea: Member;
  robinName: string;
  leaName: string;
};

/**
 * Robin and Léa have both signed in on Robin's browser, so the browser remembers both and Léa is the most
 * recent. Léa's own browser (her invitation context) stays signed in. Returns Robin's browser page, on the
 * chooser after Léa signed out.
 */
export async function twoPeopleOnOneBrowser(isolated: Isolated): Promise<TwoPeople> {
  const robin = await isolated.member({ name: "Robin" });
  const lea = await isolated.member({ name: "Léa" });
  const page = robin.page;
  await page.goto("/");
  await signOut(page);
  await signInWithForm(page, robin.email, robin.password);
  await signOut(page);
  await signInWithForm(page, lea.email, lea.password);
  await page.goto("/");
  await signOut(page);
  await expectChooser(page, "/sign-in/?after=sign-out");
  return { page, robin, lea, robinName: isolated.name("Robin"), leaName: isolated.name("Léa") };
}
