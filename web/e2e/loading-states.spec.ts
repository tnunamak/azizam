import { type Page, type Route } from "@playwright/test";
import { test, expect } from "./fixtures/isolation";

// The loading state of every data route, in both themes and both widths.
// Each route's own data requests are held until release, so the page shows
// what a person sees while the gateway is still thinking.
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
  loadingLabel: string;
  /** The tab to open once the page is up, when the route has tabs. */
  tab?: string;
  /**
   * The route's data is the signed-in snapshot itself. The document head asks for it once and the shell takes
   * that answer; the route asks again, and that second request is the one held (gate trace of train 89: the
   * Apps loader stood for the 40 ms of its own session request only).
   */
  holdsSession?: boolean;
}

const CAPTURES: Capture[] = [
  { group: "home", url: "/", scope: "signed-in", loadingLabel: "Loading recent work" },
  { group: "activity-audit", url: "/activity/", scope: "signed-in", tab: "Changes", loadingLabel: "Loading changes" },
  { group: "activity-requests", url: "/activity/", scope: "signed-in", loadingLabel: "Loading requests" },
  { group: "apps", url: "/apps/", scope: "signed-in", loadingLabel: "Loading apps", holdsSession: true },
  { group: "status-engines-services", url: "/engines/", scope: "signed-in", loadingLabel: "Loading engines" },
  { group: "people-invite", url: "/invite/#token=loading-state", scope: "public", loadingLabel: "Loading invitation" },
  { group: "library", url: "/library/", scope: "signed-in", loadingLabel: "Loading library" },
  { group: "models", url: "/models/", scope: "signed-in", loadingLabel: "Loading models" },
  { group: "people-people", url: "/people/", scope: "signed-in", loadingLabel: "Loading people" },
  { group: "people-invites", url: "/people/?tab=invites", scope: "signed-in", loadingLabel: "Loading invites" },
  { group: "people-plans", url: "/people/?tab=plans", scope: "signed-in", loadingLabel: "Loading access plans" },
  { group: "providers", url: "/providers/", scope: "signed-in", loadingLabel: "Loading providers" },
  { group: "settings", url: "/settings/", scope: "signed-in", loadingLabel: "Loading settings" },
  { group: "public-setup", url: "/setup/", scope: "public", loadingLabel: "Loading setup" },
  { group: "public-sign-in", url: "/sign-in/", scope: "public", loadingLabel: "Loading sign-in" },
  // The owner page waits on two sources; the requests list is the one with a labeled loader. "Loading status" is
  // only the route's code-loading fallback, which a held data request never shows.
  { group: "status", url: "/status/", scope: "signed-in", loadingLabel: "Loading your requests" },
  { group: "try", url: "/try/", scope: "signed-in", loadingLabel: "Loading models" },
  { group: "usage", url: "/usage/", scope: "signed-in", loadingLabel: "Loading usage" },
];

const SESSION = /\/(me|admin)\/api\/session(\?|$)/;
const SIGNED_IN_DATA = /\/(me|admin)\/api\//;
const PUBLIC_DATA = /\/(auth|setup)\/api\//;

interface Hold {
  scope: Scope;
  holdsSession: boolean;
  sessions: number;
  held: number;
  released: Promise<void>;
  release: () => void;
}

/**
 * One route for the page's whole test, which holds the data requests of the
 * capture that is current until that capture releases them. Removing the route
 * and adding a new one between captures left the next page's requests unrouted
 * (gate run of train 88: no route call at all after the second navigation), so
 * the route stays and only the hold changes.
 */
async function routeHolds(page: Page) {
  let current: Hold | undefined;
  await page.route(
    () => true,
    async (route: Route) => {
      const hold = current;
      const url = new URL(route.request().url());
      const path = url.pathname + url.search;
      if (SESSION.test(path)) {
        // The first signed-in snapshot is the shell's; only a route that holds its own asks a second time.
        const own = hold?.holdsSession && path.startsWith("/me/") && route.request().method() === "GET" && hold.sessions++ > 0;
        if (!own) return route.fallback();
      }
      const data = hold?.scope === "public" ? PUBLIC_DATA : SIGNED_IN_DATA;
      if (!hold || !data.test(path)) return route.fallback();
      if (hold.scope === "signed-in" && route.request().method() !== "GET") return route.fallback();
      hold.held += 1;
      await hold.released;
      try {
        await route.fallback();
      } catch (error) {
        // A request the browser already cancelled cannot be continued.
        if (!route.request().failure() &&
            !(error instanceof Error && /invalid interception.?id|interception (?:was )?cancelled/i.test(error.message))) {
          throw error;
        }
      }
    },
  );
  return {
    /** Holds the data requests of one capture until its release. */
    hold(scope: Scope, holdsSession = false) {
      let release!: () => void;
      const released = new Promise<void>((resolve) => {
        release = resolve;
      });
      const hold: Hold = { scope, holdsSession, sessions: 0, held: 0, released, release };
      current = hold;
      return {
        count: () => hold.held,
        release: () => {
          if (current === hold) current = undefined;
          hold.release();
        },
      };
    },
  };
}

async function capture(
  page: Page,
  holds: Awaited<ReturnType<typeof routeHolds>>,
  item: Capture,
  theme: (typeof themes)[number],
  width: number,
) {
  const { count, release } = holds.hold(item.scope, item.holdsSession);
  try {
    await page.goto(item.url, { waitUntil: "domcontentloaded" });
    // The shell's own wait for the account is not the route's loading state.
    await expect(page.getByLabel("Loading your account")).toHaveCount(0);
    if (item.tab) await page.getByRole("tab", { name: item.tab, exact: true }).click();
    await expect.poll(count, { message: "The route must ask for its data" }).toBeGreaterThan(0);
    await page.evaluate(() => document.fonts.ready);
    await page.evaluate(
      () => new Promise<void>((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))),
    );

    const where = `${item.group} ${theme} ${width}`;
    // first(): a route may show its loader in two places (a list and a panel) while both wait.
    const loader = page.getByLabel(item.loadingLabel, { exact: true }).first();
    await expect(loader, `${where}: the route's labeled loading state`).toBeAttached();
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
    if (item.group === "status" && theme === "light" && width === 1440) {
      release();
      await expect(loader).toHaveCount(0, { timeout: 15_000 });
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    }
  } finally {
    release();
  }
}

for (const theme of themes) {
  for (const width of widths) {
    test(`every data route shows a loading state: ${theme} ${width}`, async ({ page, context }) => {
      test.setTimeout(240_000);
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      await page.addInitScript((value) => localStorage.setItem("theme", value), theme);
      const cookies = await context.cookies();
      const holds = await routeHolds(page);
      for (const item of CAPTURES) {
        await test.step(item.group, async () => {
          // Public routes are seen with no session, as a visitor sees them.
          if (item.scope === "public") await context.clearCookies();
          try {
            await capture(page, holds, item, theme, width);
          } finally {
            if (item.scope === "public") await context.addCookies(cookies);
          }
        });
      }
      await page.unrouteAll({ behavior: "wait" });
    });
  }
}
