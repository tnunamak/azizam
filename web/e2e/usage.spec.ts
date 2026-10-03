import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const seededSummary = {
  requests: 4,
  media_count: 1,
  remote_spend: 0.023,
  remote_spend_available: true,
  retention_days: 14,
  coverage_note: "14-day receipt window",
  by_day: [
    { date: "2026-10-01", chat: 1, image: 1 },
    { date: "2026-10-02", chat: 1, embeddings: 1 },
  ],
  by_model: [
    { model: "demo-chat", requests: 2, remote_spend: 0.01 },
    { model: "image", requests: 1, remote_spend: 0.013 },
  ],
  receipts: [
    {
      request_id: "req_demo_chat",
      accepted_at_ms: 1790870400000,
      requested_model: "demo-chat",
      resolved_model: "demo-chat",
      intent: "chat",
      capability: "chat",
      status: "success",
      latency_ms: 42,
      remote_spend: null,
    },
  ],
};

async function expectNoSeriousAxeViolations(page: import("@playwright/test").Page) {
  const a11y = await new AxeBuilder({ page }).analyze();
  const violations = a11y.violations
    .filter((violation) =>
      ["serious", "critical"].includes(violation.impact ?? ""),
    )
    .map((violation) => violation.id);
  expect(violations, "Serious accessibility violations").toEqual([]);
}

async function expectNoHorizontalOverflow(page: import("@playwright/test").Page) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
    "Page must fit the viewport",
  ).toBe(true);
}

test("usage lists a receipt and opens technical details", async ({
  page,
}) => {
  await page.route("**/me/api/usage/summary?**", (route) =>
    route.fulfill({ json: seededSummary }),
  );
  await page.goto("/ui/usage/");
  await expect(page.getByRole("heading", { name: "Usage" })).toBeVisible();
  await expect(
    page.getByText(/Receipts are retained for 14 days, covering usage/),
  ).toBeVisible();
  await expect(page.getByText("demo-chat").first()).toBeVisible();
  await page
    .getByRole("button", { name: /Receipt details for demo-chat/ })
    .first()
    .click();
  const sheet = page.getByRole("dialog", { name: "Request receipt" });
  await expect(sheet).toBeVisible();
  await expect(sheet).toContainText("demo-chat");
  await sheet.getByText("Technical details", { exact: true }).click();
  await expect(sheet).toContainText("request_id");
  await expect(sheet).toContainText("requested_model");
});

test("usage has designed empty and error states", async ({ page }) => {
  await page.route("**/me/api/usage/summary?**", (route) =>
    route.fulfill({
      json: {
        requests: 0,
        media_count: 0,
        remote_spend: 0,
        remote_spend_available: false,
        retention_days: 14,
        by_day: [],
        by_model: [],
        receipts: [],
      },
    }),
  );
  await page.goto("/ui/usage/");
  await expect(page.getByText("No usage this month", { exact: true })).toBeVisible();
  await expect(
    page.getByText("Receipts are retained for 14 days"),
  ).toBeVisible();

  await page.unroute("**/me/api/usage/summary?**");
  await page.route("**/me/api/usage/summary?**", (route) =>
    route.fulfill({
      status: 503,
      json: { error: { message: "Unavailable for this test" } },
    }),
  );
  await page.reload();
  await expect(
    page.getByText("Usage could not be loaded. Try again."),
  ).toBeVisible();
  await page.unroute("**/me/api/usage/summary?**");
  await page.route("**/me/api/usage/summary?**", (route) =>
    route.fulfill({ json: seededSummary }),
  );
  await page.getByRole("button", { name: /try again/i }).click();
  await expect(page.getByText("demo-chat").first()).toBeVisible();
});

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390] as const) {
    test(`usage ${theme} at ${width}px`, async ({ page }) => {
      await page.setViewportSize({
        width,
        height: width === 390 ? 844 : 1000,
      });
      await page.emulateMedia({
        colorScheme: theme,
        reducedMotion: "reduce",
      });
      await page.addInitScript((value) => {
        localStorage.setItem("theme", value);
      }, theme);
      await page.route("**/me/api/usage/summary?**", (route) =>
        route.fulfill({ json: seededSummary }),
      );
      await page.goto("/ui/usage/");
      await expect(page.getByRole("heading", { name: "Usage" })).toBeVisible();
      await expect(page.locator('[aria-label="Loading your account"]')).toHaveCount(0);
      await expect(page.getByText("Remote spend")).toBeVisible();
      await expect(
        page.getByText("Receipts", { exact: true }),
      ).toBeVisible();
      await expectNoHorizontalOverflow(page);
      await expectNoSeriousAxeViolations(page);
      await page.screenshot({
        path: `test-results/screens/usage-lane-${theme}-${width}.png`,
        fullPage: true,
      });
    });
  }
}
