import { expect, test } from "./fixtures/isolation";

// Every top-level page must fit a 390 px phone: no sideways page scroll. The
// owner session is the seeded one, so owner-only pages are included.
const routes = [
  "/",
  "/try/",
  "/library/",
  "/models/",
  "/usage/",
  "/activity/",
  "/apps/",
  "/people/",
  "/providers/",
  "/engines/",
  "/status/",
  "/settings/",
  "/workflows/",
  "/setup/",
  "/design/",
];

// Layout geometry does not depend on the colour scheme (themes only swap
// colour tokens), so every route is measured once in dark. One light run on the
// home route keeps the light bootstrap path covered.
const cases = [
  ...routes.map((route) => ({ route, theme: "dark" as const })),
  { route: "/", theme: "light" as const },
];

for (const { route, theme } of cases) {
  test(`${route} has no horizontal page overflow at 390 px (${theme})`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
    await page.addInitScript(
      (value) => localStorage.setItem("theme", value),
      theme,
    );
    await page.goto(route);
    await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible();
    // The design page shows skeletons on purpose; every other route must have
    // finished loading before it is measured.
    if (route !== "/design/")
      await expect(
        page.locator(
          '[aria-busy="true"]:visible, [data-slot="skeleton"]:visible',
        ),
      ).toHaveCount(0);
    await expect
      .poll(
        () =>
          page.evaluate(() => {
            const root = document.scrollingElement ?? document.documentElement;
            const wide = [...document.querySelectorAll<HTMLElement>("body *")]
              .filter(
                (node) =>
                  node.getBoundingClientRect().right > window.innerWidth + 1,
              )
              .slice(0, 5)
              .map(
                (node) =>
                  `${node.tagName.toLowerCase()}.${String(node.className).slice(0, 60)}`,
              );
            return root.scrollWidth <= window.innerWidth
              ? "fits"
              : `wide elements: ${wide.join(" | ")}`;
          }),
        { message: `${route} (${theme}) overflows 390 px` },
      )
      .toBe("fits");
  });
}
