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
  /** The heading the page's one error carries; it names what failed. Engines is another lane's page and is not asserted. */
  heading?: RegExp;
}

const CAPTURES: Capture[] = [
  { group: "home", url: "/", heading: /^The gateway didn’t answer\.$/ },
  { group: "activity-audit", url: "/activity/", tab: "Changes", heading: /^Couldn’t load changes$/ },
  { group: "activity-requests", url: "/activity/", heading: /^Couldn’t load requests$/ },
  { group: "apps", url: "/apps/", heading: /^Couldn’t load some app details$/ },
  { group: "status-engines-services", url: "/engines/" },
  { group: "library", url: "/library/", heading: /^Couldn’t load your library$/ },
  { group: "models", url: "/models/", heading: /^Couldn’t load the models$/ },
  { group: "people-people", url: "/people/", heading: /^Couldn’t load people$/ },
  { group: "people-plans", url: "/people/?tab=plans", heading: /^Couldn’t load access plans$/ },
  { group: "providers", url: "/providers/", heading: /^Couldn’t load your providers$/ },
  { group: "settings", url: "/settings/", heading: /^Couldn’t load settings$/ },
  { group: "status", url: "/status/", heading: /^Couldn’t check status$/ },
  { group: "try", url: "/try/", heading: /^Couldn’t load the models$/ },
  { group: "usage", url: "/usage/", heading: /^Couldn’t load your usage$/ },
];

/** Every read failure says nothing was lost. Home's own words are the model. */
const REASSURANCE = /Nothing was changed|Your work is safe|Your creations are safe/;

const SESSION = /\/(me|admin)\/api\/session(\?|$)/;
const SIGNED_IN_DATA = /\/(me|admin)\/api\//;

/**
 * Fails every signed-in data GET of the capture that is current. A route the
 * stub registry does not know answers a refused connection instead, so no
 * request of the route reaches the healthy gateway; the capture counts both.
 */
async function routeFailures(page: Page) {
  let current: { entry: Capture; refused: number; dropped: number } | undefined;
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
        active.refused += 1;
      } catch {
        await route.abort("connectionrefused");
        active.dropped += 1;
      }
    },
  );
  return {
    begin(entry: Capture) {
      const capture = { entry, refused: 0, dropped: 0 };
      current = capture;
      return {
        count: () => capture.refused + capture.dropped,
        finish: () => {
          if (current === capture) current = undefined;
        },
      };
    },
  };
}

/** Waits until the page has stopped asking: the query retries are over and no skeleton is left. */
async function settle(page: Page, count: () => number, where: string) {
  let last = -1;
  await expect
    .poll(
      async () => {
        const now = count();
        const quiet = now > 0 && now === last;
        last = now;
        return quiet && (await page.locator('[data-slot="skeleton"]').count()) === 0;
      },
      { message: `${where}: the failed requests and their retries settle`, timeout: 30_000, intervals: [1200] },
    )
    .toBe(true);
}

for (const theme of themes) {
  for (const width of widths) {
    test(`every data route shows an error state: ${theme} ${width}`, async ({ page }) => {
      // Fourteen routes, each waits for its failed requests and their retries to settle.
      test.setTimeout(300_000);
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
              await expect.poll(count, { message: `${where}: a route GET fails` }).toBeGreaterThan(0);
              await settle(page, count, where);
              await expect.poll(
                () => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth),
                { message: `${where}: fits the viewport` },
              ).toBeLessThanOrEqual(0);
              // Inside main: the shell keeps its own alert region (the route announcer), which says nothing about the route.
              const main = page.getByRole("main");
              await expect(
                main.getByRole("button", { name: /try again|refresh|retry/i }).first(),
                `${where}: actionable error`,
              ).toBeVisible();
              if (item.heading) {
                // One outage, one error: the page shows a single alert whose heading names what failed.
                // Soft, so one route's wording does not hide the next route's result.
                await expect.soft(main.getByRole("alert"), `${where}: one alert in an outage`).toHaveCount(1);
                await expect.soft(
                  main.getByRole("alert").getByRole("heading", { name: item.heading }),
                  `${where}: the error names what failed`,
                ).toBeVisible();
                await expect.soft(
                  main.getByText(REASSURANCE).first(),
                  `${where}: the error says nothing was lost`,
                ).toBeVisible();
              }
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
