// The sign-in page's chooser (account switcher W4; SECURITY.md section 9, AC3 restore, AC5, AC8). Each test
// signs members in and out of one browser, so the device's name list is real: a person is remembered by
// the sign-in that the page makes, not by a seed. Switch person is the W3 menu item; until it lands, the
// same browser record is made by Sign out, which is the other way to leave.
import { expect, test } from "./fixtures/isolation";
import {
  CHOOSER_HEADING,
  rows,
  signInWithForm,
  signOut,
  twoPeopleOnOneBrowser,
} from "./chooser-journey";

// The chooser's reads of the remembered list share one budget with every other test on the same gateway: 60 reads a
// minute per address (gateway/accounts.py, throttle_remembered), and every worker in the gate comes from 127.0.0.1.
// The tests in this file run one at a time, so their reads never add up with each other.
test.describe.configure({ mode: "serial" });

test("chooser: the people who signed in on this browser are listed, most recent first, with Someone else last", async ({
  isolated,
}) => {
  const { page, robinName, leaName } = await twoPeopleOnOneBrowser(isolated);

  await expect(page.getByRole("heading", { level: 1, name: CHOOSER_HEADING })).toBeFocused();
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
  expect((await (await lea.context.request.get("/me/api/session")).json()).authenticated).toBe(true);

  const status = page.getByRole("status");
  await expect(status).toContainText("You’re signed out.");
  await status.getByRole("button", { name: `Forget ${leaName} on this browser` }).click();

  await expect(status).toContainText(`${leaName} is forgotten on this browser.`);
  // One name is left, so the plain form takes the list's place, and Robin's name is not a button.
  await expect(page.getByLabel("Email")).toBeVisible();
  await expect(page.getByRole("button", { name: robinName })).toHaveCount(0);
  expect((await (await lea.context.request.get("/me/api/session")).json()).authenticated).toBe(true);
});

test("a status line is one tinted box, and its Forget offer sits on its own line, left-aligned and 44 px tall", async ({
  isolated,
}) => {
  const { page, leaName, robinName } = await twoPeopleOnOneBrowser(isolated);

  // After a sign-out: the box, its spacing, and the offer on a line of its own under the sentence.
  const status = page.getByRole("status");
  await expect(status).toContainText("You’re signed out.");
  const sentence = status.locator("p").first();
  const offer = status.getByRole("button", { name: `Forget ${leaName} on this browser` });
  await expect(offer).toBeVisible();
  const boxStyle = await status.evaluate((element) => {
    const computed = getComputedStyle(element);
    return { background: computed.backgroundColor, padding: computed.padding, marginBottom: computed.marginBottom };
  });
  expect(boxStyle.background).not.toBe("rgba(0, 0, 0, 0)");
  expect(boxStyle.padding).toBe("12px 14px");
  expect(boxStyle.marginBottom).toBe("24px");
  const sentenceBox = await sentence.boundingBox();
  const offerBox = await offer.boundingBox();
  expect(offerBox!.y).toBeGreaterThanOrEqual(sentenceBox!.y + sentenceBox!.height);
  expect(Math.round(offerBox!.x)).toBe(Math.round(sentenceBox!.x));
  expect(offerBox!.height).toBeGreaterThanOrEqual(44);
  expect(await offer.evaluate((element) => getComputedStyle(element).textAlign)).toBe("left");

  // Forget mode reports a forget in the same box, in the space that is there before any forget.
  await page.getByRole("button", { name: "Forget someone on this browser" }).click();
  await page.getByRole("button", { name: `Forget ${robinName} on this browser` }).click();
  const forgotten = page.getByRole("status");
  await expect(forgotten).toContainText(`${robinName} is forgotten on this browser.`);
  expect(await forgotten.evaluate((element) => getComputedStyle(element).backgroundColor)).not.toBe(
    "rgba(0, 0, 0, 0)",
  );
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

  const heading = page.getByRole("heading", { level: 1, name: CHOOSER_HEADING });
  await expect(heading).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: leaName, exact: true })).toBeFocused();
  await page.keyboard.press("Enter");

  await expect(page.getByRole("heading", { level: 1, name: leaName })).toBeVisible();
  await expect(page.getByLabel(`Password for ${leaName}`)).toBeFocused();
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
  // Next’s route announcer is an empty alert on every page: the form’s alert is the one with text.
  const alert = page.getByRole("alert").filter({ hasText: /\S/ });
  const submit = page.getByRole("button", { name: "Sign in" });

  // A wrong password is the field’s error: the gateway answers 401.
  await submit.click();
  await expect(alert).toHaveText("That password isn’t right. Try again.");
  await expect(password).toHaveAttribute("aria-invalid", "true");
  await expect(password).toBeFocused();

  // No answer: the request never completes, so the field is not marked.
  await page.route("**/auth/api/login", (route) => route.abort());
  await submit.click();
  await expect(alert).toHaveText("Couldn’t reach the gateway. Try again in a moment.");
  await expect(password).not.toHaveAttribute("aria-invalid", "true");
  await page.unroute("**/auth/api/login");

  // Too many tries: the gateway’s own limit answers 429 after a run of wrong passwords.
  for (let attempt = 0; attempt < 15; attempt += 1) {
    await password.fill("not the password");
    const answered = page.waitForResponse((response) => response.url().endsWith("/auth/api/login"));
    await submit.click();
    if ((await answered).status() === 429) break;
  }
  await expect(alert).toHaveText("Too many tries. Wait 15 minutes, then try again.");
  await expect(password).not.toHaveAttribute("aria-invalid", "true");
  await expect(submit).toBeDisabled();
});

test("restore: reopening the previous person's deep link after switch shows sign-in, then Home for the next person, never the link", async ({
  isolated,
}) => {
  const robin = await isolated.member({ name: "Robin" });
  const lea = await isolated.member({ name: "Léa" });
  const page = robin.page;
  await page.goto("/");
  await signOut(page);
  await expect(page).toHaveURL(/\/sign-in\//);

  // The browser restores the previous person's page: the app sends it to sign-in with that page as the link.
  await page.goto("/usage/");
  await expect(page).toHaveURL(/\/sign-in\//);
  await signInWithForm(page, lea.email, lea.password);
});

test("sign out everywhere ends the other browser's session", async ({ isolated, browser, baseURL }) => {
  const robin = await isolated.member({ name: "Robin" });
  const page = robin.page;
  // A second browser that holds the same sign-in. It is copied, not signed in again: a fresh sign-in from a
  // browser with no cookies ends the lane owner's session through the gateway's reuse check, outside this test.
  const elsewhere = await browser.newContext({ baseURL, storageState: await robin.context.storageState() });
  expect((await (await elsewhere.request.get("/me/api/session")).json()).authenticated).toBe(true);

  await page.goto("/account/");
  await page.getByRole("button", { name: "Sign out everywhere" }).click();
  const dialog = page.getByRole("dialog", { name: "Sign out everywhere?" });
  await dialog.getByRole("button", { name: "Sign out everywhere" }).click();

  await expect(page).toHaveURL(/\/sign-in\//);
  await expect(page.getByRole("status")).toContainText("You’re signed out everywhere.");
  expect((await (await elsewhere.request.get("/me/api/session")).json()).authenticated).toBe(false);
  await elsewhere.close();
});
