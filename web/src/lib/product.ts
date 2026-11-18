import identity from "../../../gateway/product_identity.json";
import registry from "../../../gateway/ui_pages.json";

export const PRODUCT_NAME = identity.name;
export const PRODUCT_DESCRIPTION =
  "Your own AI provider. Connect your apps and manage your gateway.";
// The link preview's line.
export const PRODUCT_TAGLINE =
  "One AI gateway for your apps and the people you invite.";

// gateway/ui_pages.json is the one list of pages. The gateway serves, guards
// and sanitizes return paths from it; the shell takes public pages, owner
// gating and the Gateway nav group from it.
export type PageAccess = "public" | "member" | "admin";
const ACCESS_CLASSES: readonly string[] = ["public", "member", "admin"];
for (const [name, access] of Object.entries(registry.pages)) {
  if (!ACCESS_CLASSES.includes(access))
    throw new Error(`gateway/ui_pages.json: page "${name}" has access "${access}"`);
}
export const uiPages = registry.pages as Readonly<Record<string, PageAccess>>;

/** The registry page that serves an app path ("/" is Home, ""), if any. */
export function pageNameFor(path: string): string | undefined {
  const name = path.split(/[?#]/, 1)[0].split("/")[1] ?? "";
  return Object.hasOwn(uiPages, name) ? name : undefined;
}

export function pageAccess(path: string): PageAccess | undefined {
  const name = pageNameFor(path);
  return name === undefined ? undefined : uiPages[name];
}

export function canonicalDestinationPath(path: string): string {
  const normalized = path.replace(/\/$/, "");
  return normalized === "" ? "/" : normalized;
}

type View = { title: string; href: string };
type Destination = {
  title: string;
  href: string;
  answer: string;
  /** Pages that are views of this destination; it is current on each. */
  views?: readonly View[];
};

// Status is a member page, but it describes the gateway, so it sits in the
// Gateway group; every admin page does too.
function navGroup(href: string): "You" | "Gateway" {
  return href === "/status" || pageAccess(href) === "admin" ? "Gateway" : "You";
}

// "You" is ordered by how often a household member needs each place.
const destinationList = [
  {
    title: "Home",
    href: "/",
    answer: "Here's how to use your AI.",
  },
  {
    title: "Library",
    href: "/library",
    answer: "Everything I've made.",
  },
  {
    title: "Try",
    href: "/try",
    answer: "Does it work, and what does the request look like?",
  },
  {
    title: "History",
    href: "/history",
    answer: "What did I ask, and what came back?",
  },
  {
    title: "Apps & keys",
    href: "/apps",
    answer: "Which apps can use my account?",
  },
  {
    title: "Connect",
    href: "/connect",
    answer: "How do I use this gateway from another app?",
  },
  {
    // One destination with two peer views (walkthrough B5); the pages switch
    // between them with their own tabs.
    title: "Discover",
    href: "/models",
    answer: "Which models and workflows can I use?",
    views: [
      { title: "Models", href: "/models" },
      { title: "Workflows", href: "/workflows" },
    ],
  },
  {
    title: "Usage",
    href: "/usage",
    answer: "How much am I using, and what does it cost?",
  },
  {
    title: "Status",
    href: "/status",
    answer: "Is everything working?",
  },
  {
    title: "Engines",
    href: "/engines",
    answer: "The local engines that power your AI.",
  },
  {
    title: "Providers",
    href: "/providers",
    answer: "Choose which remote providers your gateway can use.",
  },
  {
    title: "People",
    href: "/people",
    answer: "Who can use your gateway?",
  },
  {
    title: "Categories",
    href: "/categories",
    answer: "Which category rules apply to people and apps?",
  },
  {
    title: "Activity",
    href: "/activity",
    answer: "Who changed what, and when?",
  },
  {
    title: "Settings",
    href: "/settings",
    answer: "Make this gateway yours.",
  },
] as const satisfies readonly Destination[];

export const destinations = destinationList.map((destination) => ({
  ...destination,
  views: ("views" in destination
    ? destination.views
    : [{ title: destination.title, href: destination.href }]) as readonly View[],
  group: navGroup(destination.href),
}));

/** Whether a page path is one of the destination's views. */
export function isCurrentDestination(
  destination: { views: readonly View[] },
  path: string,
): boolean {
  const current = canonicalDestinationPath(path);
  return destination.views.some(
    (view) => canonicalDestinationPath(view.href) === current,
  );
}
