import { type Page, type Route } from "@playwright/test";
import { test, expect } from "./fixtures/isolation";

// The loading state of every data route, in both themes and both widths.
// Each route's own data requests are held: the handler below never answers,
// so the page shows what a person sees while the gateway is still thinking.
// The session request is the one request left alone, because the shell waits
// for it before it shows any route at all (its own wait has its own label,
// "Loading your account").
//
// File names follow the other states: `<group>-loading-<theme>-<width>.png`
// (Home keeps its own folder), so scripts/ui/collect_screens.py counts them.

const themes = ["light", "dark"] as const;
const widths = [1440, 390] as const;

type Scope = "signed-in" | "public";

interface Capture {
  /** The screenshot group named in scripts/ui/collect_screens.py. */
  group: string;
  url: string;
  scope: Scope;
  /** The tab to open once the page is up, when the route has tabs. */
  tab?: string;
}

const CAPTURES: Capture[] = [
  { group: "home", url: "/", scope: "signed-in" },
  { group: "activity-audit", url: "/activity/", scope: "signed-in", tab: "Changes" },
  { group: "activity-requests", url: "/activity/", scope: "signed-in" },
  { group: "apps", url: "/apps/", scope: "signed-in" },
  { group: "status-engines-services", url: "/engines/", scope: "signed-in" },
  { group: "people-invite", url: "/invite/#token=loading-state", scope: "public" },
  { group: "library", url: "/library/", scope: "signed-in" },
  { group: "models", url: "/models/", scope: "signed-in" },
  { group: "people-people", url: "/people/", scope: "signed-in" },
  { group: "people-invites", url: "/people/?tab=invites", scope: "signed-in" },
  { group: "people-plans", url: "/people/?tab=plans", scope: "signed-in" },
  { group: "providers", url: "/providers/", scope: "signed-in" },
  { group: "settings", url: "/settings/", scope: "signed-in" },
  { group: "public-setup", url: "/setup/", scope: "public" },
  { group: "public-sign-in", url: "/sign-in/", scope: "public" },
  { group: "status", url: "/status/", scope: "signed-in" },
  { group: "try", url: "/try/", scope: "signed-in" },
  { group: "usage", url: "/usage/", scope: "signed-in" },
];

const SESSION = /\/(me|admin)\/api\/session(\?|$)/;
const SIGNED_IN_DATA = /\/(me|admin)\/api\//;
const PUBLIC_DATA = /\/(auth|setup)\/api\//;

/** Never answers a data request; the test's own end unroutes it. */
async function holdData(page: Page, scope: Scope) {
  const data = scope === "public" ? PUBLIC_DATA : SIGNED_IN_DATA;
  let held = 0;
  await page.route(
    (url) => data.test(url.pathname + url.search) && !SESSION.test(url.pathname + url.search),
    (route: Route) => {
      if (scope === "signed-in" && route.request().method() !== "GET") return route.fallback();
      held += 1;
      return new Promise<void>(() => {});
    },
  );
  return () => held;
}

test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

async function capture(page: Page, item: Capture, theme: (typeof themes)[number], width: number) {
  const held = await holdData(page, item.scope);
  await page.goto(item.url, { waitUntil: "domcontentloaded" });
  // The shell's own wait for the account is not the route's loading state.
  await expect(page.getByLabel("Loading your account")).toHaveCount(0);
  if (item.tab) await page.getByRole("tab", { name: item.tab, exact: true }).click();
  await expect.poll(held, { message: "The route must ask for its data" }).toBeGreaterThan(0);
  await page.evaluate(() => document.fonts.ready);
  await page.evaluate(
    () => new Promise<void>((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))),
  );

  const where = `${item.group} ${theme} ${width}`;
  // Loading is announced under an accessible name containing "Loading"
  // (the convention waitSettled in integrated-missing-screens.spec.ts reads).
  await expect.soft(page.getByLabel(/Loading/i).first(), `${where}: a labeled loading state`).toBeAttached();
  await expect.soft(page.getByRole("heading", { level: 1 }), `${where}: the page title stays`).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect.soft(overflow, `${where}: fits the viewport`).toBeLessThanOrEqual(0);
  const main = await page.evaluate(() => {
    const root = document.querySelector("main") ?? document.body;
    return {
      text: (root as HTMLElement).innerText.trim().length,
      skeletons: root.querySelectorAll('[data-slot="skeleton"], [data-slot="skeleton-rows"]').length,
      height: Math.round(root.getBoundingClientRect().height),
    };
  });
  expect.soft(main.text + main.skeletons, `${where}: not a blank page`).toBeGreaterThan(0);

  const file = item.group === "home" ? `home/loading-${theme}-${width}` : `${item.group}-loading-${theme}-${width}`;
  await page.screenshot({ path: `test-results/screens/${file}.png`, fullPage: true });
}

for (const theme of themes) {
  for (const width of widths) {
    test(`every data route shows a loading state: ${theme} ${width}`, async ({ page, context }) => {
      test.setTimeout(240_000);
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      await page.addInitScript((value) => localStorage.setItem("theme", value), theme);
      const cookies = await context.cookies();
      for (const item of CAPTURES) {
        await test.step(item.group, async () => {
          // Public routes are seen with no session, as a visitor sees them.
          if (item.scope === "public") await context.clearCookies();
          await capture(page, item, theme, width);
          if (item.scope === "public") await context.addCookies(cookies);
          await page.unrouteAll({ behavior: "ignoreErrors" });
        });
      }
    });
  }
}
