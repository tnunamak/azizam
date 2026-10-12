import { test, expect, type Page, type Route } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const themes = ["light", "dark"] as const;
const widths = [1440, 390] as const;
const states = ["seeded", "empty", "error"] as const;

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
  await page.waitForLoadState("networkidle");
  await expect(page.getByLabel(/Loading/i)).toHaveCount(0);
}

async function inspect(page: Page, file: string) {
  await waitSettled(page);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
    "Page must fit the viewport",
  ).toBe(true);
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

async function mockApps(page: Page, state: "empty" | "error") {
  await page.route("**/me/api/session", (route) =>
    route.fulfill({
      status: state === "error" ? 503 : 200,
      json:
        state === "error"
          ? { error: { message: "Unavailable for this test" } }
          : {
              authenticated: true,
              user: {
                id: "user_apps_empty",
                name: "Empty apps user",
                email: "apps@example.test",
                role: "member",
              },
              csrf_token: "csrf",
              apps: [],
            },
    }),
  );
}

async function mockProviders(page: Page, state: "seeded" | "empty" | "error") {
  await page.route("**/admin/api/providers", (route) => {
    if (state === "error") {
      return route.fulfill({
        status: 503,
        json: { error: { message: "Provider API unavailable" } },
      });
    }
    return route.fulfill({
      json: {
        providers:
          state === "empty"
            ? []
            : [
                {
                  id: "openrouter",
                  name: "OpenRouter",
                  enabled: true,
                  reachable: true,
                  key_set: true,
                  spend_usd: 1.25,
                  cap_usd: 10,
                },
              ],
      },
    });
  });
}

for (const theme of themes) {
  for (const width of widths) {
    for (const state of states) {
      test(`apps screenshot matrix ${state}: ${theme} ${width}`, async ({
        page,
      }) => {
        await setViewport(page, theme, width);
        if (state !== "seeded") await mockApps(page, state);
        await page.goto("/apps/");
        if (state === "seeded") {
          await expect(
            page.getByRole("heading", { name: "Browser test app" }),
          ).toBeVisible();
        } else if (state === "empty") {
          await expect(
            page.getByText("Connect your first app", { exact: true }),
          ).toBeVisible();
        } else {
          await expect(
            page.getByText("Check your connection and try again."),
          ).toBeVisible();
        }
        await inspect(page, `apps-${state}-${theme}-${width}`);
      });

      test(`providers screenshot matrix ${state}: ${theme} ${width}`, async ({
        page,
      }) => {
        await setViewport(page, theme, width);
        await mockProviders(page, state);
        await page.goto("/providers/");
        if (state === "seeded") {
          await expect(
            page.getByRole("heading", { name: "OpenRouter" }),
          ).toBeVisible();
        } else if (state === "empty") {
          await expect(
            page.getByText("No providers yet", { exact: true }),
          ).toBeVisible();
        } else {
          await expect(
            page.getByRole("button", { name: "Retry providers" }),
          ).toBeVisible();
        }
        await inspect(page, `providers-${state}-${theme}-${width}`);
      });
    }
  }
}
