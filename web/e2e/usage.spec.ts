import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const seededSummary = {
  requests: 4,
  media_count: 1,
  remote_spend: 0.023,
  remote_spend_available: true,
  spend_measured_requests: 3,
  spend_total_requests: 4,
  retention_days: 14,
  coverage_start: "2026-10-01T00:00:00Z",
  coverage_note: "Provider spend is available for matched receipts.",
  by_day: [
    { date: "2026-10-01", chat: 1, image: 1 },
    { date: "2026-10-02", chat: 1, embeddings: 1 },
  ],
  by_model: [
    { model: "demo-chat", capability: "chat", requests: 2, remote_spend: 0.01 },
    { model: "image", capability: "image", requests: 1, remote_spend: 0.013 },
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

async function expectNoSeriousAxeViolations(
  page: import("@playwright/test").Page,
) {
  const a11y = await new AxeBuilder({ page }).analyze();
  const violations = a11y.violations
    .filter((violation) =>
      ["serious", "critical"].includes(violation.impact ?? ""),
    )
    .map((violation) => violation.id);
  expect(violations, "Serious accessibility violations").toEqual([]);
}

async function expectNoHorizontalOverflow(
  page: import("@playwright/test").Page,
) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
    "Page must fit the viewport",
  ).toBe(true);
}

test("usage lists a receipt and opens technical details", async ({ page }) => {
  await page.route("**/me/api/usage/summary?**", (route) =>
    route.fulfill({ json: seededSummary }),
  );
  await page.goto("/ui/usage/");
  await expect(page.getByRole("heading", { name: "Usage" })).toBeVisible();
  await expect(
    page.getByText(
      /Receipts are retained for 14 days\s*, covering usage since Oct 1, 2026\s*\. Provider spend is available for matched receipts\./,
    ),
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

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390] as const) {
    test(`usage empty ${theme} at ${width}px`, async ({ page }) => {
      await prepareUsageViewport(page, theme, width);
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
      await expect(
        page.getByText("No usage this month", { exact: true }),
      ).toBeVisible();
      await expectNoHorizontalOverflow(page);
      await expectNoSeriousAxeViolations(page);
      await page.screenshot({
        path: `test-results/screens/usage-empty-${theme}-${width}.png`,
        fullPage: true,
      });
    });

    test(`usage error ${theme} at ${width}px`, async ({ page }) => {
      await prepareUsageViewport(page, theme, width);
      let failed = true;
      await page.route("**/me/api/usage/summary?**", (route) => {
        if (failed) {
          failed = false;
          return route.fulfill({
            status: 503,
            json: { error: { message: "Unavailable for this test" } },
          });
        }
        return route.fulfill({ json: seededSummary });
      });
      await page.goto("/ui/usage/");
      await expect(page.getByText(/Usage could not be loaded/i)).toBeVisible();
      await expectNoHorizontalOverflow(page);
      await expectNoSeriousAxeViolations(page);
      await page.screenshot({
        path: `test-results/screens/usage-error-${theme}-${width}.png`,
        fullPage: true,
      });
      await page.getByRole("button", { name: /try again/i }).click();
      await expect(page.getByText("demo-chat").first()).toBeVisible();
    });

    test(`usage ${theme} at ${width}px`, async ({ page }) => {
      await prepareUsageViewport(page, theme, width);
      await page.route("**/me/api/usage/summary?**", (route) =>
        route.fulfill({ json: seededSummary }),
      );
      await page.goto("/ui/usage/");
      await expect(page.getByRole("heading", { name: "Usage" })).toBeVisible();
      await expect(
        page.locator('[aria-label="Loading your account"]'),
      ).toHaveCount(0);
      await expect(page.getByText("Remote spend")).toBeVisible();
      await expect(page.getByText("Receipts", { exact: true })).toBeVisible();
      await expectNoHorizontalOverflow(page);
      await expectNoSeriousAxeViolations(page);
      await page.screenshot({
        path: `test-results/screens/usage-lane-${theme}-${width}.png`,
        fullPage: true,
      });
    });
  }
}

async function prepareUsageViewport(
  page: import("@playwright/test").Page,
  theme: "light" | "dark",
  width: 1440 | 390,
) {
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
}

test("usage labels unavailable remote spend honestly", async ({ page }) => {
  await page.route("**/me/api/usage/summary?**", (route) =>
    route.fulfill({
      json: {
        ...seededSummary,
        remote_spend: 0,
        remote_spend_available: false,
        spend_measured_requests: 0,
        spend_total_requests: 4,
      },
    }),
  );
  await page.goto("/ui/usage/");
  const remoteSpendCard = page
    .getByText("Remote spend", { exact: true })
    .locator("..");
  await expect(remoteSpendCard).toContainText("Not measured yet");
  await expect(remoteSpendCard).not.toContainText("$0");
});

test("usage reads the real account summary and seeded receipt", async ({
  page,
  request,
}) => {
  const session = await request.get("/me/api/session");
  expect(session.status()).toBe(200);
  const account = await session.json();
  expect(
    account.authenticated,
    "Seeded owner must have an account session",
  ).toBe(true);
  const response = await request.get("/me/api/usage/summary?period=month");
  expect(response.status(), "Summary must accept the same account cookie").toBe(
    200,
  );
  const summary = await response.json();
  expect(summary.requests).toBeGreaterThan(0);
  expect(
    summary.receipts.some(
      (receipt: { requested_model: string }) =>
        receipt.requested_model === "demo-chat",
    ),
  ).toBe(true);
  await page.goto("/ui/usage/");
  await expect(
    page.getByRole("button", { name: /Receipt details for demo-chat/ }).first(),
  ).toBeVisible();
  await page
    .getByRole("button", { name: /Receipt details for demo-chat/ })
    .first()
    .click();
  await expect(
    page.getByRole("dialog", { name: "Request receipt" }),
  ).toContainText("succeeded");
});
