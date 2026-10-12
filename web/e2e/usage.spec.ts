import { test, expect } from "./fixtures/isolation";
import AxeBuilder from "@axe-core/playwright";

const seededSummary = {
  requests: 4,
  media_count: 1,
  remote_spend: 0.023,
  remote_spend_available: true,
  paid_requests: 3,
  spend_measured_requests: 3,
  spend_total_requests: 4,
  retention_days: null,
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
      latency_ms: 104,
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
  await expect
    .poll(
      () =>
        page.evaluate(
          () =>
            (document.scrollingElement ?? document.documentElement).scrollWidth -
            window.innerWidth,
        ),
      { message: "Page must fit the viewport" },
    )
    .toBeLessThanOrEqual(0);
}

// Display states (copy, empty, error, spend labels, model names, source ranking,
// partial totals) are asserted in web/src/app/usage/page.spec.tsx. The browser
// keeps what jsdom cannot judge: layout at a real width, contrast under both
// themes (axe) and the Source popover's geometry.

test("usage light at 1440px has no serious axe violation", async ({ page }) => {
  await prepareUsageViewport(page, "light", 1440);
  await page.route("**/me/api/usage/summary?**", (route) =>
    route.fulfill({ json: seededSummary }),
  );
  await page.goto("/usage/");
  await expect(page.getByRole("heading", { name: "Usage" })).toBeVisible();
  await expect(page.getByText("Spend", { exact: true }).first()).toBeVisible();
  await page.getByRole("button", { name: "View recent requests" }).click();
  await expect(page.getByText("Receipts", { exact: true })).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await expectNoSeriousAxeViolations(page);
});

test("usage dark at 390px fits the phone and has no serious axe violation", async ({ page }) => {
  await prepareUsageViewport(page, "dark", 390);
  await page.route("**/me/api/usage/summary?**", (route) =>
    route.fulfill({ json: seededSummary }),
  );
  await page.goto("/usage/");
  await expect(page.getByRole("heading", { name: "Usage" })).toBeVisible();
  await expect(page.getByText("Spend", { exact: true }).first()).toBeVisible();
  await page.getByRole("button", { name: "View recent requests" }).click();
  await expect(page.getByText("Receipts", { exact: true })).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await expectNoSeriousAxeViolations(page);
});

test("usage empty dark at 390px fits the phone and has no serious axe violation", async ({ page }) => {
  await prepareUsageViewport(page, "dark", 390);
  await page.route("**/me/api/usage/summary?**", (route) =>
    route.fulfill({
      json: {
        requests: 0,
        media_count: 0,
        remote_spend: 0,
        remote_spend_available: false,
        retention_days: null,
        by_day: [],
        by_model: [],
        receipts: [],
      },
    }),
  );
  await page.goto("/usage/");
  await expect(
    page.getByText("No requests in this period", { exact: true }),
  ).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await expectNoSeriousAxeViolations(page);
});

test("usage error light at 1440px has no serious axe violation", async ({ page }) => {
  await prepareUsageViewport(page, "light", 1440);
  await page.route("**/me/api/usage/summary?**", (route) =>
    route.fulfill({
      status: 503,
      json: { error: { message: "Unavailable for this test" } },
    }),
  );
  await page.goto("/usage/");
  await expect(
    page.getByText("Usage could not be loaded", { exact: true }),
  ).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await expectNoSeriousAxeViolations(page);
});

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
  await page.goto("/usage/");
  await page.getByRole("button", { name: "View recent requests" }).click();
  await expect(
    page
      .getByRole("button", { name: /Receipt details for Chat request/ })
      .first(),
  ).toBeVisible();
  await page
    .getByRole("button", { name: /Receipt details for Chat request/ })
    .first()
    .click();
  await expect(
    page.getByRole("dialog", { name: "Request receipt" }),
  ).toContainText("succeeded");
});

const GGUF =
  "/media/windows/AI Models/LLMs/GGUF/Qwen3.8-27B-Uncensored-Q4_K_M.gguf";

const phoneSummary = {
  ...seededSummary,
  requests: 8264,
  by_model: [
    { model: GGUF, capability: "chat", requests: 3402, remote_spend: null, paid_requests: 0, unknown_paid_requests: 0 },
    { model: null, capability: "transcribe", requests: 2322, remote_spend: null, paid_requests: 0, unknown_paid_requests: 0 },
    { model: "Qwen3 (Q4)", capability: "chat", requests: 505, remote_spend: null, paid_requests: 0, unknown_paid_requests: 0 },
    { model: "default", capability: "chat", requests: 408, remote_spend: 0, paid_requests: 0, unknown_paid_requests: 0 },
    { model: "decide-quality", capability: "decide", requests: 377, remote_spend: null, paid_requests: 0, unknown_paid_requests: 0 },
    { model: "paid-remote-model", capability: "image", requests: 300, remote_spend: null, paid_requests: 0, unknown_paid_requests: 4 },
  ],
  by_day: [
    { date: "2026-10-01", chat: 5, image: 1, audio: 2, transcribe: 3, decide: 4, embeddings: 5 },
    { date: "2026-10-02", chat: 1 },
  ],
  receipts: [
    { ...seededSummary.receipts[0], requested_model: "default", resolved_model: GGUF },
  ],
};

test("usage fits a 390 px phone with long model names", async ({ page }) => {
  await prepareUsageViewport(page, "dark", 390);
  await page.route("**/me/api/usage/summary?**", (route) =>
    route.fulfill({ json: phoneSummary }),
  );
  await page.goto("/usage/");
  await expect(page.getByText("Top models by requests")).toBeVisible();
  await expectNoHorizontalOverflow(page);

  // The tab bar is not cut off: its right edge is inside the viewport.
  const tabs = page.getByRole("navigation", { name: "Phone navigation" });
  await expect
    .poll(async () => {
      const box = await tabs.boundingBox();
      return (box?.x ?? 0) + (box?.width ?? 0);
    })
    .toBeLessThanOrEqual(390);

  // Every row sits inside the viewport, with no clipped left edge: the header
  // and the five busiest models.
  const rows = page
    .getByRole("table")
    .filter({ hasText: "Task and model" })
    .getByRole("row");
  await expect(rows).toHaveCount(6);
  const edges = () =>
    rows.evaluateAll((elements) => {
      const rects = elements.map((element) => element.getBoundingClientRect());
      return {
        left: Math.min(...rects.map((rect) => rect.left)),
        right: Math.max(...rects.map((rect) => rect.right)),
      };
    });
  await expect.poll(async () => (await edges()).left).toBeGreaterThanOrEqual(0);
  await expect.poll(async () => (await edges()).right).toBeLessThanOrEqual(390);
});

const manySources = [
  { id: "local-network", label: "Local network · no key", kind: "local-network", requests: 60, disabled: false, created_at: null },
  { id: "app:1", label: "tim.nunamaker@gmail.com personal access with a rather long name", kind: "app", requests: 90, disabled: false, created_at: 1 },
  ...Array.from({ length: 20 }, (_, index) => ({
    id: `app:tmp${index}`,
    label: index < 3 ? "Temporary empathy Jev triage" : `copy-triage-17911237${index}`,
    kind: "app",
    requests: index < 3 ? 6 : 1,
    disabled: true,
    created_at: 1000 + index,
  })),
  ...Array.from({ length: 6 }, (_, index) => ({
    id: `app:live${index}`,
    label: `Household app ${index}`,
    kind: "app",
    requests: 20 - index,
    disabled: false,
    created_at: 100 + index,
  })),
];

// Source ranking and numbering are asserted in the vitest page spec. The
// browser checks what needs a real viewport: the open list is fully on screen,
// scrolls inside itself and never widens the page.
test("usage Source list stays on screen, scrolls inside itself and never widens the phone page", async ({ page }) => {
  await prepareUsageViewport(page, "dark", 390);
  await page.route("**/me/api/usage/summary?**", (route) =>
    route.fulfill({ json: { ...phoneSummary, sources: manySources } }),
  );
  await page.goto("/usage/");
  await expect(page.getByText("Top models by requests")).toBeVisible();
  await page.getByRole("combobox", { name: "Source" }).click();
  const list = page.getByRole("listbox");
  await expect(list).toBeVisible();
  // Radix parks an unplaced popper at translate(0, -200%) until it has
  // measured, so the list is visible before it is on screen. Wait for it to
  // sit fully inside the 390 x 844 viewport instead of reading its box once.
  await expect(list).toBeInViewport({ ratio: 1 });
  // It has a maximum height and scrolls inside itself.
  await expect
    .poll(() =>
      list.evaluate((node) => {
        const holder = node.querySelector("[data-radix-select-viewport]") as HTMLElement;
        return holder.scrollHeight - holder.clientHeight;
      }),
    )
    .toBeGreaterThan(0);
  await expectNoHorizontalOverflow(page);
  await page.keyboard.press("End");
  await expectNoHorizontalOverflow(page);
  await page.keyboard.press("Escape");
  await expectNoHorizontalOverflow(page);
});
