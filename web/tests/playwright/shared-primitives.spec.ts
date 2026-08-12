import { expect, test } from "@playwright/test";

const widths = [639, 640, 767, 768, 979, 980] as const;

async function assertGeometry(
  page: import("@playwright/test").Page,
  width: number,
) {
  const geometry = await page.evaluate(() => {
    const viewportWidth = window.innerWidth;
    const rect = (selector: string) => {
      const element = document.querySelector<HTMLElement>(selector);
      if (!element) throw new Error(`Missing ${selector}`);
      const box = element.getBoundingClientRect();
      return {
        left: box.left,
        right: box.right,
        top: box.top,
        bottom: box.bottom,
      };
    };
    const scrollWidths = [
      document.documentElement,
      document.body,
      document.querySelector<HTMLElement>(".vf-console-shell"),
      document.querySelector<HTMLElement>(".vf-console-stage"),
      ...Array.from(
        document.querySelectorAll<HTMLElement>(".vf-mobile-data-card"),
      ),
    ].map((element) => {
      if (!element)
        throw new Error("Missing shared primitive containment element");
      return {
        scrollWidth: element.scrollWidth,
        clientWidth: element.clientWidth,
      };
    });
    const cardDescendants = Array.from(
      document.querySelectorAll<HTMLElement>(".vf-mobile-data-card *"),
    )
      .map((element) => element.getBoundingClientRect())
      .filter((box) => box.width > 0 && box.height > 0)
      .map((box) => ({ left: box.left, right: box.right }));
    return {
      shell: rect(".vf-console-shell"),
      stage: rect(".vf-console-stage"),
      cards: rect("[data-testid='shared-primitives-stage']"),
      scrollWidths,
      cardDescendants,
      viewportWidth,
    };
  });

  expect(geometry.viewportWidth).toBe(width);
  expect(geometry.shell.left).toBeGreaterThanOrEqual(0);
  expect(geometry.shell.right).toBeLessThanOrEqual(width);
  for (const item of geometry.scrollWidths) {
    expect(item.scrollWidth).toBeLessThanOrEqual(item.clientWidth);
  }
  for (const descendant of geometry.cardDescendants) {
    expect(descendant.left).toBeGreaterThanOrEqual(0);
    expect(descendant.right).toBeLessThanOrEqual(width);
  }
}

test("actual React shell preserves native card controls over the primary overlay", async ({
  page,
}) => {
  await page.setViewportSize({ width: 639, height: 812 });
  await page.goto("/ui/admin/status/p1v-fixture/");
  const status = page.getByTestId("event-status");

  await page
    .getByRole("button", { name: /Nested action: Actionable card/ })
    .click();
  await expect(status).toHaveText("actionable-head");
  await page
    .getByRole("link", { name: /this-is-an-intentionally-long-route-value/ })
    .first()
    .click();
  await expect(status).toHaveText("actionable-link");
  await page
    .getByRole("button", { name: "More actions for Actionable card" })
    .click();
  await expect(status).toHaveText("actionable-menu");
  await page
    .getByRole("button", { name: "Open Actionable card" })
    .click({ position: { x: 4, y: 4 } });
  await expect(status).toHaveText("actionable-primary");
});

test("actual React shell has containment and the exact 980px visibility switch", async ({
  page,
}) => {
  for (const width of widths) {
    await page.setViewportSize({ width, height: 812 });
    await page.goto("/ui/admin/status/p1v-fixture/");
    const nav = page.getByRole("navigation", { name: "Primary navigation" });
    if (width < 980) {
      await expect(nav).toBeVisible();
      const [stage, navBox] = await Promise.all([
        page.locator(".vf-console-stage").boundingBox(),
        nav.boundingBox(),
      ]);
      expect(stage?.y ?? 0).toBeLessThanOrEqual(navBox?.y ?? 0);
      expect((stage?.y ?? 0) + (stage?.height ?? 0)).toBeLessThanOrEqual(
        navBox?.y ?? 0,
      );
    } else {
      await expect(nav).toBeHidden();
    }
    await assertGeometry(page, width);
  }
});

test("actual React shell exposes the canonical destinations and one current item", async ({
  page,
}) => {
  await page.setViewportSize({ width: 639, height: 812 });
  await page.goto("/ui/admin/status/p1v-fixture/");
  await expect(
    page.getByRole("navigation", { name: "Primary navigation" }),
  ).toContainText("OverviewStatusServicesAppsMore");
  await expect(
    page
      .getByRole("navigation", { name: "Primary navigation" })
      .locator('[aria-current="page"]'),
  ).toHaveCount(1);
  await expect(
    page
      .getByRole("navigation", { name: "Primary navigation" })
      .getByRole("link", { name: "Status", exact: true }),
  ).toHaveAttribute("aria-current", "page");

  await page.goto("/ui/admin/profiles/p1v-fixture/");
  await expect(
    page
      .getByRole("navigation", { name: "Primary navigation" })
      .locator('[aria-current="page"]'),
  ).toHaveCount(1);
  const more = page.getByRole("button", { name: "More navigation" });
  await expect(more).toHaveAttribute("aria-current", "page");
  await more.click();
  await expect(page.getByRole("dialog", { name: "Navigation" })).toContainText(
    "ProfilesUsersAuditSettings",
  );
});
