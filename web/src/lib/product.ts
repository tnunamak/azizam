import identity from "../../../gateway/product_identity.json";
import registry from "../../../gateway/ui_pages.json";

export const PRODUCT_NAME = identity.name;

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

type Destination = {
  title: string;
  href: string;
  answer: string;
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
    title: "Models",
    href: "/models",
    answer: "What can I use, and which one should I pick?",
  },
  {
    title: "Workflows",
    href: "/workflows",
    answer: "What can I create with a named workflow?",
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
  group: navGroup(destination.href),
}));
