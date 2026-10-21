import { type Page } from "@playwright/test";
import { expect, test } from "./fixtures/isolation";
import AxeBuilder from "@axe-core/playwright";
import { workflowsPolishCatalog } from "./fixtures/workflows-polish";
import { seededCatalog, seededRecentJobs } from "./fixtures/workflows-seeded";
import { fulfilJson } from "./fixtures/intercept";

// Browser-only checks for the Workflows page: real layout geometry, the
// horizontal-overflow guard, axe, and the colour tokens of both themes. What the
// page says (copy, search, empty/error states, sheet content, availability) is
// tested in src/app/workflows/page.spec.tsx.

async function setSurface(
  page: Page,
  theme: "light" | "dark",
  width: 1440 | 768 | 390,
) {
  await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
  await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
  await page.addInitScript(
    (value) => localStorage.setItem("theme", value),
    theme,
  );
}

async function routeWorkflows(page: Page, payload: unknown, status = 200) {
  await page.route("**/me/api/workflows", (route) =>
    fulfilJson(route, payload, { status }),
  );
}

async function routeRecentJobs(page: Page, payload: unknown) {
  await page.route("**/me/api/media?**", (route) =>
    fulfilJson(route, payload),
  );
}

const noJobs = { assets: [], models: [], next_cursor: null };

async function preparePage(
  page: Page,
  theme: "light" | "dark",
  width: 1440 | 768 | 390,
  payload: unknown,
) {
  await setSurface(page, theme, width);
  await routeWorkflows(page, payload);
  await routeRecentJobs(page, noJobs);
  const response = await page.goto("/workflows/");
  expect(response?.status()).toBeLessThan(500);
  await expect(
    page.getByRole("heading", { name: /^Workflows/ }),
  ).toBeVisible();
}

async function assertNoSeriousAxe(page: Page) {
  const a11y = await new AxeBuilder({ page }).analyze();
  const violations = a11y.violations
    .filter((violation) =>
      ["serious", "critical"].includes(violation.impact ?? ""),
    )
    .map((violation) => violation.id);
  expect(violations).toEqual([]);
}

async function assertNoOverflow(page: Page) {
  await expect
    .poll(
      () =>
        page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
      { message: "Workflows page must fit the viewport" },
    )
    .toBe(true);
}

async function checkState(page: Page, screenshotName: string) {
  await assertNoOverflow(page);
  await assertNoSeriousAxe(page);
  await page.screenshot({
    path: `test-results/screens/${screenshotName}`,
    fullPage:
      !screenshotName.includes("sheet") && page.viewportSize()?.width !== 390,
  });
}

test("workflow rows use the available width on a phone and on desktop", async ({
  page,
}) => {
  // Craft PR 7: a group is one ListCard of full-width rows at every width,
  // 60 tall on desktop and 64 on a phone, instead of a 1, 2 or 3 column grid.
  for (const [width, rowHeight] of [
    [390, 64],
    [1440, 60],
  ] as const) {
    await preparePage(page, "light", width, workflowsPolishCatalog);
    await expect(
      page.getByRole("listitem", { name: "Product render" }),
    ).toBeVisible();
    // Groups follow declared outputs: two rows make pictures, one lists no output.
    const cards = page.getByLabel("Available workflows").getByRole("listitem");
    await expect(cards).toHaveCount(3);
    await expect
      .poll(
        () =>
          cards.evaluateAll((elements, minHeight) => {
            const rects = elements.map((element) =>
              element.getBoundingClientRect(),
            );
            const listWidth =
              elements[0].parentElement?.getBoundingClientRect().width ?? 0;
            return {
              oneColumn: new Set(rects.map((rect) => Math.round(rect.x))).size,
              tallEnough: rects.every(
                (rect) => Math.round(rect.height) >= minHeight,
              ),
              stacked: rects.every(
                (rect, index) => index === 0 || rect.y > rects[index - 1].y,
              ),
              fullWidth: Math.abs(rects[0].width - listWidth) < 2,
            };
          }, rowHeight - 1),
        { message: `rows at ${width}px` },
      )
      .toEqual({
        oneColumn: 1,
        tallEnough: true,
        stacked: true,
        fullWidth: true,
      });

    await page.getByPlaceholder("Search workflows").fill("product-render");
    await expect(cards).toHaveCount(1);
    await expect
      .poll(async () => {
        const singleton = await cards
          .first()
          .evaluate((card) => card.getBoundingClientRect().width);
        const content = await page
          .getByLabel("Available workflows")
          .evaluate((grid) => grid.getBoundingClientRect().width);
        return singleton > content - 4;
      })
      .toBe(true);
    await assertNoOverflow(page);
  }
});

test("the typed input table fits the desktop sheet and the narrow sheet does not overflow", async ({
  page,
}) => {
  for (const width of [1440, 390] as const) {
    await setSurface(page, "light", width);
    await routeWorkflows(page, workflowsPolishCatalog);
    await routeRecentJobs(page, noJobs);
    await page.goto("/workflows/");
    await page.getByRole("button", { name: /^Details for / }).first().click();

    const sheet = page.getByRole("dialog", { name: "Product render" });
    await expect(sheet).toBeVisible();
    // The typed input table sits inside the closed Technical details.
    await sheet.getByText("Technical details", { exact: true }).click();

    if (width === 1440) {
      const scrollport = sheet.getByLabel("Scrollable inputs");
      await expect(
        scrollport.getByRole("columnheader", { name: "Range", exact: true }),
      ).toBeVisible();
      await expect
        .poll(() =>
          scrollport.evaluate((element) => {
            const viewport = element.getBoundingClientRect();
            const columns = Array.from(
              element.querySelectorAll('th[data-slot="table-head"]'),
            ).map((column) => column.getBoundingClientRect());
            return {
              fits: element.scrollWidth <= element.clientWidth,
              columns: columns.length,
              inside: columns.every(
                (rect) =>
                  rect.left >= viewport.left && rect.right <= viewport.right,
              ),
            };
          }),
        )
        .toEqual({ fits: true, columns: 5, inside: true });
      // Tailwind wraps a long input name instead of widening the table.
      await expect(
        scrollport.getByRole("cell", { name: "guidance_scale", exact: true }),
      ).toHaveCSS("white-space", "normal");
    } else {
      await expect(
        sheet.getByRole("list", { name: "Input summaries" }),
      ).toBeVisible();
      await assertNoOverflow(page);
    }
  }
});

// One representative per theme: the colour tokens differ, the layout does not.
for (const [theme, width] of [
  ["light", 1440],
  ["dark", 390],
] as const) {
  test(`workflows seeded catalog fits and passes axe: ${theme} at ${width}px`, async ({
    page,
  }) => {
    await preparePage(page, theme, width, seededCatalog);
    await expect(
      page.getByRole("button", {
        name: "Details for Product render",
        exact: true,
      }),
    ).toBeVisible();
    await checkState(page, `workflows-seeded-${theme}-${width}.png`);
  });

  test(`workflow seeded sheet fits and passes axe: ${theme} at ${width}px`, async ({
    page,
  }) => {
    await setSurface(page, theme, width);
    await routeWorkflows(page, seededCatalog);
    await routeRecentJobs(page, seededRecentJobs);
    await page.goto("/workflows/");
    await page.getByRole("button", { name: /^Details for / }).first().click();
    await expect(
      page.getByRole("dialog", { name: "Product render" }),
    ).toBeVisible();
    await expect(page.getByText("A teal desk lamp on walnut")).toBeVisible();
    await checkState(page, `workflows-sheet-seeded-${theme}-${width}.png`);
  });
}
