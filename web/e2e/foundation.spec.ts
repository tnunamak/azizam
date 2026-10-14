import { test, expect } from "./fixtures/isolation";
import AxeBuilder from "@axe-core/playwright";

const routes = [
  ["home", "/"],
  ["models", "/models/"],
  ["try", "/try/"],
  ["library", "/library/"],
  ["usage", "/usage/"],
  ["apps", "/apps/"],
  ["status", "/status/"],
  ["engines", "/engines/"],
  ["providers", "/providers/"],
  ["people", "/people/"],
  ["activity", "/activity/"],
  ["settings", "/settings/"],
  ["design", "/design/"],
  ["sign-in", "/sign-in/"],
  ["setup", "/setup/"],
  ["invite", "/invite/"],
  ["docs", "/docs"],
  ["redoc", "/redoc"],
] as const;

// Every route renders against the real gateway in the light desktop theme.
// The dark phone pass covers the data-bearing app routes only: theme tokens
// and 390 px geometry are one concept, not one concept per route.
const darkPhoneRoutes = new Set([
  "home",
  "models",
  "try",
  "library",
  "usage",
  "apps",
  "activity",
  "settings",
]);
const passes = [
  { theme: "light", width: 1440, only: null },
  { theme: "dark", width: 390, only: darkPhoneRoutes },
] as const;

for (const { theme, width, only } of passes) {
  {
    for (const [name, route] of routes) {
      if (only && !only.has(name)) continue;
      test(`${name}: ${theme} at ${width}px`, async ({ page }) => {
        await page.setViewportSize({
          width,
          height: width === 390 ? 844 : 1000,
        });
        await page.emulateMedia({
          colorScheme: theme,
          reducedMotion: "reduce",
        });
        await page.addInitScript((value) => {
          localStorage.setItem("theme", value);
        }, theme);
        const response = await page.goto(route);
        expect(response?.status()).toBe(200);
        await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
        await page.evaluate(() => document.fonts.ready);
        if (name === "apps") {
          await expect(
            page.getByRole("heading", {
              name: "Browser test app",
              exact: true,
            }),
          ).toBeVisible();
          await expect(page.getByLabel("Loading apps")).toHaveCount(0);
        }
        if (name === "activity") {
          await page.getByRole("tab", { name: "Changes", exact: true }).click();
          await expect(
            page.getByRole("button", { name: /Details:/ }).first(),
          ).toBeVisible();
          await expect(page.getByLabel("Loading audit events")).toHaveCount(0);
        }
        if (name === "people") {
          // The seeded plans enable this action after loading. Inspect its
          // settled color rather than the disabled-to-enabled transition.
          const invite = page
            .getByRole("button", {
              name: "Invite someone",
              exact: true,
            })
            .first();
          await expect(invite).toBeEnabled();
          await expect
            .poll(() =>
              invite.evaluate((element) => getComputedStyle(element).opacity),
            )
            .toBe("1");
        }
        if (name !== "design") {
          // A page that is still loading is not the page to scan: axe would
          // read the placeholders. Every loading state sets aria-busy.
          await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
          // Design examples intentionally show error states; axe still checks them.
          await expect(
            page
              .getByRole("alert")
              .filter({ hasText: /could not|failed to|unable to/i }),
          ).toHaveCount(0);
        }
        await expect
          .poll(
            () =>
              page.evaluate(
                () => document.documentElement.scrollWidth <= window.innerWidth,
              ),
            { message: "Page must fit the viewport" },
          )
          .toBe(true);
        if (name === "status") {
          // D1: Refresh is a header action; the menu keeps Technical details.
          const refresh = page.getByRole("button", {
            name: "Refresh",
            exact: true,
          });
          await expect(refresh).toBeVisible();
          await expect(refresh).toBeEnabled();
          await page.getByRole("button", { name: "Status actions" }).click();
          await expect(
            page.getByRole("menuitem", { name: /Technical details/ }),
          ).toBeVisible();
          await page.keyboard.press("Escape");
        }
        await page.screenshot({
          path: `test-results/screens/${name}-${theme}-${width}.png`,
          fullPage: true,
        });
        const a11y = await new AxeBuilder({ page }).analyze();
        const violations = a11y.violations
          .filter((violation) =>
            ["serious", "critical"].includes(violation.impact ?? ""),
          )
          .map((violation) => ({
            rule: violation.id,
            nodes: violation.nodes.map((node) => ({
              target: node.target,
              reason: node.failureSummary,
            })),
          }));
        expect(violations, "Serious accessibility violations").toEqual([]);
      });
    }
  }
}

test("offline API reference renders endpoints without external requests", async ({
  page,
  baseURL,
}) => {
  for (const route of ["/docs", "/redoc"]) {
    const external: string[] = [];
    const listener = (request: { url(): string }) => {
      if (!request.url().startsWith(new URL(baseURL!).origin))
        external.push(request.url());
    };
    page.on("request", listener);
    await page.goto(route);
    await expect(
      page.getByRole("heading", { name: /API reference/ }),
    ).toBeVisible();
    await expect(page.locator("details").first()).toBeVisible();
    await page.locator("details summary").first().click();
    await expect(page.locator("details").first().locator("pre")).toBeVisible();
    expect(external).toEqual([]);
    page.off("request", listener);
  }
});
