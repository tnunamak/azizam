import { expect, test } from "@playwright/test";

// Every top-level page must fit a 390 px phone: no sideways page scroll. The
// owner session is the seeded one, so owner-only pages are included.
const routes = [
  "/ui/",
  "/ui/home/",
  "/ui/try/",
  "/ui/library/",
  "/ui/models/",
  "/ui/usage/",
  "/ui/activity/",
  "/ui/apps/",
  "/ui/people/",
  "/ui/providers/",
  "/ui/engines/",
  "/ui/services/",
  "/ui/status/",
  "/ui/settings/",
  "/ui/workflows/",
  "/ui/setup/",
  "/ui/gallery/",
  "/ui/showcase/",
  "/ui/design/",
];

for (const theme of ["dark", "light"] as const) {
  for (const route of routes) {
    test(`${route} has no horizontal page overflow at 390 px (${theme})`, async ({ page }) => {
      await page.setViewportSize({ width: 390, height: 844 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      await page.addInitScript((value) => localStorage.setItem("theme", value), theme);
      await page.goto(route);
      await page.waitForLoadState("networkidle");
      const widths = await page.evaluate(() => {
        const root = document.scrollingElement ?? document.documentElement;
        const wide = [...document.querySelectorAll<HTMLElement>("body *")]
          .filter((node) => node.getBoundingClientRect().right > window.innerWidth + 1)
          .slice(0, 5)
          .map((node) => `${node.tagName.toLowerCase()}.${String(node.className).slice(0, 60)}`);
        return { scroll: root.scrollWidth, inner: window.innerWidth, wide };
      });
      expect(widths.scroll, `wide elements: ${widths.wide.join(" | ")}`).toBeLessThanOrEqual(
        widths.inner,
      );
    });
  }
}
