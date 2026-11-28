// Every route the sweep visits, and the states worth checking on each.
// Coverage is enforced: sweep.spec.ts discovers page.tsx files under src/app
// and fails if any app route has no entry here.
import { readdirSync, existsSync } from "node:fs";
import path from "node:path";
import type { Page } from "@playwright/test";
import type { Role } from "./rules";

export interface SweepContext {
  role: Role;
  phone: boolean;
  base: string;
  /** An unclaimed gateway for the first-run Setup screen (fixtures only). */
  setupBase?: string;
  inviteToken?: string;
  pictureWorkflow?: string;
}

/** Apply a state. Return true when applied, or a reason it could not be. */
export type StateAction = (page: Page, ctx: SweepContext) => Promise<true | string>;

export interface SweepState {
  id: string;
  /** Path (and query/hash) to load before the action; defaults to the route path. */
  url?: (ctx: SweepContext) => string | null;
  /** Use the unclaimed first-run gateway. */
  firstRun?: boolean;
  act?: StateAction;
  roles?: Role[];
  /** Expected to be missing for some data sets; a miss is listed, not failed. */
  optional?: boolean;
}

export interface SweepRoute {
  /** App route as derived from src/app (e.g. "/", "/try"). */
  appRoute: string;
  surface: string;
  path: string;
  roles: Role[];
  states: SweepState[];
  /** Design and gallery reference pages: not product surfaces. */
  dev?: boolean;
}

/** Wait briefly for a control; data can arrive after the page settles. */
const CONTROL_MS = Number(process.env.SWEEP_CONTROL_MS ?? (process.env.SWEEP_TARGET === "copy" ? "15000" : "5000"));
const appears = (locator: ReturnType<Page["locator"]>, timeout = CONTROL_MS) =>
  locator.waitFor({ state: "visible", timeout }).then(() => true, () => false);

const click = (name: RegExp | string, role: "button" | "tab" | "link" | "switch" = "button"): StateAction =>
  async (page) => {
    // The first visible match: a closed fold can hold hidden copies of the same control.
    const control = page
      .locator("main, [role=dialog]")
      .getByRole(role, { name, exact: typeof name === "string" })
      .filter({ visible: true })
      .first();
    if (!(await appears(control))) return `no ${role} named ${name}`;
    await control.scrollIntoViewIfNeeded();
    await control.click();
    return true;
  };

/** Open the first overflow / row action menu in main, to check popover placement. */
const firstMenu: StateAction = async (page) => {
  const trigger = page.locator('main [aria-haspopup="menu"]').filter({ visible: true }).first();
  if (!(await appears(trigger))) return "no row menu in main";
  await trigger.scrollIntoViewIfNeeded();
  await trigger.click();
  await page.locator("[role=menu]").first().waitFor({ state: "visible", timeout: 5_000 });
  return true;
};

/** Open Details for the first model name on the Models page. */
const firstModelDetails: StateAction = async (page) => {
  const name = page.locator('main button[aria-haspopup="dialog"]').filter({ visible: true }).first();
  if (!(await appears(name))) return "no model name in main";
  await name.scrollIntoViewIfNeeded();
  await name.click();
  await page.getByRole("dialog").waitFor({ state: "visible", timeout: 5_000 });
  return true;
};

const tab = (name: string): SweepState => ({
  id: `tab-${name.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`,
  act: click(name, "tab"),
});

const owner: Role[] = ["owner"];
const signedIn: Role[] = ["owner", "member"];

// Try tabs come from TAB_ITEMS in src/app/try/page.tsx; capability links from Home.
const tryTabs = ["chat", "image", "speech", "transcribe", "decide", "embed", "workflow"];
const tryLinks = ["audio", "video"];

export const ROUTES: SweepRoute[] = [
  {
    appRoute: "/",
    surface: "Home",
    path: "/",
    roles: ["owner", "member", "anonymous"],
    // Signed out, "/" is the landing; signed in, it is Home.
    states: [
      { id: "landing", roles: ["anonymous"] },
      { id: "default", roles: signedIn },
    ],
  },
  {
    appRoute: "/status",
    surface: "Status",
    path: "/status/",
    roles: signedIn,
    states: [
      { id: "default" },
      { id: "show-all", act: click("Show all"), optional: true },
    ],
  },
  {
    appRoute: "/library",
    surface: "Library",
    path: "/library/",
    roles: signedIn,
    states: [
      { id: "default" },
      { id: "filters-open", act: click(/^Filters/) },
      { id: "detail", act: click(/^Open /), optional: true },
    ],
  },
  {
    appRoute: "/try",
    surface: "Try",
    path: "/try/",
    roles: signedIn,
    states: [
      ...tryTabs.map((name) => ({ id: `tab-${name}`, url: () => `/try/?capability=${name}` })),
      ...tryLinks.map((name) => ({ id: `link-${name}`, url: () => `/try/?capability=${name}` })),
      {
        id: "workflow-picture",
        url: (ctx: SweepContext) =>
          ctx.pictureWorkflow ? `/try/?workflow=${encodeURIComponent(ctx.pictureWorkflow)}` : null,
      },
      {
        id: "workflow-picture-run-empty",
        url: (ctx: SweepContext) =>
          ctx.pictureWorkflow ? `/try/?workflow=${encodeURIComponent(ctx.pictureWorkflow)}` : null,
        // Pressing Run with nothing filled shows each field's message; it sends nothing.
        act: async (page) => {
          const run = page.locator("main [role=tabpanel]:visible").getByRole("button", { name: /^Run/ }).first();
          if (!(await appears(run))) return "no Run button";
          if (!(await run.isEnabled())) return "Run is disabled";
          await run.click();
          return true;
        },
        optional: true,
      },
    ],
  },
  {
    appRoute: "/people",
    surface: "People and invites",
    path: "/people/",
    roles: signedIn,
    states: [
      { id: "default" },
      { ...tab("Invites"), roles: owner },
      { ...tab("Access plans"), roles: owner },
      { id: "invite-dialog", url: () => "/people/?tab=invites", act: click(/invite someone/i), roles: owner },
      { id: "row-menu", act: firstMenu, roles: owner },
    ],
  },
  {
    appRoute: "/sign-in",
    surface: "Sign-in and invite acceptance",
    path: "/sign-in/",
    roles: ["anonymous", "owner"],
    states: [
      { id: "default" },
      {
        id: "wrong-password",
        roles: ["anonymous"],
        act: async (page) => {
          await page.getByLabel("Email", { exact: true }).fill("nobody@example.test");
          await page.getByLabel("Password", { exact: true }).fill("not the password");
          const answered = page.waitForResponse((response) => response.request().method() === "POST");
          await page.getByRole("button", { name: "Sign in", exact: true }).click();
          await answered;
          return true;
        },
      },
    ],
  },
  {
    appRoute: "/invite",
    surface: "Sign-in and invite acceptance",
    path: "/invite/",
    roles: ["anonymous", "member"],
    states: [
      { id: "no-token" },
      { id: "bad-token", url: () => "/invite/#token=not-a-real-invitation" },
      {
        id: "valid-token",
        url: (ctx) => (ctx.inviteToken ? `/invite/#token=${encodeURIComponent(ctx.inviteToken)}` : null),
        optional: true,
      },
    ],
  },
  {
    appRoute: "/usage",
    surface: "Usage",
    path: "/usage/",
    roles: signedIn,
    states: [
      { id: "default" },
      {
        id: "source-filter-open",
        roles: owner,
        act: async (page) => {
          const trigger = page.locator("#usage-source");
          if (!(await appears(trigger))) return "no Source filter";
          await trigger.scrollIntoViewIfNeeded();
          await trigger.click();
          await page.getByRole("listbox").first().waitFor({ state: "visible", timeout: 5_000 });
          return true;
        },
      },
      { id: "recent-requests", act: click("View recent requests"), optional: true },
    ],
  },
  {
    appRoute: "/activity",
    surface: "Activity",
    path: "/activity/",
    roles: signedIn,
    states: [
      { id: "default" },
      { ...tab("Changes"), roles: owner },
      { ...tab("Requests"), roles: owner },
      {
        id: "request-detail",
        roles: owner,
        act: async (page, ctx) => {
          const applied = await click("Requests", "tab")(page, ctx);
          if (applied !== true) return applied;
          const receipts = page.getByRole("button", { name: /^Request details:/ });
          const failed = receipts.filter({ hasText: "Failed" }).first();
          if (!(await appears(receipts.first(), CONTROL_MS * 2))) return "no request rows";
          const receipt = (await failed.count()) ? failed : receipts.first();
          await receipt.click();
          return true;
        },
        optional: true,
      },
    ],
  },
  {
    appRoute: "/models",
    surface: "Models",
    path: "/models/",
    roles: signedIn,
    states: [{ id: "default" }, { id: "details", act: firstModelDetails, roles: owner, optional: true }],
  },
  {
    appRoute: "/workflows",
    surface: "Workflows",
    path: "/workflows/",
    roles: signedIn,
    states: [
      { id: "default" },
      {
        id: "not-set-up-open",
        act: async (page) => {
          const fold = page.locator("main details summary", { hasText: /Not set up/ }).first();
          if (!(await appears(fold))) return "no “Not set up” fold";
          await fold.scrollIntoViewIfNeeded();
          await fold.click();
          return true;
        },
        optional: true,
      },
      { id: "detail", act: click("Details"), optional: true },
    ],
  },
  {
    appRoute: "/engines",
    surface: "Engines",
    path: "/engines/",
    roles: signedIn,
    states: [
      { id: "default" },
      {
        id: "show-not-in-use",
        roles: owner,
        act: async (page) => {
          const control = page.getByRole("switch", { name: "Show not in use" });
          if (!(await appears(control))) return "no Show not in use switch";
          if ((await control.getAttribute("aria-checked")) !== "true") await control.click();
          return true;
        },
        optional: true,
      },
      { id: "row-menu", act: firstMenu, roles: owner, optional: true },
    ],
  },
  {
    appRoute: "/providers",
    surface: "Providers",
    path: "/providers/",
    roles: signedIn,
    states: [{ id: "default" }, { id: "row-menu", act: firstMenu, roles: owner, optional: true }],
  },
  {
    appRoute: "/apps",
    surface: "Apps and keys",
    path: "/apps/",
    roles: signedIn,
    states: [
      { id: "default" },
      { id: "new-app", act: click("New app") },
      { id: "row-menu", act: firstMenu, optional: true },
    ],
  },
  { appRoute: "/settings", surface: "Settings", path: "/settings/", roles: signedIn, states: [{ id: "default" }] },
  { appRoute: "/account", surface: "Account", path: "/account/", roles: signedIn, states: [{ id: "default" }] },
  { appRoute: "/categories", surface: "Categories", path: "/categories/", roles: owner, states: [{ id: "default" }] },
  {
    appRoute: "/setup",
    surface: "Setup and first run",
    path: "/setup/",
    roles: ["anonymous", "owner"],
    states: [
      { id: "first-run", firstRun: true, roles: ["anonymous"] },
      { id: "claimed" },
    ],
  },
  {
    appRoute: "(not-found)",
    surface: "Page not found",
    path: "/no-such-page/",
    roles: ["owner", "member"],
    states: [{ id: "default" }],
  },
];

/**
 * Dev reference pages (route.dev === true) are not product surfaces and stay out of ROUTES:
 * the design page shows permanent loading examples (aria-busy, skeletons) by design, which
 * the sweep's settle contract would always report as never finished. They stay listed here so
 * the every-app-route coverage check still sees them.
 */
export const DEV_REFERENCE_ROUTES: SweepRoute[] = [
  { appRoute: "/design", surface: "Design reference (dev)", path: "/design/", roles: owner, dev: true, states: [{ id: "default" }] },
];

/** App routes discovered from src/app: every directory holding a page.tsx. */
export function discoverAppRoutes(appDir = path.resolve(__dirname, "../../src/app")): string[] {
  const found: string[] = [];
  const walk = (dir: string, route: string) => {
    if (existsSync(path.join(dir, "page.tsx")) || existsSync(path.join(dir, "page.jsx"))) found.push(route || "/");
    if (existsSync(path.join(dir, "not-found.tsx")) && route === "") found.push("(not-found)");
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      if (!entry.isDirectory() || entry.name.startsWith("_") || entry.name === "api") continue;
      // Route groups "(x)" do not add a path segment.
      const segment = /^\(.*\)$/.test(entry.name) ? "" : `/${entry.name}`;
      walk(path.join(dir, entry.name), `${route}${segment}`);
    }
  };
  walk(appDir, "");
  return found.sort();
}
