import { type Page } from "@playwright/test";
import { test, expect } from "./fixtures/isolation";
import AxeBuilder from "@axe-core/playwright";
import { fulfilJson } from "./fixtures/intercept";

const themes = ["light", "dark"] as const;
const widths = [1440, 390] as const;

test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

async function setViewport(
  page: Page,
  theme: (typeof themes)[number],
  width: number,
) {
  await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
  await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
  await page.addInitScript(
    (value) => localStorage.setItem("theme", value),
    theme,
  );
}

async function waitSettled(page: Page) {
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await expect(page.getByLabel(/Loading/i)).toHaveCount(0);
}

async function inspect(page: Page, file: string) {
  await waitSettled(page);
  await expect
    .poll(
      () =>
        page.evaluate(
          () => document.documentElement.scrollWidth - window.innerWidth,
        ),
      { message: "Page must fit the viewport" },
    )
    .toBeLessThanOrEqual(0);
  await page.screenshot({
    path: `test-results/screens/${file}.png`,
    fullPage: true,
  });
  const a11y = await new AxeBuilder({ page }).analyze();
  const violations = a11y.violations
    .filter((violation) =>
      ["serious", "critical"].includes(violation.impact ?? ""),
    )
    .map(({ id, impact, nodes }) => ({
      id,
      impact,
      nodes: nodes.map(({ target, failureSummary }) => ({
        target,
        failureSummary,
      })),
    }));
  expect(violations).toEqual([]);
}

async function mockAppsSessionFailure(page: Page) {
  await page.route("**/me/api/session", (route) =>
    fulfilJson(
      route,
      { error: { message: "Unavailable for this test" } },
      { status: 503 },
    ),
  );
}

async function mockProviders(page: Page, state: "seeded" | "empty") {
  await page.route("**/admin/api/providers", (route) =>
    fulfilJson(route, {
      providers:
        state === "empty"
          ? []
          : [
              {
                id: "openrouter",
                name: "OpenRouter",
                enabled: true,
                reachable: true,
                key_status: { state: "set" },
                spend: { daily_usd: 1.25, monthly_usd: 1.25, status: "known" },
                caps: { daily_usd: 10 },
              },
            ],
    }),
  );
}

// Which text each state shows is asserted in vitest: apps empty and session
// failure (src/app/apps/page.spec.tsx, src/components/shell/app-shell.regression.spec.tsx),
// providers seeded, empty and failed (web/tests/providers-load-retry.spec.tsx).
// The browser keeps one overflow + axe + screenshot representative per route
// and state, spread over both themes and both widths.

async function openApps(
  page: Page,
  state: "seeded" | "error",
  theme: (typeof themes)[number],
  width: (typeof widths)[number],
) {
  await setViewport(page, theme, width);
  if (state === "error") await mockAppsSessionFailure(page);
  await page.goto("/apps/");
  if (state === "seeded") {
    await expect(
      page.getByRole("heading", { name: "Browser test app" }),
    ).toBeVisible();
  } else {
    await expect(
      page.getByText(
        "We couldn't tell why your account didn't load. Try again.",
      ),
    ).toBeVisible();
  }
  await inspect(page, `apps-${state}-${theme}-${width}`);
}

async function openProviders(
  page: Page,
  state: "seeded" | "empty",
  theme: (typeof themes)[number],
  width: (typeof widths)[number],
) {
  await setViewport(page, theme, width);
  await mockProviders(page, state);
  await page.goto("/providers/");
  if (state === "seeded") {
    await expect(
      page.getByRole("heading", { name: "OpenRouter" }),
    ).toBeVisible();
  } else {
    await expect(
      page.getByText("No providers yet", { exact: true }),
    ).toBeVisible();
  }
  await inspect(page, `providers-${state}-${theme}-${width}`);
}

test("apps seeded: light 1440", async ({ page }) => {
  await openApps(page, "seeded", "light", 1440);
});

test("apps seeded: dark 390", async ({ page }) => {
  await openApps(page, "seeded", "dark", 390);
});

test("apps session failure: dark 390", async ({ page }) => {
  await openApps(page, "error", "dark", 390);
});

test("providers seeded: light 1440", async ({ page }) => {
  await openProviders(page, "seeded", "light", 1440);
});

test("providers seeded: dark 390", async ({ page }) => {
  await openProviders(page, "seeded", "dark", 390);
});

test("providers empty: dark 390", async ({ page }) => {
  await openProviders(page, "empty", "dark", 390);
});
