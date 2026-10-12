import { expect, test } from "./fixtures/isolation";
import AxeBuilder from "@axe-core/playwright";
import { mkdir } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

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

test("signed-out root shows the landing, not a sign-in redirect", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page).toHaveURL(/^https?:\/\/[^/]+\/$/);
  await expect(
    page.getByText("Your own AI, in the apps you use", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("navigation")).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Sign in", exact: true })).toHaveAttribute(
    "href",
    "/sign-in/?return_to=%2F",
  );
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
    route.fulfill({ json: options }),
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
  await page.keyboard.press("Tab");
  await expect(page.getByLabel("Email", { exact: true })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByLabel("Password", { exact: true })).toBeFocused();
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
      route.fulfill({ status, json: { error: { code, message } } }),
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
    route.fulfill({
      status: 404,
      json: { error: { code: "link_unavailable", message: "Expired" } },
    }),
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

test("a new gateway offers setup before asking for a password", async ({
  page,
}) => {
  await page.route("**/setup/api/state", (route) =>
    route.fulfill({ json: { owner_exists: false } }),
  );
  await page.goto("/sign-in/");
  await expect(
    page.getByRole("heading", { name: "Set up Azizam" }),
  ).toBeVisible();
  await expect(page.locator("form")).toHaveCount(0);
  await page.getByRole("link", { name: "Set up your gateway" }).click();
  await expect(page).toHaveURL(/\/setup\/$/);
});
