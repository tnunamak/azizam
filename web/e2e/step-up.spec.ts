import { createHmac, randomBytes } from "node:crypto";
import { fulfilJson } from "./fixtures/intercept";
import { expect, GLOBAL_STATE_TAG, test } from "./fixtures/isolation";
import {
  request as playwrightRequest,
  type APIRequestContext,
  type Browser,
  type Page,
} from "@playwright/test";

// Sliding sessions and step-up re-authentication, in a real browser.
//
// A risky owner action from a session whose last password check is older than
// the step-up window answers 401 reauth_required; the app shows one "Confirm
// it's you" prompt and retries the action once the password is right. To get a
// stale session without waiting, the spec forges the signed cookie with the
// fixture gateway's fixed test key (scripts/ui/serve.py). The saved lane
// sign-in is never stale: global setup widens its window to a day
// (fixtures/session-policy.ts), and the forged cookie is three days old.

const SESSION_KEY = "ui-e2e-fixture-session-key-not-production";
const COOKIE = "opx_admin_session";
const OWNER_PASSWORD = "Portable UI test password 2026";
const DAY = 86_400;

const b64url = (data: Buffer | string) => Buffer.from(data).toString("base64url");

/** The cookie the gateway would issue for this person, last confirmed `ageDays` ago. */
function forgedSession(owner: { sub: string; email: string }, ageDays: number) {
  const now = Math.floor(Date.now() / 1000);
  const then = now - ageDays * DAY;
  // Keys in sorted order, no spaces: the same bytes the gateway signs.
  const payload = b64url(
    JSON.stringify({
      aat: then,
      aexp: then + 90 * DAY,
      email: owner.email,
      exp: then + 30 * DAY,
      iat: then,
      rat: then,
      role: "owner",
      sid: b64url(randomBytes(16)),
      sub: owner.sub,
    }),
  );
  const mac = createHmac("sha256", SESSION_KEY).update(`v1.${payload}`).digest();
  return `v1.${payload}.${b64url(mac)}`;
}

async function ownerIdentity(request: APIRequestContext) {
  const { actor } = await (await request.get("/admin/api/session")).json();
  return { sub: actor.subject as string, email: actor.email as string };
}

/** A fresh browser context signed in as the owner with a three-day-old password check. */
async function staleOwnerPage(
  browser: Browser,
  baseURL: string,
  request: APIRequestContext,
  width: number,
): Promise<Page> {
  const owner = await ownerIdentity(request);
  const context = await browser.newContext({
    baseURL,
    storageState: { cookies: [], origins: [] },
    viewport: { width, height: 900 },
  });
  await context.addCookies([
    { name: COOKIE, value: forgedSession(owner, 3), url: baseURL, httpOnly: true },
  ]);
  return context.newPage();
}

async function openAdvanced(page: Page) {
  await page.goto("/settings/");
  const section = page.getByRole("region", { name: "Advanced", exact: true });
  await section.getByRole("button", { name: "Edit Advanced", exact: true }).click();
  return section;
}

// The gateway holds the effective configuration behind a recent sign-in, as it does the export of the same
// document (gateway/step_up.py, decision D36). The page loads and Advanced opens without asking; showing
// the configuration is the first thing that does.
const effectiveToggle = (page: Page) =>
  page.getByText("Effective configuration and export", { exact: true });

const prompt = (page: Page) => page.getByRole("dialog", { name: /Confirm it.s you/ });

for (const width of [1440, 390]) {
  test(`a stale owner confirms with a password and the export completes at ${width}px`, async ({
    browser,
    baseURL,
    request,
  }) => {
    const page = await staleOwnerPage(browser, baseURL!, request, width);
    try {
      // The page itself loads and Advanced opens: only the risky action asks.
      await openAdvanced(page);
      const dialog = prompt(page);
      await expect(dialog).toBeHidden();
      const exportLink = page.getByRole("link", { name: "Export redacted snapshot" });

      // Showing the configuration is that action.
      await effectiveToggle(page).click();
      await expect(dialog).toBeVisible();
      await expect(dialog).toContainText("This action needs a recent sign-in.");
      await expect(exportLink).toBeHidden();
      const password = dialog.getByLabel("Password");
      await expect(password).toBeFocused();
      await expect(password).toHaveAttribute("autocomplete", "current-password");
      // The dialog fits the screen, with no sideways scroll.
      const box = (await dialog.boundingBox())!;
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width).toBeLessThanOrEqual(width);
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      ).toBe(true);
      await page.screenshot({ path: `test-results/screens/step-up-prompt-${width}.png` });

      // A wrong password keeps the prompt open, clears the field and refocuses it.
      await password.fill("not the password");
      await password.press("Enter");
      await expect(dialog.getByRole("alert")).not.toHaveText("");
      await expect(dialog).toBeVisible();
      await expect(password).toHaveValue("");
      await expect(password).toBeFocused();

      // The right password finishes the original action with no second click: the configuration shows.
      await password.fill(OWNER_PASSWORD);
      await password.press("Enter");
      await expect(dialog).toBeHidden();
      await expect(exportLink).toBeVisible();
      await expect(page.getByLabel("Effective configuration JSON")).toBeVisible();
      // Focus goes back to the control that started the action.
      await expect(effectiveToggle(page)).toBeFocused();

      // The session is now fresh: the export does not ask again.
      const file = page.waitForEvent("download");
      await exportLink.click();
      expect((await file).suggestedFilename()).toBe("gateway-effective-config.json");
      await expect(dialog).toBeHidden();
    } finally {
      await page.context().close();
    }
  });
}

test("cancelling the prompt leaves the configuration hidden and the page usable", async ({
  browser,
  baseURL,
  request,
}) => {
  const page = await staleOwnerPage(browser, baseURL!, request, 1440);
  try {
    await openAdvanced(page);
    const exportLink = page.getByRole("link", { name: "Export redacted snapshot" });
    let downloads = 0;
    page.on("download", () => (downloads += 1));
    await effectiveToggle(page).click();
    const dialog = prompt(page);
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(dialog).toBeHidden();
    await expect(effectiveToggle(page)).toBeFocused();
    // The configuration stays hidden and the page stays usable: the failure is in plain words, with a retry.
    await expect(exportLink).toBeHidden();
    await expect(page.getByText("Effective configuration could not be loaded.")).toBeVisible();
    expect(downloads).toBe(0);
    await page.getByRole("button", { name: "Try again" }).click();
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(dialog).toBeHidden();
  } finally {
    await page.context().close();
  }
});

test("a stale owner's settings change waits for the password, then saves", { tag: GLOBAL_STATE_TAG }, async ({
  browser,
  baseURL,
  request,
}) => {
  const page = await staleOwnerPage(browser, baseURL!, request, 1440);
  try {
    await page.goto("/settings/");
    const card = page.getByRole("region", { name: "Sign-in sessions", exact: true });
    const idle = card.getByLabel("Sign out after this many days without use");
    await expect(idle).toHaveValue("30");
    await idle.fill("31");
    await card.getByRole("button", { name: "Save sign-in session settings" }).click();

    const dialog = prompt(page);
    await expect(dialog).toBeVisible();
    await dialog.getByLabel("Password").fill(OWNER_PASSWORD);
    await dialog.getByRole("button", { name: "Confirm", exact: true }).click();
    await expect(dialog).toBeHidden();
    await expect(page.getByText("Sign-in session settings saved")).toBeVisible();
    await expect(idle).toHaveValue("31");
    const saved = await (await request.get("/admin/api/settings/sessions")).json();
    expect(saved.sessions.idle_timeout_days).toBe(31);
    // The fixture guard puts the setting back.
  } finally {
    await page.context().close();
  }
});

for (const width of [1440, 390]) {
  test(`the owner saves sign-in session settings and sees the gateway's refusal at ${width}px`, { tag: GLOBAL_STATE_TAG }, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto("/settings/");
    const card = page.getByRole("region", { name: "Sign-in sessions", exact: true });
    const idle = card.getByLabel("Sign out after this many days without use");
    const absolute = card.getByLabel("Always sign in again after this many days");
    const stepUp = card.getByLabel(
      "Ask for the password again for risky actions after this many minutes",
    );
    const save = card.getByRole("button", { name: "Save sign-in session settings" });
    await expect(idle).toHaveValue("30");
    await expect(absolute).toHaveValue("90");
    await expect(stepUp).toHaveValue("1440");
    await expect(card).toContainText("from 1 to 90");
    await expect(save).toBeDisabled();

    // Outside the bounds: the gateway's own message is shown, the draft is kept.
    await idle.fill("0");
    await save.click();
    await expect(card.getByRole("alert")).toContainText("from 1 to 90");
    await expect(idle).toHaveValue("0");
    await idle.fill("60");
    await absolute.fill("45");
    await save.click();
    await expect(card.getByRole("alert")).toContainText(/idle timeout cannot be longer/i);
    // Not a whole number never leaves the browser.
    await idle.fill("1.5");
    await save.click();
    await expect(card).toContainText("Enter a whole number.");

    await idle.fill("45");
    await absolute.fill("60");
    await save.click();
    await expect(page.getByText("Sign-in session settings saved")).toBeVisible();
    await expect(card.getByRole("alert")).toHaveCount(0);
    await expect(save).toBeDisabled();
    await page.reload();
    await expect(idle).toHaveValue("45");
    await expect(absolute).toHaveValue("60");
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
    ).toBe(true);
    // The fixture guard puts the setting back.
  });
}

test("sign out everywhere ends the person's sign-ins and returns to sign-in", async ({
  isolated,
  baseURL,
}) => {
  // A member, not the lane owner: signing the lane owner out would end the
  // sign-in every other test on this server shares.
  const member = await isolated.member();
  const cookies = await member.context.cookies();
  const session = cookies.find((cookie) => cookie.name === COOKIE);
  expect(session, "the member's session cookie").toBeDefined();
  const withCookie = async () =>
    playwrightRequest.newContext({
      baseURL,
      storageState: { cookies: [session!], origins: [] },
    });
  const before = await withCookie();
  expect((await (await before.get("/me/api/session")).json()).authenticated).toBe(true);
  await before.dispose();

  await member.page.goto("/account/");
  await member.page.getByRole("button", { name: "Sign out everywhere" }).click();
  const dialog = member.page.getByRole("dialog", { name: "Sign out everywhere?" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Sign out everywhere" }).click();
  await expect(member.page).toHaveURL(/\/sign-in\/?$/);

  // The old cookie is dead on the server, not just forgotten by the browser.
  const after = await withCookie();
  expect((await (await after.get("/me/api/session")).json()).authenticated).toBe(false);
  await after.dispose();
});

test("the confirm prompt offers a way out when a password cannot be used", async ({
  page,
}) => {
  // The gateway names the methods; a sign-in that has neither a password nor a
  // provider can only be renewed by signing in again.
  await page.route("**/admin/api/settings/effective/export", (route) =>
    fulfilJson(
      route,
      {
        error: {
          code: "reauth_required",
          message: "Confirm it's you to do this.",
          reauth: { methods: ["sign_in"], window_seconds: 900, oidc_url: "/login?reauth=1" },
        },
      },
      { status: 401 },
    ),
  );
  await page.goto("/settings/");
  await page.getByRole("button", { name: "Edit Advanced", exact: true }).click();
  await page.getByText("Effective configuration and export", { exact: true }).click();
  await page.getByRole("link", { name: "Export redacted snapshot" }).click();
  const dialog = prompt(page);
  await expect(dialog).toBeVisible();
  await expect(dialog.getByLabel("Password")).toHaveCount(0);
  const signIn = dialog.getByRole("button", { name: "Sign in again" });
  await expect(signIn).toBeFocused();
  // This lane is signed in, so the sign-in page may bounce it on; the request is the proof.
  const request = page.waitForRequest(/\/sign-in\/\?return_to=%2Fsettings%2F/);
  await signIn.click();
  await request;
});
