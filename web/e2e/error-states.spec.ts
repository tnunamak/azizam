import AxeBuilder from "@axe-core/playwright";
import { type Page, type Route } from "@playwright/test";
import { fulfilJson } from "./fixtures/intercept";
import { test, expect } from "./fixtures/isolation";

// Proves signed-in routes show an actionable, accessible error after gateway 503s.
// Vitest asserts the specific text each state shows; this spec does not.

const themes = ["light", "dark"] as const;
const widths = [1440, 390] as const;

interface Capture {
  group: string;
  url: string;
  tab?: string;
}

const CAPTURES: Capture[] = [
  { group: "home", url: "/" },
  { group: "activity-audit", url: "/activity/", tab: "Changes" },
  { group: "activity-requests", url: "/activity/" },
  { group: "apps", url: "/apps/" },
  { group: "status-engines-services", url: "/engines/" },
  { group: "library", url: "/library/" },
  { group: "models", url: "/models/" },
  { group: "people-people", url: "/people/" },
  { group: "people-plans", url: "/people/?tab=plans" },
  { group: "providers", url: "/providers/" },
  { group: "settings", url: "/settings/" },
  { group: "status", url: "/status/" },
  { group: "try", url: "/try/" },
  { group: "usage", url: "/usage/" },
];

const SESSION = /\/(me|admin)\/api\/session(\?|$)/;
const SIGNED_IN_DATA = /\/(me|admin)\/api\//;

async function routeFailures(page: Page) {
  let current: { entry: Capture; refused: number } | undefined;
  await page.route(
    (url) => !SESSION.test(url.pathname + url.search),
    async (route: Route) => {
      const active = current;
      const request = route.request();
      const url = new URL(request.url());
      if (!active || !SIGNED_IN_DATA.test(url.pathname + url.search) || request.method() !== "GET") {
        return route.fallback();
      }
      try {
        await fulfilJson(route, { error: { message: "Unavailable for this test" } }, { status: 503 });
      } catch {
        return route.fallback();
      }
      active.refused += 1;
    },
  );
  return {
    begin(entry: Capture) {
      const capture = { entry, refused: 0 };
      current = capture;
      return {
        count: () => capture.refused,
        finish: () => {
          if (current === capture) current = undefined;
        },
      };
    },
  };
}

for (const theme of themes) {
  for (const width of widths) {
    test(`every data route shows an error state: ${theme} ${width}`, async ({ page }) => {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      await page.addInitScript((value) => localStorage.setItem("theme", value), theme);
      const failures = await routeFailures(page);
      try {
        for (const item of CAPTURES) {
          await test.step(item.group, async () => {
            const { count, finish } = failures.begin(item);
            try {
              await page.goto(item.url, { waitUntil: "domcontentloaded" });
              await expect(page.getByLabel("Loading your account")).toHaveCount(0);
              if (item.tab) await page.getByRole("tab", { name: item.tab, exact: true }).click();
              const where = `${item.group} ${theme} ${width}`;
              await expect(page.getByRole("heading", { level: 1 }), `${where}: the page title stays`).toBeVisible();
              await expect(page.getByLabel(/^Loading/i), `${where}: loading finishes`).toHaveCount(0, { timeout: 15_000 });
              await expect.poll(count, { message: `${where}: a route GET receives 503` }).toBeGreaterThan(0);
              await expect.poll(
                () => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth),
                { message: `${where}: fits the viewport` },
              ).toBeLessThanOrEqual(0);
              const error = page.getByRole("alert")
                .or(page.getByRole("button", { name: new RegExp("try again", "i") }))
                .first();
              await expect(error, `${where}: actionable error`).toBeVisible();
              await page.screenshot({
                path: `test-results/screens/${item.group}-error-${theme}-${width}.png`,
                fullPage: true,
              });
              const axe = await new AxeBuilder({ page }).analyze();
              expect(
                axe.violations.filter(({ impact }) => impact === "serious" || impact === "critical"),
                `${where}: no serious or critical accessibility violations`,
              ).toEqual([]);
            } finally {
              finish();
            }
          });
        }
      } finally {
        await page.unrouteAll({ behavior: "wait" });
      }
    });
  }
}
