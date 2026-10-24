import { expect, test } from "./fixtures/isolation";
import AxeBuilder from "@axe-core/playwright";
import { mkdir } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fulfilJson } from "./fixtures/intercept";
import type { Locator, Page } from "@playwright/test";

const options = {
  built_in: true,
  oidc: true,
  oidc_label: "Household accounts",
  oidc_url: "/login?return_to=%2F",
  instance_name: "Azizam",
};
const screenshotDir = path.join(os.homedir(), ".tmp/signin-shots/after");

test.beforeEach(async ({ context }) => {
  await context.clearCookies();
});

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390]) {
    for (const [name, url] of [["signin", "/sign-in/"]]) {
      test(`signed out ${name}: ${theme}, ${width}px`, async ({ page }) => {
        await page.setViewportSize({
          width,
          height: width === 390 ? 844 : 900,
        });
        await page.emulateMedia({
          colorScheme: theme,
          reducedMotion: "reduce",
        });
        await page.addInitScript(
          (value) => localStorage.setItem("theme", value),
          theme,
        );
        // Observe every insertion, including the static export before hydration.
        await page.addInitScript(() => {
          const flashes: string[] = [];
          Object.assign(window, { shellFlashes: flashes });
          new MutationObserver((records) => {
            for (const record of records)
              for (const node of record.addedNodes) {
                if (
                  node instanceof Element &&
                  (node.matches(".shell-sidebar, nav, [data-slot=skeleton]") ||
                    node.querySelector(
                      ".shell-sidebar, nav, [data-slot=skeleton]",
                    ))
                )
                  flashes.push(node.tagName);
              }
          }).observe(document, { childList: true, subtree: true });
        });
        await page.goto(url);
        await expect(page).toHaveURL(/\/sign-in\//);
        await expect(page.getByLabel("Email", { exact: true })).toBeVisible();
        await expect(page.getByRole("navigation")).toHaveCount(0);
        await expect.poll(() => page.evaluate(
            () =>
              (window as unknown as { shellFlashes: string[] }).shellFlashes,
          )).toEqual([]);
        await page.evaluate(() => document.fonts.ready);
        await mkdir(screenshotDir, { recursive: true });
        await page.screenshot({
          path: path.join(screenshotDir, `${name}-${theme}-${width}.png`),
          fullPage: true,
        });
        const result = await new AxeBuilder({ page }).analyze();
        expect(
          result.violations.filter((v) =>
            ["serious", "critical"].includes(v.impact ?? ""),
          ),
        ).toEqual([]);
        for (const element of await page
          .locator("input, button, a[data-slot=button]")
          .all()) {
          const box = await element.boundingBox();
          expect(box?.height).toBeGreaterThanOrEqual(44);
        }
        await page.evaluate(() => {
          document.documentElement.style.fontSize = "200%";
        });
        await expect.poll(() => page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          )).toBe(true);
        await expect(
          page.getByRole("button", { name: "Sign in", exact: true }),
        ).toBeVisible();
        await page.screenshot({
          path: path.join(
            screenshotDir,
            `${name}-${theme}-${width}-text-200.png`,
          ),
          fullPage: true,
        });
      });
    }
  }
}

test("signed-out root is the sign-in page, not a redirect", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page).toHaveURL(/^https?:\/\/[^/]+\/$/);
  await expect(
    page.getByRole("heading", { level: 1, name: /^Sign in to / }),
  ).toBeVisible();
  await expect(page.getByLabel("Email", { exact: true })).toBeVisible();
  await expect(page.getByRole("navigation")).toHaveCount(0);
  await expect(page.locator("a[href^='/sign-in/']")).toHaveCount(0);
});

test("slow session checks never mount the app shell and retain filters and fragments", async ({
  page,
}) => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/me/api/session", async (route) => {
    await gate;
    await route.continue();
  });
  await page.goto("/models/?capability=chat#selection");
  await expect(page.getByRole("status")).toHaveText("Loading your account…");
  await expect(page.getByRole("navigation")).toHaveCount(0);
  await expect(page.locator("[data-slot=skeleton]")).toHaveCount(0);
  release();
  await expect(page).toHaveURL(/\/sign-in\//);
  expect(new URL(page.url()).searchParams.get("return_to")).toBe(
    "/models/?capability=chat#selection",
  );
});

for (const route of [
  "library",
  "try",
  "apps",
  "status",
  "people",
  "settings",
]) {
  test(`${route} uses the shared signed-out guard`, async ({ page }) => {
    await page.goto(`/${route}/`);
    await expect(page).toHaveURL(/\/sign-in\//);
    expect(new URL(page.url()).searchParams.get("return_to")).toBe(
      `/${route}/`,
    );
    await expect(page.getByRole("navigation")).toHaveCount(0);
  });
}

test("named provider is first, keyboard focus is visible, and provider text wraps at 200%", async ({
  page,
}) => {
  await page.route("**/auth/api/options*", (route) =>
    fulfilJson(route, options),
  );
  await page.goto("/sign-in/");
  const provider = page.getByRole("link", {
    name: "Continue with Household accounts",
  });
  await expect(provider).toBeVisible();
  await provider.focus();
  await expect.poll(() => provider.evaluate(
      (element) => getComputedStyle(element).outlineStyle,
    )).not.toBe("none");
  // With a password form too, the form comes next: its email, its password, its Sign in. The name box follows that Sign in,
  // so it covers both ways in (DESIGN.md 4.9).
  await page.keyboard.press("Tab");
  await expect(page.getByLabel("Email", { exact: true })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByLabel("Password", { exact: true })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Sign in", exact: true })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("checkbox", { name: "Show my name on this browser next time" })).toBeFocused();
  await page.getByRole("heading", { level: 1 }).click();
  for (const theme of ["light", "dark"] as const) {
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
      await page.evaluate((value) => {
        document.documentElement.className = value;
      }, theme);
      await page.screenshot({
        path: path.join(screenshotDir, `provider-${theme}-${width}.png`),
        fullPage: true,
      });
    }
  }
  await page.evaluate(() => {
    document.documentElement.style.fontSize = "200%";
  });
  await expect.poll(() => page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    )).toBe(true);
  await expect(provider).toHaveCSS("white-space", "normal");
  await page.screenshot({
    path: path.join(screenshotDir, "provider-dark-390-text-200.png"),
    fullPage: true,
  });
  const result = await new AxeBuilder({ page }).analyze();
  expect(
    result.violations.filter((v) =>
      ["serious", "critical"].includes(v.impact ?? ""),
    ),
  ).toEqual([]);
});

for (const [state, code, status, message, expected] of [
  [
    "locked",
    "login_rate_limited",
    429,
    "Too many attempts. Try again in 15 minutes.",
    "Try again in 15 minutes.",
  ],
  [
    "disabled",
    "account_disabled",
    403,
    "This account is disabled.",
    "Ask the person who invited you for help.",
  ],
] as const) {
  test(`${state} sign-in keeps the form and explains recovery`, async ({
    page,
  }) => {
    await page.route("**/auth/api/login", (route) =>
      fulfilJson(route, { error: { code, message } }, { status }),
    );
    await page.goto("/sign-in/");
    await page.getByLabel("Email", { exact: true }).fill("member@example.test");
    await page
      .getByLabel("Password", { exact: true })
      .fill("Browser test password");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(page.getByRole("main").getByRole("alert")).toContainText(
      expected,
    );
    await expect(page.getByRole("main").getByRole("alert")).toBeFocused();
  });
}

test("expired reset and provider failure each explain the next step", async ({
  page,
}) => {
  await page.route("**/auth/api/password-reset", (route) =>
    fulfilJson(route, { error: { code: "link_unavailable", message: "Expired" } }, { status: 404 }),
  );
  await page.goto("/sign-in/#reset=expired-fixture");
  await page
    .getByLabel("New password", { exact: true })
    .fill("A long new browser password");
  await page.getByRole("button", { name: "Save password" }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "Ask for a new link.",
  );
  await page.goto("/sign-in/?error=provider");
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "Single sign-on didn’t finish.",
  );
});

// A refused sign-in shows the sign-out screen (S8, DESIGN.md section 6), at a phone and a desktop width. The page is
// opened at the address the gateway redirects to; the redirect itself is the gateway's test.
for (const width of [390, 1440]) {
  test.describe(`a refused sign-in at ${width} px`, () => {
    test.beforeEach(async ({ page }) => {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
      // A gateway with fresh single sign-on reports it (SignInOptions.fresh_sign_in); the fixture gateway does not yet.
      await page.route("**/auth/api/options*", (route) =>
        fulfilJson(route, { ...options, fresh_sign_in: true }),
      );
    });

    test("offers the provider's sign-out when the provider publishes one (S8)", async ({ page }) => {
      await page.goto("/sign-in/?error=not_fresh&return_to=%2Fsettings%2F&provider_sign_out=1");
      const main = page.getByRole("main");
      await expect(
        page.getByRole("heading", { level: 1, name: "Sign out of Household accounts first" }),
      ).toBeFocused();
      await expect(main).toContainText(
        "Azizam couldn’t confirm that Household accounts just asked for a password, so it stopped. Someone may still be signed in to Household accounts in this browser.",
      );
      await expect(main).toContainText(
        "Signing out of Household accounts also signs out of other apps that use it in this browser.",
      );
      await expect(
        main.getByRole("link", { name: "Sign out of Household accounts" }),
      ).toHaveAttribute("href", "/login/provider-sign-out");
      await expect(main.getByRole("button", { name: "Back" })).toBeVisible();
      await page.screenshot({
        path: test.info().outputPath(`w5-shots/${width}-s8-with-sign-out.png`),
        fullPage: true,
      });
    });

    test("says who can sign out when the provider publishes no sign-out address (S8b)", async ({ page }) => {
      await page.goto("/sign-in/?error=not_fresh&return_to=%2Fsettings%2F");
      const main = page.getByRole("main");
      await expect(
        page.getByRole("heading", { level: 1, name: "Sign out of Household accounts first" }),
      ).toBeFocused();
      await expect(main).toContainText(
        "Azizam couldn’t confirm that Household accounts just asked for a password, so it stopped. Ask the person who used Household accounts last to sign out of it. Or open Household accounts in a new tab, choose Sign out, and open nothing else there.",
      );
      await expect(main).toContainText("If neither works, ask the person who runs Azizam.");
      await expect(
        main.getByRole("link", { name: "Sign out of Household accounts" }),
      ).toHaveCount(0);
      await expect(main.getByRole("link", { name: "Sign in again" })).toHaveAttribute(
        "href",
        "/login?return_to=%2F&remember=1",
      );
      await page.screenshot({
        path: test.info().outputPath(`w5-shots/${width}-s8-no-sign-out.png`),
        fullPage: true,
      });
    });

    test("Back returns to the sign-in page without the message", async ({ page }) => {
      await page.goto("/sign-in/?error=not_fresh&provider_sign_out=1");
      await page.getByRole("button", { name: "Back" }).click();
      await expect(
        page.getByRole("heading", { level: 1, name: "Sign in to Azizam" }),
      ).toBeVisible();
      await expect(page.getByRole("main")).not.toContainText("couldn’t confirm");
    });

    test("a refused first-owner sign-in shows the same screen, and Back goes to the setup page", async ({ page }) => {
      await page.route("**/setup/api/state", (route) =>
        fulfilJson(route, { owner_exists: false }),
      );
      await page.goto("/sign-in/?error=not_fresh&return_to=%2Fsetup%2F&provider_sign_out=1");
      await expect(
        page.getByRole("heading", { level: 1, name: "Sign out of Household accounts first" }),
      ).toBeVisible();
      await page.getByRole("button", { name: "Back" }).click();
      await expect(page.getByRole("heading", { name: "Set up Azizam" })).toBeVisible();
      await expect(page.getByRole("link", { name: "Set up your gateway" })).toHaveAttribute(
        "href",
        "/setup/",
      );
    });
  });
}

// Until the gateway reports fresh single sign-on, a refused-sign-in address is an ordinary sign-in page: no screen, no
// sign-out link. This one runs on the fixture gateway, whose own options say fresh_sign_in is false.
test("a refused-sign-in address shows the plain sign-in page while the gateway does not report fresh sign-in", async ({
  page,
}) => {
  await page.goto("/sign-in/?error=not_fresh&return_to=%2Fsettings%2F&provider_sign_out=1");
  await expect(page.getByRole("heading", { level: 1, name: "Sign in to Azizam" })).toBeVisible();
  await expect(page.getByRole("link", { name: /^Sign out of / })).toHaveCount(0);
});

test("a stale single sign-on name opens the sign-in page with the notice, and the form under it", async ({
  page,
}) => {
  await page.goto("/sign-in/?error=tile_unknown");
  const main = page.getByRole("main");
  await expect(main.getByRole("alert")).toContainText(
    "That name can’t sign in from this list. Try another way to sign in, or ask the person who invited you for help.",
  );
  await expect(main.getByLabel("Email")).toBeVisible();
});

test("a new gateway offers setup before asking for a password", async ({
  page,
}) => {
  await page.route("**/setup/api/state", (route) =>
    fulfilJson(route, { owner_exists: false }),
  );
  await page.goto("/sign-in/");
  await expect(
    page.getByRole("heading", { name: "Set up Azizam" }),
  ).toBeVisible();
  await expect(page.locator("form")).toHaveCount(0);
  await page.getByRole("link", { name: "Set up your gateway" }).click();
  await expect(page).toHaveURL(/\/setup\/$/);
});

// Sign-in round 3: the primary's size and colour, the dark edge of an outline button, the remember row's gaps, the 44 px
// targets, and a 40-character instance name with no spaces (the case that used to break the page).
const UNBROKEN_NAME = "Okaforfamilygatewayforthewholehome2026xx";
const TWO_SSO_PEOPLE = ["Robin Okafor", "June Okafor"].map((name, index) => ({
  tile: `tile-${index}`,
  name,
  avatar: { variant: "v1", initial: name[0] },
  hint: null,
  provider: "Household accounts",
  method: "sso",
}));

/** Answers the sign-in options and the remembered people, as the gateway does for these methods. */
async function answerSignIn(
  page: Page,
  methods: Record<string, unknown>,
  people: unknown[] = [],
) {
  await page.route("**/auth/api/options*", (route) =>
    fulfilJson(route, { ...options, ...methods }),
  );
  await page.route("**/auth/api/remembered", (route) =>
    fulfilJson(route, { remember: true, people }),
  );
}

/** A design token's colour in the page's current theme, in the same form the browser reports for a background. */
async function tokenColour(page: Page, token: string) {
  return page.evaluate((name) => {
    const probe = document.createElement("div");
    probe.style.backgroundColor = `var(--${name})`;
    document.body.append(probe);
    const colour = getComputedStyle(probe).backgroundColor;
    probe.remove();
    return colour;
  }, token);
}

/** Where a run of text starts, so a gap is measured between words and not between 44 px targets. */
async function textTop(locator: Locator) {
  return locator.evaluate((element) => {
    const range = document.createRange();
    range.selectNodeContents(element);
    return range.getBoundingClientRect().top;
  });
}

function bottomOf(box: { y: number; height: number } | null) {
  if (!box) throw new Error("no bounding box");
  return box.y + box.height;
}

for (const width of [1440, 390] as const) {
  const height = width === 390 ? 844 : 900;

  test(`sign-in round 3: the primary is 16 px semibold in the Home's Make colour at ${width} px`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
    await page.addInitScript((value) => localStorage.setItem("theme", value), "light");
    await answerSignIn(page, { built_in: false, oidc: true });
    await page.goto("/");
    const primary = page.getByRole("link", { name: "Continue with Household accounts" });
    await expect(primary).toBeVisible();
    await expect(primary).toHaveCSS("font-size", "16px");
    await expect(primary).toHaveCSS("font-weight", "600");
    await expect(primary).toHaveCSS("background-color", await tokenColour(page, "make"));
  });

  test(`sign-in round 3: an outline button's edge is the field edge in dark at ${width} px`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
    await page.addInitScript((value) => localStorage.setItem("theme", value), "dark");
    await answerSignIn(page, { built_in: true, oidc: true });
    await page.goto("/");
    const signIn = page.getByRole("button", { name: "Sign in", exact: true });
    await expect(signIn).toHaveAttribute("data-variant", "outline");
    await expect(signIn).toHaveCSS("border-top-color", await tokenColour(page, "input"));
  });

  test(`sign-in round 3: the remember row keeps its gaps, and the controls keep 44 px targets, at ${width} px`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
    await page.addInitScript((value) => localStorage.setItem("theme", value), "light");
    // Single sign-on, with the owner's password one link below the remember row: about 24 px from the text above it.
    await answerSignIn(page, { built_in: false, oidc: true });
    await page.goto("/");
    const helper = page.getByText("Turn this off on a public computer.");
    const owner = page.getByRole("button", { name: "Owner? Sign in with your password" });
    await expect(owner).toBeVisible();
    const gapToOwner = (await textTop(owner)) - bottomOf(await helper.boundingBox());
    expect(gapToOwner).toBeGreaterThanOrEqual(20);
    expect(gapToOwner).toBeLessThanOrEqual(28);
    for (const control of [owner, page.getByRole("link", { name: "About Azizam" }), page.getByRole("button", { name: /^Switch to (dark|light)$/ })]) {
      const box = await control.boundingBox();
      expect(box?.height).toBeGreaterThanOrEqual(44);
      expect(box?.width).toBeGreaterThanOrEqual(44);
    }
    // Both ways in: the remember row sits after the form's Sign in, and at least 16 px above the reset line.
    await page.unroute("**/auth/api/options*");
    await answerSignIn(page, { built_in: true, oidc: true });
    await page.reload();
    const forgot = page.getByText(/^Forgot your password\?/);
    await expect(forgot).toBeVisible();
    const gapToForgot = (await forgot.boundingBox())!.y - bottomOf(await helper.boundingBox());
    expect(gapToForgot).toBeGreaterThanOrEqual(16);
  });

  test(`sign-in round 3: a 40-character instance name without spaces fits at ${width} px, on the plain page and in the chooser`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
    await answerSignIn(page, { built_in: false, oidc: true, instance_name: UNBROKEN_NAME });
    await page.goto("/");
    const heading = page.getByRole("heading", { level: 1 });
    await expect(heading).toHaveText(`Sign in to ${UNBROKEN_NAME}`);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    expect(await heading.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
    await page.unroute("**/auth/api/options*");
    await answerSignIn(page, { built_in: false, oidc: true, instance_name: UNBROKEN_NAME }, TWO_SSO_PEOPLE);
    await page.reload();
    await expect(heading).toHaveText(`Who’s signing in to ${UNBROKEN_NAME}?`);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    expect(await heading.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
  });
}
