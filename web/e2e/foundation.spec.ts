import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const routes = [
  ["home", "/ui/"],
  ["models", "/ui/models/"],
  ["try", "/ui/try/"],
  ["library", "/ui/library/"],
  ["usage", "/ui/usage/"],
  ["apps", "/ui/apps/"],
  ["status", "/ui/status/"],
  ["engines", "/ui/engines/"],
  ["providers", "/ui/providers/"],
  ["people", "/ui/people/"],
  ["activity", "/ui/activity/"],
  ["settings", "/ui/settings/"],
  ["design", "/ui/design/"],
  ["sign-in", "/ui/sign-in/"],
  ["setup", "/ui/setup/"],
  ["invite", "/ui/invite/"],
  ["showcase", "/ui/showcase/"],
  ["docs", "/docs"],
  ["redoc", "/redoc"],
] as const;

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390]) {
    for (const [name, route] of routes) {
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
          await expect(
            page.getByRole("button", { name: /Details:/ }).first(),
          ).toBeVisible();
          await expect(page.getByLabel("Loading audit events")).toHaveCount(0);
        }
        await expect(
          page
            .getByRole("alert")
            .filter({ hasText: /could not|failed to|unable to/i }),
        ).toHaveCount(0);
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
          ),
          "Page must fit the viewport",
        ).toBe(true);
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
