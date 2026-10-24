import { mkdirSync } from "node:fs";
import path from "node:path";
import type { Browser, Page } from "@playwright/test";
import { expect, test } from "./fixtures/isolation";
import { fulfilBytes, fulfilJson } from "./fixtures/intercept";
import { answerSignInOptions, signedOut } from "./fixtures/signed-out";

/*
 * The built signed-out front door, saved for the design side-by-side check: three sign-in methods, with and without
 * remembered people, at 1440 and 390, in light and dark. Round 3 adds the states the design judge asked for: the loading
 * page, a wrong password, the provider opening, a long instance name, and a focus ring on the primary action. Round 4
 * adds the password form while it signs in, and focus rings on Email, the remember checkbox and a text link. The
 * household's own names are answered here, because the fixture has no identity provider to remember people through. Set
 * SIGNIN_SHOTS_DIR to save the pictures; otherwise these cases are skipped.
 */
const SHOTS = process.env.SIGNIN_SHOTS_DIR;

const METHODS = {
  "sso-only": { built_in: false, oidc: true },
  "password-only": { built_in: true, oidc: false },
  both: { built_in: true, oidc: true },
} as const;

type Mode = keyof typeof METHODS;

/** A 40-character name with spaces, and a 40-character name without any: the second is the case that used to break. */
const LONG_NAME = "Okafor family gateway for the whole home";
const UNBROKEN_NAME = "Okaforfamilygatewayforthewholehome2026xx";

/** The people a browser remembers, as the gateway lists them for each method. */
function rememberedFor(mode: Mode) {
  const robin = { tile: "tile-robin", name: "Robin Okafor", avatar: { variant: "v1", initial: "R" }, hint: null, provider: null };
  const june = { tile: "tile-june", name: "June Okafor", avatar: { variant: "v4", initial: "J" }, hint: null, provider: null };
  if (mode === "sso-only")
    return [
      { ...robin, method: "sso", provider: "Okafor ID" },
      { ...june, method: "sso", provider: "Okafor ID" },
    ];
  if (mode === "password-only") return [
    { ...robin, method: "password" },
    { ...june, method: "password" },
  ];
  return [
    { ...robin, method: "password" },
    { ...june, method: "sso", provider: "Okafor ID" },
  ];
}

/** Answers the options and the remembered people for one method, as the gateway would. */
async function answer(page: Page, mode: Mode, { remembered = false, instance }: { remembered?: boolean; instance?: string } = {}) {
  await page.route("**/auth/api/options*", (route) =>
    answerSignInOptions(route, { ...METHODS[mode], ...(instance ? { instance_name: instance } : {}) }),
  );
  await page.route("**/auth/api/remembered", (route) =>
    fulfilJson(route, { remember: true, people: remembered ? rememberedFor(mode) : [] }),
  );
}

/** The loaded page: its heading names the household, or the people who remember it. */
async function loaded(page: Page) {
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(/^(Sign in to |Who’s signing in to )/);
}

/** The password form with a wrong password: the gateway refuses it, and the page says so. */
async function wrongPassword(page: Page) {
  await page.route("**/auth/api/login", (route) =>
    fulfilJson(route, { error: { code: "invalid_login", message: "Email or password is incorrect." } }, { status: 401 }),
  );
  await loaded(page);
  await page.getByLabel("Email", { exact: true }).fill("robin@example.test");
  await page.getByLabel("Password", { exact: true }).fill("not the password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Email or password is incorrect." })).toBeVisible();
}

/** Keyboard focus on single sign-on, so its focus ring shows. The first Tab reaches it. */
async function focusSso(page: Page) {
  await loaded(page);
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Continue with Okafor ID" })).toBeFocused();
}

/** Keyboard focus on the form's Sign in: Tab reaches the email field, and the button takes focus from there. */
async function focusSignIn(page: Page) {
  await loaded(page);
  await page.keyboard.press("Tab");
  const signIn = page.getByRole("button", { name: "Sign in", exact: true });
  await signIn.focus();
  await expect(signIn).toBeFocused();
}

/** Keyboard focus on Email: the first Tab after the heading reaches it. */
async function focusEmail(page: Page) {
  await loaded(page);
  await page.keyboard.press("Tab");
  await expect(page.getByLabel("Email", { exact: true })).toBeFocused();
}

/** Keyboard focus on the remember checkbox: after Email, Password and Sign in. */
async function focusRemember(page: Page) {
  await focusEmail(page);
  for (let step = 0; step < 3; step += 1) await page.keyboard.press("Tab");
  await expect(page.getByRole("checkbox", { name: "Remember me on this browser" })).toBeFocused();
}

/** Keyboard focus on the owner's text link: after the provider and the remember checkbox. */
async function focusOwnerLink(page: Page) {
  await loaded(page);
  for (let step = 0; step < 3; step += 1) await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Owner? Sign in with your password" })).toBeFocused();
}

/** The password form submitted: the sign-in never answers, so the page holds "Signing in…" for the picture. */
async function signingIn(page: Page) {
  await loaded(page);
  await page.getByLabel("Email", { exact: true }).fill("robin@example.test");
  await page.getByLabel("Password", { exact: true }).fill("not the password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("button", { name: "Signing in…" })).toBeDisabled();
}

/**
 * Saves one picture at each width and theme. `arrange` routes the page before it loads; `settle` waits until the state is
 * on screen, and may act first.
 */
async function savePictures(
  browser: Browser,
  baseURL: string | undefined,
  name: string,
  arrange: (page: Page) => Promise<void>,
  settle: (page: Page) => Promise<void> = loaded,
) {
  mkdirSync(SHOTS!, { recursive: true });
  for (const width of [1440, 390]) {
    for (const theme of ["light", "dark"] as const) {
      const height = width === 390 ? 844 : 900;
      const { context, page } = await signedOut(browser, baseURL, { width, height });
      try {
        await context.addInitScript((value) => localStorage.setItem("theme", value), theme);
        await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
        await arrange(page);
        await page.goto("/");
        await settle(page);
        await page.evaluate(() => document.fonts.ready);
        await page.screenshot({ path: path.join(SHOTS!, `${name}-${width}${theme === "dark" ? "-dark" : ""}.png`) });
      } finally {
        await context.close();
      }
    }
  }
}

test.describe("built sign-in pages for the design side-by-side check", () => {
  test.skip(!SHOTS, "set SIGNIN_SHOTS_DIR to save the built sign-in pages");
  for (const mode of Object.keys(METHODS) as Mode[]) {
    for (const remembered of [false, true]) {
      const name = `signin-${mode}${remembered ? "-remembered" : ""}`;
      test(`saves the ${mode} sign-in${remembered ? " with remembered people" : ""} at 1440 and 390, light and dark`, async ({ browser, baseURL }) => {
        await savePictures(browser, baseURL, name, (page) => answer(page, mode, { remembered }));
      });
    }
  }

  test("saves the loading page, before the options arrive", async ({ browser, baseURL }) => {
    await savePictures(
      browser,
      baseURL,
      "signin-loading",
      async (page) => {
        // Never answered: the page stays in its loading state for the picture.
        await page.route("**/auth/api/options*", () => {});
      },
      async (page) => {
        await expect(page.getByRole("heading", { level: 1 })).toHaveText("Sign in");
      },
    );
  });

  test("saves the password form after a wrong password", async ({ browser, baseURL }) => {
    await savePictures(browser, baseURL, "signin-password-only-wrong-password", (page) => answer(page, "password-only"), wrongPassword);
  });

  test("saves the form beside single sign-on after a wrong password", async ({ browser, baseURL }) => {
    await savePictures(browser, baseURL, "signin-both-wrong-password", (page) => answer(page, "both"), wrongPassword);
  });

  test("saves the provider opening, from the list", async ({ browser, baseURL }) => {
    await savePictures(
      browser,
      baseURL,
      "signin-sso-only-remembered-opening",
      async (page) => {
        await answer(page, "sso-only", { remembered: true });
        // A 204 ends the provider's navigation without a new page, so the list stays on "Opening" for the picture.
        await page.route(/\/login\?/, (route) => fulfilBytes(route, { status: 204 }));
      },
      async (page) => {
        await loaded(page);
        // The provider never answers, so the click must not wait for the navigation to commit.
        await page.getByRole("button", { name: "June Okafor" }).click({ noWaitAfter: true });
        await expect(page.getByRole("status")).toContainText("Opening Okafor ID");
      },
    );
  });

  test("saves a long instance name, with spaces, on the plain page", async ({ browser, baseURL }) => {
    await savePictures(browser, baseURL, "signin-both-long-name", (page) => answer(page, "both", { instance: LONG_NAME }));
  });

  test("saves a long instance name, without spaces, on the plain page", async ({ browser, baseURL }) => {
    await savePictures(browser, baseURL, "signin-both-long-name-unbroken", (page) => answer(page, "both", { instance: UNBROKEN_NAME }));
  });

  test("saves a long instance name, with spaces, in the chooser", async ({ browser, baseURL }) => {
    await savePictures(browser, baseURL, "signin-both-remembered-long-name", (page) =>
      answer(page, "both", { remembered: true, instance: LONG_NAME }),
    );
  });

  test("saves the focus ring on single sign-on", async ({ browser, baseURL }) => {
    await savePictures(browser, baseURL, "signin-sso-only-focus", (page) => answer(page, "sso-only"), focusSso);
  });

  test("saves the focus ring on the password form's Sign in", async ({ browser, baseURL }) => {
    await savePictures(browser, baseURL, "signin-password-only-focus", (page) => answer(page, "password-only"), focusSignIn);
  });

  test("saves the password form while it signs in", async ({ browser, baseURL }) => {
    await savePictures(
      browser,
      baseURL,
      "signin-password-only-signing-in",
      async (page) => {
        await answer(page, "password-only");
        // Never answered: the sign-in stays pending for the picture.
        await page.route("**/auth/api/login", () => {});
      },
      signingIn,
    );
  });

  test("saves the focus ring on Email", async ({ browser, baseURL }) => {
    await savePictures(browser, baseURL, "signin-password-only-focus-email", (page) => answer(page, "password-only"), focusEmail);
  });

  test("saves the focus ring on the remember checkbox", async ({ browser, baseURL }) => {
    await savePictures(browser, baseURL, "signin-password-only-focus-remember", (page) => answer(page, "password-only"), focusRemember);
  });

  test("saves the focus ring on a text link, the owner's", async ({ browser, baseURL }) => {
    await savePictures(browser, baseURL, "signin-sso-only-focus-link", (page) => answer(page, "sso-only"), focusOwnerLink);
  });
});
