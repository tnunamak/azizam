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
    const control = page.locator("main, [role=dialog]").getByRole(role, { name, exact: typeof name === "string" }).first();
    if (!(await appears(control))) return `no ${role} named ${name}`;
    await control.scrollIntoViewIfNeeded();
    await control.click();
    return true;
  };

/** Open the first overflow / row action menu in main, to check popover placement. */
const firstMenu: StateAction = async (page) => {
  const trigger = page.locator('main [aria-haspopup="menu"]').first();
  if (!(await appears(trigger))) return "no row menu in main";
  await trigger.scrollIntoViewIfNeeded();
  await trigger.click();
  await page.locator("[role=menu]").first().waitFor({ state: "visible", timeout: 5_000 });
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
    path: "/ui/",
    roles: ["owner", "member", "anonymous"],
    states: [{ id: "landing" }],
  },
  { appRoute: "/home", surface: "Home", path: "/ui/home/", roles: signedIn, states: [{ id: "default" }] },
  {
    appRoute: "/status",
    surface: "Status",
    path: "/ui/status/",
    roles: signedIn,
    states: [
      { id: "default" },
      { id: "show-all", act: click("Show all"), optional: true },
    ],
  },
  {
    appRoute: "/library",
    surface: "Library",
    path: "/ui/library/",
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
    path: "/ui/try/",
    roles: signedIn,
    states: [
      ...tryTabs.map((name) => ({ id: `tab-${name}`, url: () => `/ui/try/?capability=${name}` })),
      ...tryLinks.map((name) => ({ id: `link-${name}`, url: () => `/ui/try/?capability=${name}` })),
      {
        id: "workflow-picture",
        url: (ctx: SweepContext) =>
          ctx.pictureWorkflow ? `/ui/try/?workflow=${encodeURIComponent(ctx.pictureWorkflow)}` : null,
      },
      {
        id: "workflow-picture-run-empty",
        url: (ctx: SweepContext) =>
          ctx.pictureWorkflow ? `/ui/try/?workflow=${encodeURIComponent(ctx.pictureWorkflow)}` : null,
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
    path: "/ui/people/",
    roles: signedIn,
    states: [
      { id: "default" },
      { ...tab("Invites"), roles: owner },
      { ...tab("Access plans"), roles: owner },
      { id: "invite-dialog", url: () => "/ui/people/?tab=invites", act: click(/invite someone/i), roles: owner },
      { id: "row-menu", act: firstMenu, roles: owner },
    ],
  },
  {
    appRoute: "/sign-in",
    surface: "Sign-in and invite acceptance",
    path: "/ui/sign-in/",
    roles: ["anonymous", "owner"],
    states: [
      { id: "default" },
      {
        id: "wrong-password",
        roles: ["anonymous"],
        act: async (page) => {
          await page.getByLabel("Email", { exact: true }).fill("nobody@example.test");
          await page.getByLabel("Password", { exact: true }).fill("not the password");
          await page.getByRole("button", { name: "Sign in", exact: true }).click();
          await page.waitForTimeout(1_000);
          return true;
        },
      },
    ],
  },
  {
    appRoute: "/invite",
    surface: "Sign-in and invite acceptance",
    path: "/ui/invite/",
    roles: ["anonymous", "member"],
    states: [
      { id: "no-token" },
      { id: "bad-token", url: () => "/ui/invite/#token=not-a-real-invitation" },
      {
        id: "valid-token",
        url: (ctx) => (ctx.inviteToken ? `/ui/invite/#token=${encodeURIComponent(ctx.inviteToken)}` : null),
        optional: true,
      },
    ],
  },
  {
    appRoute: "/usage",
    surface: "Usage",
    path: "/ui/usage/",
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
    path: "/ui/activity/",
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
    path: "/ui/models/",
    roles: signedIn,
    states: [{ id: "default" }, { id: "row-menu", act: firstMenu, roles: owner, optional: true }],
  },
  {
    appRoute: "/workflows",
    surface: "Workflows",
    path: "/ui/workflows/",
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
    path: "/ui/engines/",
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
    appRoute: "/services",
    surface: "Engines",
    path: "/ui/services/",
    roles: owner,
    states: [{ id: "legacy-path" }],
  },
  {
    appRoute: "/providers",
    surface: "Providers",
    path: "/ui/providers/",
    roles: signedIn,
    states: [{ id: "default" }, { id: "row-menu", act: firstMenu, roles: owner, optional: true }],
  },
  {
    appRoute: "/apps",
    surface: "Apps and keys",
    path: "/ui/apps/",
    roles: signedIn,
    states: [
      { id: "default" },
      { id: "new-app", act: click("New app") },
      { id: "row-menu", act: firstMenu, optional: true },
    ],
  },
  { appRoute: "/settings", surface: "Settings", path: "/ui/settings/", roles: signedIn, states: [{ id: "default" }] },
  {
    appRoute: "/setup",
    surface: "Setup and first run",
    path: "/ui/setup/",
    roles: ["anonymous", "owner"],
    states: [
      { id: "first-run", firstRun: true, roles: ["anonymous"] },
      { id: "claimed" },
    ],
  },
  { appRoute: "/design", surface: "Design reference (dev)", path: "/ui/design/", roles: owner, dev: true, states: [{ id: "default" }] },
  { appRoute: "/gallery", surface: "Design reference (dev)", path: "/ui/gallery/", roles: owner, dev: true, states: [{ id: "default" }] },
  { appRoute: "/showcase", surface: "Design reference (dev)", path: "/ui/showcase/", roles: owner, dev: true, states: [{ id: "default" }] },
  {
    appRoute: "(not-found)",
    surface: "Page not found",
    path: "/ui/no-such-page/",
    roles: ["owner", "member"],
    states: [{ id: "default" }],
  },
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
