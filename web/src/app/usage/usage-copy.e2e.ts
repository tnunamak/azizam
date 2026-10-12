import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mkdirSync } from "node:fs";

const reviewRoot = "/home/tnunamak/.tmp/gw-product/ui-review/gw-copy-c/usage";
mkdirSync(reviewRoot, { recursive: true });

const models = Array.from({ length: 6 }, (_, index) => ({
  model: `model-${index + 1}`,
  capability: index === 1 ? "image" : "chat",
  requests: 10 - index,
  remote_spend: index === 0 ? 0.01 : null,
}));

const seededSummary = {
  requests: 12,
  media_count: 1,
  remote_spend_available: true,
  remote_spend: 0.023,
  spend_measured_requests: 8,
  spend_total_requests: 12,
  paid_requests: 8,
  unknown_paid_requests: 0,
  retention_days: 30,
  coverage_start: "2026-10-01T00:00:00Z",
  coverage_note: "Provider spend is available for matched receipts.",
  truncated: true,
  people: [{ id: "person-a", label: "Mira" }],
  sources: [{ id: "app-a", label: "Open WebUI" }],
  by_day: [{ date: "2026-10-01", chat: 2, image: 1, decide: 1 }],
  by_model: models,
  receipts: [
    {
      request_id: "req_usage_copy",
      accepted_at_ms: 1790870400000,
      requested_model: "fast",
      resolved_model: "fast",
      capability: "chat",
      status: "success",
      latency_ms: 104,
      source: { label: "Open WebUI" },
    },
  ],
};

async function setDisplay(page: Page, theme: "light" | "dark", width: number) {
  await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
  await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
  await page.addInitScript(
    (value) => localStorage.setItem("theme", value),
    theme,
  );
}

async function inspect(page: Page, name: string) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  const violations = (
    await new AxeBuilder({ page }).analyze()
  ).violations.filter((violation) =>
    ["serious", "critical"].includes(violation.impact ?? ""),
  );
  expect(violations).toEqual([]);
  await page.screenshot({ path: `${reviewRoot}/${name}.png`, fullPage: true });
}

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390] as const) {
    test(`Usage seeded disclosure ${theme} ${width}`, async ({ page }) => {
      await setDisplay(page, theme, width);
      await page.route("**/me/api/usage/summary?**", (route) =>
        route.fulfill({ json: seededSummary }),
      );

      await page.goto("/usage/");
      const decisions = page.locator('span[style*="--usage-decisions"]');
      await expect(decisions).toBeVisible();
      const colors = await decisions.evaluate((element) => ({
        decisions: getComputedStyle(element).backgroundColor,
        images: getComputedStyle(
          document.querySelector('span[style*="--capability-image"]')!,
        ).backgroundColor,
      }));
      expect(colors.decisions).toBe(
        theme === "light" ? "rgb(180, 83, 9)" : "rgb(251, 146, 60)",
      );
      expect(colors.decisions).not.toBe(colors.images);

      await expect(
        page.getByText(
          /Receipt coverage · 30 days · since Oct 1, 2026 · truncated/,
        ),
      ).toBeVisible();
      await page.getByText(/Receipt coverage · 30 days/).click();
      await expect(
        page.getByText(/Receipts are kept for 30 days/),
      ).toBeVisible();
      await expect(
        page.getByText(
          "This view is limited to the most recent stored receipts.",
        ),
      ).toBeVisible();
      await expect(page.getByText("model-1")).toBeVisible();
      await expect(page.getByText("model-6")).toHaveCount(0);
      await page.getByRole("button", { name: "View all 6 models" }).click();
      await expect(page.getByText("model-6")).toBeVisible();
      await expect(page.getByText("req_usage_copy")).toHaveCount(0);
      await page.getByRole("button", { name: "View recent requests" }).click();
      await expect(page.locator("#usage-receipts")).toContainText("fast");
      await inspect(page, `seeded-${theme}-${width}`);
    });

    test(`Usage empty ${theme} ${width}`, async ({ page }) => {
      await setDisplay(page, theme, width);
      await page.route("**/me/api/usage/summary?**", (route) =>
        route.fulfill({
          json: {
            ...seededSummary,
            requests: 0,
            media_count: 0,
            remote_spend_available: false,
            by_day: [],
            by_model: [],
            receipts: [],
            retention_days: 90,
            people: [],
            sources: [],
          },
        }),
      );
      await page.goto("/usage/");
      await expect(page.getByText("No requests in this period")).toBeVisible();
      await expect(page.getByText(/90-day receipts appear here/)).toBeVisible();
      await inspect(page, `empty-${theme}-${width}`);
    });

    test(`Usage error ${theme} ${width}`, async ({ page }) => {
      await setDisplay(page, theme, width);
      await page.route("**/me/api/usage/summary?**", (route) =>
        route.fulfill({ status: 503, json: { error: "Unavailable" } }),
      );
      await page.goto("/usage/");
      await expect(page.getByText(/Usage could not be loaded/)).toBeVisible();
      await inspect(page, `error-${theme}-${width}`);
    });

    test(`Usage loading ${theme} ${width}`, async ({ page }) => {
      await setDisplay(page, theme, width);
      await page.route(
        "**/me/api/usage/summary?**",
        () => new Promise(() => {}),
      );
      await page.goto("/usage/");
      await expect(page.getByRole("status")).toBeVisible();
      await page.screenshot({
        path: `${reviewRoot}/loading-${theme}-${width}.png`,
        fullPage: true,
      });
    });
  }
}

test("Usage preserves person and source filters in API requests", async ({
  page,
}) => {
  const urls: string[] = [];
  await page.route("**/me/api/usage/summary?**", (route) => {
    urls.push(route.request().url());
    return route.fulfill({ json: seededSummary });
  });

  await page.goto("/usage/");
  await page.getByRole("combobox", { name: "Person" }).click();
  await page.getByRole("option", { name: "Mira" }).click();
  await page.getByRole("combobox", { name: "Source" }).click();
  await page.getByRole("option", { name: "Open WebUI" }).click();

  await expect
    .poll(() => urls.some((url) => url.includes("person=person-a")))
    .toBe(true);
  expect(urls.some((url) => url.includes("source=app-a"))).toBe(true);
});

for (const [status, expected, summary, extra] of [
  [
    "success",
    "Completed",
    "The gateway completed this request.",
    { metadata: { status_code: 200 } },
  ],
  [
    "rejected",
    "Rejected",
    "The gateway rejected this request.",
    { metadata: { status_code: 403 } },
  ],
  [
    "backend_failed",
    "Failed",
    "We couldn't tell where this stopped. Try again.",
    { metadata: { status_code: 502 } },
  ],
  [
    "accepted",
    "Accepted",
    "The gateway accepted this request for processing.",
    {},
  ],
  [
    "running",
    "In progress",
    "The request was still running when this receipt was recorded.",
    {},
  ],
  [
    "stale_incomplete",
    "Outcome not recorded",
    "We couldn't tell where this stopped. Check for a newer receipt, then try again.",
    {},
  ],
] as const) {
  test(`Usage receipt outcome ${status}`, async ({ page }) => {
    await page.route("**/me/api/usage/summary?**", (route) =>
      route.fulfill({
        json: {
          ...seededSummary,
          receipts: [
            {
              request_id: `req_${status}`,
              accepted_at_ms: 1790870400000,
              requested_model: "fast",
              resolved_model:
                status === "stale_incomplete" ? null : "demo-chat",
              fallback_model: "safe-chat",
              capability: "chat",
              status,
              latency_ms: status === "running" ? null : 104,
              ...extra,
            },
          ],
        },
      }),
    );

    await page.goto("/usage/");
    await page.getByRole("button", { name: "View recent requests" }).click();
    await page.getByRole("button", { name: /Receipt details for/ }).click();
    const dialog = page.getByRole("dialog", { name: "Request receipt" });
    await expect(dialog).toContainText(expected);
    if (status !== "success") await expect(dialog).toContainText(summary);
    if (status === "success") {
      await expect(dialog).not.toContainText(
        "The request could not be completed.",
      );
    }
    if (status === "rejected") {
      await expect(dialog).toContainText(
        "Your account does not have permission for this action.",
      );
    }
    await dialog.getByText("Technical details", { exact: true }).click();
    await expect(dialog).toContainText(`req_${status}`);
    await expect(dialog).toContainText("fast");
    await expect(dialog).toContainText("safe-chat");
    await expect(
      dialog.getByRole("button", { name: "Copy request ID" }),
    ).toBeVisible();
  });
}

test("Usage omits unavailable retention details", async ({
  page,
}) => {
  const withoutRetention: Record<string, unknown> = { ...seededSummary };
  delete withoutRetention.retention_days;
  await page.route("**/me/api/usage/summary?**", (route) =>
    route.fulfill({ json: withoutRetention }),
  );
  await page.goto("/usage/");
  await expect(page.getByText("Receipt coverage", { exact: true })).toBeVisible();
  await expect(page.getByText(/14 days/)).toHaveCount(0);
  await page.getByText("Receipt coverage", { exact: true }).click();
  await expect(page.getByText(/retention not reported/i)).toHaveCount(0);
});

test("Usage labels local and unknown paid spend honestly", async ({ page }) => {
  await page.route("**/me/api/usage/summary?**", async (route) => {
    const url = route.request().url();
    const unknownPaid = url.includes("source=app-a");
    await route.fulfill({
      json: {
        ...seededSummary,
        remote_spend_available: false,
        remote_spend: 0,
        spend_measured_requests: 0,
        spend_total_requests: 12,
        paid_requests: 0,
        unknown_paid_requests: unknownPaid ? 2 : 0,
        by_model: [
          {
            model: "",
            capability: "transcribe",
            requests: 6,
            remote_spend: null,
          },
          {
            model: "default",
            capability: "chat",
            requests: 4,
            remote_spend: null,
          },
        ],
      },
    });
  });

  await page.goto("/usage/");
  await expect(page.getByText("Spend", { exact: true })).toBeVisible();
  await expect(page.getByText("Cost not recorded").first()).toBeVisible();
  await expect(page.getByText("Transcribe a recording", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("/v1/audio/transcriptions")).toHaveCount(0);
  await expect(page.getByText("Unknown model")).toHaveCount(0);
  await expect(page.getByRole("table").last()).not.toContainText("$0.00");

  await page.getByRole("combobox", { name: "Source" }).click();
  await page.getByRole("option", { name: "Open WebUI" }).click();
  await expect(page.getByText("Spend", { exact: true })).toBeVisible();
  await expect(page.getByText("Cost not recorded", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("No paid requests")).toHaveCount(0);
  await expect(
    page.getByText(/2 paid requests have unknown cost/),
  ).toBeVisible();
});
