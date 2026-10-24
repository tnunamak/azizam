import { test, expect } from "./fixtures/isolation";
import AxeBuilder from "@axe-core/playwright";
import { fulfilJson } from "./fixtures/intercept";

const builtInOptions = {
  built_in: true,
  oidc: false,
  oidc_label: "",
  oidc_url: "/login?return_to=/",
  instance_name: "Browser test gateway",
  owner_name: "Gateway owner",
};

// Display states, copy and form wiring for these two pages are asserted in
// vitest (setup/page.spec.tsx, sign-in/page.spec.tsx). The browser keeps one
// representative per page for what jsdom cannot see: no horizontal scroll at
// 390 px, axe contrast in the page's theme, and the public shell without the
// signed-in navigation.
const surfaces = [
  { surface: "setup", theme: "dark" },
  { surface: "sign-in", theme: "light" },
] as const;
for (const { surface, theme } of surfaces) {
  test(`${surface}: public shell fits 390px and passes axe in ${theme}`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
    await page.addInitScript(
      (value) => localStorage.setItem("theme", value),
      theme,
    );
    if (surface === "setup") {
      await page.route("**/setup/api/state", (route) =>
        fulfilJson(route, { owner_exists: true, oidc_enabled: false, can_recover: true }),
      );
    } else {
      await page.route("**/auth/api/options*", (route) =>
        fulfilJson(route, builtInOptions),
      );
    }
    await page.goto(`/${surface}/`);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await expect(page.getByRole("button", { name: "Sign in", exact: true })).toBeVisible();
    await expect(
      page.getByRole("navigation", { name: "Main navigation" }),
    ).toHaveCount(0);
    await expect
      .poll(() =>
        page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      )
      .toBe(true);
    const result = await new AxeBuilder({ page }).analyze();
    expect(
      result.violations.filter((v) =>
        ["serious", "critical"].includes(v.impact ?? ""),
      ),
    ).toEqual([]);
    await page.screenshot({
      path: `test-results/screens/public-${surface}-${theme}-390.png`,
      fullPage: true,
    });
  });
}

test("built-in sign-in posts JSON and retains its fields after denied credentials", async ({
  page,
  context,
}) => {
  await context.clearCookies();
  await page.route("**/auth/api/options*", (route) =>
    fulfilJson(route, builtInOptions),
  );
  await page.goto("/sign-in/");
  await page.getByLabel("Email", { exact: true }).fill("unknown@example.test");
  await page
    .getByLabel("Password", { exact: true })
    .fill("Invalid browser test password");
  const posted = page.waitForResponse(
    (response) =>
      response.url().endsWith("/auth/api/login") &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  const response = await posted;
  expect(response.status()).toBe(401);
  expect(response.request().postDataJSON()).toEqual({
    email: "unknown@example.test",
    password: "Invalid browser test password",
    return_to: "/",
    remember: true,
  });
  await expect(page).toHaveURL(/\/sign-in\/$/);
  await expect(
    page
      .getByRole("alert")
      .filter({ hasText: "Email or password is incorrect." }),
  ).toBeVisible();
  await expect(page.getByLabel("Email", { exact: true })).toHaveValue(
    "unknown@example.test",
  );
  await expect(page.getByLabel("Password", { exact: true })).toHaveValue(
    "Invalid browser test password",
  );
  await expect(
    page.getByRole("button", { name: "Sign in", exact: true }),
  ).toBeEnabled();
});

for (const theme of ["light", "dark"] as const) {
  test(`native recovery failure fits 390px in ${theme}`, { tag: "@global-state" }, async ({
    page,
    context,
  }) => {
    await context.clearCookies();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
    await page.addInitScript(
      (value) => localStorage.setItem("theme", value),
      theme,
    );
    await page.route("**/setup/api/state", (route) =>
      fulfilJson(route, { owner_exists: true, oidc_enabled: false, can_recover: true }),
    );
    await page.goto("/setup/");
    await page
      .getByText("Recovery and advanced setup", { exact: true })
      .click();
    await page
      .getByLabel("Recovery admin key")
      .fill("invalid-browser-recovery-key");
    await page
      .getByRole("button", { name: "Continue with recovery key" })
      .click();
    await expect(page).toHaveURL(/\/setup\/claim$/);
    await expect(page.getByRole("alert")).toBeVisible();
    await expect(page.locator("details.recovery")).toHaveAttribute("open", "");
    await expect(page.locator("html")).toHaveClass(
      theme === "dark" ? /setup-public.*dark/ : /^setup-public$/,
    );
    await expect
      .poll(() =>
        page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      )
      .toBe(true);
    await page.screenshot({
      path: `test-results/screens/public-native-recovery-${theme}-390.png`,
      fullPage: true,
    });
    const axe = await new AxeBuilder({ page }).analyze();
    expect(
      axe.violations.filter((v) =>
        ["serious", "critical"].includes(v.impact ?? ""),
      ),
    ).toEqual([]);
  });
}
