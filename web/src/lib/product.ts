import identity from "../../../gateway/product_identity.json";

export const PRODUCT_NAME = identity.name;

export function canonicalDestinationPath(path: string): string {
  const normalized = path.replace(/^\/ui(?=\/|$)/, "").replace(/\/$/, "");
  if (normalized === "/services") return "/engines";
  return normalized === "" || normalized === "/home" ? "/" : normalized;
}

// Next adds /ui automatically to Link hrefs. These are application paths.
// "You" is ordered by how often a household member needs each place.
export const destinations = [
  {
    title: "Home",
    href: "/",
    answer: "Here's how to use your AI.",
    group: "You",
  },
  {
    title: "Library",
    href: "/library",
    answer: "Everything I've made.",
    group: "You",
  },
  {
    title: "Try",
    href: "/try",
    answer: "Does it work, and what does the request look like?",
    group: "You",
  },
  {
    title: "Apps & keys",
    href: "/apps",
    answer: "Which apps can use my account?",
    group: "You",
  },
  {
    title: "Models",
    href: "/models",
    answer: "What can I use, and which one should I pick?",
    group: "You",
  },
  {
    title: "Workflows",
    href: "/workflows",
    answer: "What can I create with a named workflow?",
    group: "You",
  },
  {
    title: "Usage",
    href: "/usage",
    answer: "How much am I using, and what does it cost?",
    group: "You",
  },
  {
    title: "Status",
    href: "/status",
    answer: "Is everything working?",
    group: "Gateway",
  },
  {
    title: "Engines",
    href: "/engines",
    answer: "The local engines that power your AI.",
    group: "Gateway",
  },
  {
    title: "Providers",
    href: "/providers",
    answer: "Choose which remote providers your gateway can use.",
    group: "Gateway",
  },
  {
    title: "People",
    href: "/people",
    answer: "Who can use your gateway?",
    group: "Gateway",
  },
  {
    title: "Activity",
    href: "/activity",
    answer: "Who changed what, and when?",
    group: "Gateway",
  },
  {
    title: "Settings",
    href: "/settings",
    answer: "Make this gateway yours.",
    group: "Gateway",
  },
] as const;
