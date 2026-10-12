import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("requests refresh visible content on focus and keep metadata on one line", async ({ page }) => {
  let refreshed = false;
  let loads = 0;
  await page.clock.setFixedTime(new Date("2026-10-12T12:00:00Z"));
  await page.route("**/admin/api/activity/requests?*", (route) => {
    loads += 1;
    return route.fulfill({
      json: {
        records: [{
          person_label: refreshed ? "After focus" : "Before focus",
          app_label: refreshed ? "Fresh notebook" : "Original notebook",
          created_at: 1_791_806_400,
          receipt: {
            request_id: refreshed ? "focus-new-receipt" : "focus-old-receipt",
            intent: "chat",
            requested_model: "demo-chat",
            resolved_model: "demo-chat",
            status: refreshed ? "failed" : "succeeded",
            latency_ms: refreshed ? 250 : 100,
          },
        }],
        people: [],
        next_cursor: null,
      },
    });
  });

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/activity/");
  await page.getByRole("tab", { name: "Requests", exact: true }).click();
  const oldRow = page.getByRole("button", { name: "Request details: Chat by Before focus", exact: true });
  await expect(oldRow).toBeVisible();
  await expect(oldRow).toContainText("Completed");
  await expect(oldRow).toContainText("Original notebook");
  await expect(page.getByRole("combobox", { name: "Person", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Refresh", exact: true })).toHaveCount(0);

  const before = loads;
  refreshed = true;
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  const freshRow = page.getByRole("button", { name: "Request details: Chat by After focus", exact: true });
  await expect(freshRow).toBeVisible();
  await expect(freshRow).toContainText("Failed");
  await expect(freshRow).toContainText("Fresh notebook");
  await expect(oldRow).toHaveCount(0);
  await expect(page.getByRole("button", { name: /^Request details:/ })).toHaveCount(1);
  expect(loads).toBeGreaterThan(before);
  expect(await freshRow.locator("p").nth(1).evaluate((element) => element.clientHeight))
    .toBeLessThanOrEqual(20);

  await freshRow.click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("After focus");
  await expect(dialog).toContainText("Failed");
  const technical = page.getByLabel("Technical request details");
  await dialog.getByText("Technical details", { exact: true }).click();
  await expect(technical).toContainText("focus-new-receipt");
  await expect(technical).not.toContainText("focus-old-receipt");
});


// Display states (empty, error, copy, filters, receipt privacy) are asserted in
// web/src/app/activity/*.spec.tsx. The browser keeps one representative per
// concept that jsdom cannot check: sheet geometry at 390 px, axe in light and
// dark, and the real-server person filter.
const noHorizontalScroll = () =>
  document.documentElement.scrollWidth <= innerWidth;
async function expectNoSeriousViolations(page: Page) {
  const { violations } = await new AxeBuilder({ page }).analyze();
  expect(
    violations.filter((v) => ["serious", "critical"].includes(v.impact ?? "")),
  ).toEqual([]);
}
async function prepare(
  page: Page,
  theme: "light" | "dark",
  width: number,
) {
  await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
  await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
  await page.addInitScript((value) => localStorage.setItem("theme", value), theme);
}

for (const [theme, width] of [
  ["light", 390],
  ["dark", 1440],
] as const) {
  test(`activity requests detail sheet fits and passes axe: ${theme} ${width}`, async ({
    page,
  }) => {
    await prepare(page, theme, width);
    await page.goto("/activity/");
    await page.getByRole("tab", { name: "Requests", exact: true }).click();
    const rows = page.getByRole("button", { name: /Request details:/ });
    await expect(rows.first()).toBeVisible();
    await rows.first().click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await dialog.getByText("Technical details", { exact: true }).click();
    await expect(dialog.locator("pre")).toBeVisible();
    await expect
      .poll(() =>
        dialog.evaluate((element) => element.scrollWidth <= element.clientWidth),
      )
      .toBe(true);
    await expectNoSeriousViolations(page);
    await page.keyboard.press("Escape");
    await page.getByRole("combobox", { name: "Person", exact: true }).click();
    const options = page.getByRole("option");
    await expect(options.nth(1)).toBeVisible();
    await options.nth(1).click();
    await expect(rows.first()).toBeVisible();
    await page.getByRole("button", { name: "Clear filters" }).click();
    await expect.poll(() => page.evaluate(noHorizontalScroll)).toBe(true);
    await expectNoSeriousViolations(page);
  });
}

test("activity audit list fits at 390 and passes axe", async ({ page }) => {
  await prepare(page, "light", 390);
  await page.goto("/activity/");
  await page.getByRole("tab", { name: "Changes", exact: true }).click();
  await expect(
    page.getByRole("button", { name: /Details:/ }).first(),
  ).toBeVisible();
  await expect.poll(() => page.evaluate(noHorizontalScroll)).toBe(true);
  await expectNoSeriousViolations(page);
});

test("request errors retry and audit details use readable timestamps", async ({
  page,
}) => {
  let fail = true;
  await page.route("**/admin/api/activity/requests?*", (route) =>
    fail ? route.fulfill({ status: 503, body: "{}" }) : route.continue(),
  );
  await page.goto("/activity/");
  await page.getByRole("tab", { name: "Changes", exact: true }).click();
  await page
    .getByRole("button", { name: /Details:/ })
    .first()
    .click();
  await page
    .getByRole("dialog")
    .getByText("Technical details", { exact: true })
    .click();
  await expect(page.getByRole("dialog").locator("pre")).toContainText(
    /"created_at": "\d{4}-\d{2}-\d{2}T/,
  );
  await page.keyboard.press("Escape");
  await page.getByRole("tab", { name: "Requests", exact: true }).click();
  await expect(
    page.getByText("Couldn't load requests", { exact: true }),
  ).toBeVisible();
  fail = false;
  await page.getByRole("button", { name: /Retry|Try again/ }).click();
  await expect(
    page.getByRole("button", { name: /Request details:/ }).first(),
  ).toBeVisible();
});
