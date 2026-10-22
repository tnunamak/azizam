// The sign-in page's chooser (account switcher W4; SECURITY.md section 9, AC3 restore, AC5, AC8). Each test
// signs members in and out of one browser, so the device's name list is real: a person is remembered by
// the sign-in that the page makes, not by a seed. Switch person is the W3 menu item; until it lands, the
// same browser record is made by Sign out, which is the other way to leave.
import type { Locator, Page } from "@playwright/test";
import { expect, test, type Isolated } from "./fixtures/isolation";

const HEADING = "Who’s signing in?";

/** The account menu's Sign out, from any app page. */
async function signOut(page: Page) {
  await page.getByRole("button", { name: /^Account menu:/ }).click();
  await page.getByRole("menuitem", { name: "Sign out" }).click();
}

/** The plain form (S9): the way in while the browser remembers fewer than two people. */
async function signInWithForm(page: Page, email: string, password: string) {
  await expect(page.getByLabel("Email")).toBeVisible();
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect.poll(() => new URL(page.url()).pathname).toBe("/");
}

/**
 * Robin and Léa have both signed in on Robin's browser, so the browser remembers both and Léa is the most
 * recent. Léa's own browser (her invitation context) stays signed in. Returns the page of Robin's browser,
 * now on the chooser after Léa signed out.
 */
async function twoPeopleOnOneBrowser(isolated: Isolated) {
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
  await expect(page.getByRole("heading", { level: 1, name: HEADING })).toBeVisible();
  return {
    page,
    robin,
    lea,
    robinName: isolated.name("Robin"),
    leaName: isolated.name("Léa"),
  };
}

/** The chooser's rows, in order: each is a list item whose first button names the person. */
function rows(page: Page): Locator {
  return page.getByRole("list").getByRole("listitem");
}

test("chooser: the people who signed in on this browser are listed, most recent first, with Someone else last", async ({
  isolated,
}) => {
  const { page, robinName, leaName } = await twoPeopleOnOneBrowser(isolated);

  await expect(page.getByRole("heading", { level: 1, name: HEADING })).toBeFocused();
  await expect(page).toHaveTitle("Who’s signing in? · Azizam");
  await expect(rows(page)).toHaveCount(3);
  await expect(rows(page).nth(0)).toContainText(leaName);
  await expect(rows(page).nth(1)).toContainText(robinName);
  await expect(rows(page).nth(2)).toContainText("Someone else");
  await expect(page.getByRole("button", { name: "Forget someone on this browser" })).toBeVisible();
});

test("sign out offers Forget for the person who left, and forgetting removes only their name", async ({
  isolated,
}) => {
  const { page, lea, leaName, robinName } = await twoPeopleOnOneBrowser(isolated);
  // Léa's own browser: signed in before, and it stays signed in after this browser forgets her.
  const elsewhere = await lea.context.request.get("/me/api/session");
  expect((await elsewhere.json()).authenticated).toBe(true);

  const status = page.getByRole("status");
  await expect(status).toContainText("You’re signed out.");
  await status.getByRole("button", { name: `Forget ${leaName} on this browser` }).click();

  await expect(status).toContainText(`${leaName} is forgotten on this browser.`);
  // One name is left, so the plain form takes the list's place, and Robin's name is still there.
  await expect(page.getByLabel("Email")).toBeVisible();
  await expect(page.getByRole("button", { name: robinName })).toHaveCount(0);
  expect((await (await lea.context.request.get("/me/api/session")).json()).authenticated).toBe(true);
});

test("forget mode: rows are not buttons, and focus moves to the next Forget, then Done", async ({ isolated }) => {
  const { page, leaName, robinName } = await twoPeopleOnOneBrowser(isolated);

  await page.getByRole("button", { name: "Forget someone on this browser" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Forget someone on this browser" })).toBeFocused();
  await expect(page.getByRole("button", { name: leaName, exact: true })).toHaveCount(0);

  await page.getByRole("button", { name: `Forget ${leaName} on this browser` }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("button", { name: `Forget ${robinName} on this browser` })).toBeFocused();

  // The last name goes: the plain form takes over and says so.
  await page.keyboard.press("Enter");
  await expect(page.getByRole("status")).toContainText("Everyone is forgotten on this browser.");
  await expect(page.getByLabel("Email")).toBeVisible();
});

test("keyboard: the chooser journey with keys only, from a person's row to Home", async ({ isolated }) => {
  const { page, lea, leaName } = await twoPeopleOnOneBrowser(isolated);

  const heading = page.getByRole("heading", { level: 1, name: HEADING });
  await expect(heading).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: leaName, exact: true })).toBeFocused();
  await page.keyboard.press("Enter");

  const password = page.getByLabel(`Password for ${leaName}`);
  await expect(page.getByRole("heading", { level: 1, name: leaName })).toBeVisible();
  await expect(password).toBeFocused();
  await expect(page).toHaveTitle(`${leaName} · Sign in · Azizam`);
  await page.keyboard.type(lea.password);
  await page.keyboard.press("Enter");

  await expect.poll(() => new URL(page.url()).pathname).toBe("/");
});

test("errors: only a wrong password marks the field invalid; too many tries and no answer are form-level alerts", async ({
  isolated,
}) => {
  const { page, leaName } = await twoPeopleOnOneBrowser(isolated);
  await page.getByRole("button", { name: leaName, exact: true }).click();
  const password = page.getByLabel(`Password for ${leaName}`);
  await password.fill("not the password");
  const alert = page.getByRole("alert");

  await page.route("**/auth/api/login", (route) =>
    route.fulfill({
      status: 401,
      contentType: "application/json",
      body: JSON.stringify({ error: { code: "invalid_login", message: "Wrong" } }),
    }),
  );
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(alert).toHaveText("That password isn’t right. Try again.");
  await expect(password).toHaveAttribute("aria-invalid", "true");
  await expect(password).toBeFocused();
  await page.unroute("**/auth/api/login");

  await page.route("**/auth/api/login", (route) => route.abort());
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(alert).toHaveText("Couldn’t reach the gateway. Try again in a moment.");
  await expect(password).not.toHaveAttribute("aria-invalid", "true");
  await page.unroute("**/auth/api/login");

  await page.route("**/auth/api/login", (route) =>
    route.fulfill({
      status: 429,
      contentType: "application/json",
      body: JSON.stringify({ error: { code: "login_rate_limited", message: "Too many attempts. Try again in 15 minutes." } }),
    }),
  );
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(alert).toHaveText("Too many tries. Wait 15 minutes, then try again.");
  await expect(password).not.toHaveAttribute("aria-invalid", "true");
  await page.unroute("**/auth/api/login");
});

test("restore: reopening the previous person's deep link after switch shows sign-in, then Home for the next person, never the link", async ({
  isolated,
}) => {
  const robin = await isolated.member({ name: "Robin" });
  const lea = await isolated.member({ name: "Léa" });
  const page = robin.page;
  await page.goto("/");
  await signOut(page);
  await expect(page).toHaveURL(/\/sign-in\/?$/);

  // The browser restores the previous person's page: the app sends it to sign-in with that page as the link.
  await page.goto("/usage/");
  await expect(page).toHaveURL(/\/sign-in\//);
  await signInWithForm(page, lea.email, lea.password);

  await expect.poll(() => new URL(page.url()).pathname).toBe("/");
});

test("sign out everywhere ends the other browser's session, and that browser keeps its own name list", async ({
  isolated,
  browser,
  baseURL,
}) => {
  const robin = await isolated.member({ name: "Robin" });
  const page = robin.page;
  // Robin signs in again on a second browser, which remembers Robin there.
  const elsewhere = await browser.newContext({ baseURL });
  const other = await elsewhere.newPage();
  await other.goto("/sign-in/");
  await signInWithForm(other, robin.email, robin.password);
  const listBefore = await (await elsewhere.request.get("/auth/api/remembered")).json();
  expect(listBefore.people).toHaveLength(1);

  await page.goto("/account/");
  await page.getByRole("button", { name: "Sign out everywhere" }).click();
  const dialog = page.getByRole("dialog", { name: "Sign out everywhere?" });
  await dialog.getByRole("button", { name: "Sign out everywhere" }).click();

  await expect(page).toHaveURL(/\/sign-in\//);
  await expect(page.getByRole("status")).toContainText("You’re signed out everywhere.");
  expect((await (await elsewhere.request.get("/me/api/session")).json()).authenticated).toBe(false);
  expect(await (await elsewhere.request.get("/auth/api/remembered")).json()).toEqual(listBefore);
  await elsewhere.close();
});
