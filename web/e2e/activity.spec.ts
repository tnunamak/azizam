import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("requests refresh on focus and keep metadata on one line", async ({
  page,
}) => {
  let loads = 0;
  await page.route("**/admin/api/activity/requests?*", (route) => {
    loads += 1;
    return route.continue();
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/ui/activity/");
  await page.getByRole("tab", { name: "Requests", exact: true }).click();
  const row = page.getByRole("button", { name: /Request details:/ }).first();
  await expect(row).toBeVisible();
  await expect(
    page.getByRole("combobox", { name: "Person", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Refresh", exact: true }),
  ).toHaveCount(0);
  const meta = row.locator("p").nth(1);
  expect(
    await meta.evaluate((element) => element.clientHeight),
  ).toBeLessThanOrEqual(18);
  const before = loads;
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect.poll(() => loads).toBeGreaterThan(before);
  await expect(row).toBeVisible();
});

test("owner access audit rows stay hidden until the filter is enabled", async ({
  page,
}) => {
  const requestedFilters: string[] = [];
  const rows = [
    {
      id: "setting-change",
      action: "operator.instance.update",
      actor_id: "owner-user",
      actor_label: "owner@example.test",
      target_kind: "operator_setting",
      target_id: "instance",
      metadata: {},
      created_at: 1_760_000_000,
    },
    {
      id: "usage-view",
      action: "usage.summary.everyone",
      actor_id: "owner-user",
      actor_label: "owner@example.test",
      target_kind: "usage",
      target_id: "everyone",
      metadata: {},
      created_at: 1_760_000_001,
    },
  ];
  await page.route("**/admin/api/audit**", (route) => {
    const url = new URL(route.request().url());
    const excluded = url.searchParams.get("exclude_access_audit") === "true";
    requestedFilters.push(url.searchParams.get("exclude_access_audit") ?? "");
    return route.fulfill({
      json: {
        records: excluded ? rows.slice(0, 1) : rows,
        next_cursor: null,
      },
    });
  });

  await page.goto("/ui/activity/");
  await expect(
    page.getByRole("button", { name: /Updated instance settings/ }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: /Viewed everyone’s usage/ }),
  ).toHaveCount(0);
  expect(requestedFilters).toContain("true");
  await expect(
    page.getByText("owner@example.test", { exact: true }),
  ).toHaveCount(0);

  await page.getByRole("button", { name: "Show access audit" }).click();
  await expect(
    page.getByRole("button", { name: /Viewed everyone’s usage/ }),
  ).toBeVisible();
  expect(requestedFilters).toContain("false");
  await page.getByRole("button", { name: "Hide access audit" }).click();
  await expect(
    page.getByRole("button", { name: /Viewed everyone’s usage/ }),
  ).toHaveCount(0);
});

test("automated changes are grouped by default and use plain wording when shown", async ({
  page,
}) => {
  const rows = [
    {
      id: "human-backfill",
      action: "operator.service_registry.backfill",
      actor_kind: "user",
      actor_id: "owner-user",
      actor_label: "Owner",
      target_kind: "operator_setting",
      target_id: "service_registry",
      metadata: {},
      created_at: 1_760_000_000,
    },
    {
      id: "automated-key",
      action: "credential.create",
      actor_kind: "service_account",
      actor_id: "svc_deploy",
      actor_label: "safe-restart",
      target_kind: "credential",
      target_id: "cred_1",
      metadata: {},
      created_at: 1_760_000_002,
    },
    {
      id: "automated-app",
      action: "app.create",
      actor_kind: "service_account",
      actor_id: "svc_deploy",
      actor_label: "safe-restart",
      target_kind: "app",
      target_id: "app_1",
      metadata: {},
      created_at: 1_760_000_001,
    },
  ];
  await page.route("**/admin/api/audit?*", (route) => {
    const url = new URL(route.request().url());
    const showAutomated = url.searchParams.get("show_automated") === "true";
    return route.fulfill({
      json: {
        records: showAutomated
          ? rows
          : rows.filter((row) => row.actor_kind !== "service_account"),
        next_cursor: null,
        automated_count: 2,
      },
    });
  });

  await page.goto("/ui/activity/");
  await expect(page.getByText("2 automated changes hidden")).toBeVisible();
  await expect(
    page.getByRole("button", { name: /Updated engine configuration/ }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: /Created a key/ })).toHaveCount(0);

  await page.getByRole("button", { name: "Show automated changes" }).click();
  await expect(page.getByText("Showing 2 automated changes")).toBeVisible();
  await expect(
    page.getByRole("button", { name: /Created a key by Automation/ }),
  ).toBeVisible();
  await expect(page.getByText(/registry|safe-restart/i)).toHaveCount(0);
});

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390]) {
    for (const state of ["seeded", "empty", "error"] as const) {
      test(`activity requests ${state}: ${theme} ${width}`, async ({
        page,
      }) => {
        await page.setViewportSize({
          width,
          height: width === 390 ? 844 : 1000,
        });
        await page.emulateMedia({
          colorScheme: theme,
          reducedMotion: "reduce",
        });
        await page.addInitScript(
          (value) => localStorage.setItem("theme", value),
          theme,
        );
        if (state !== "seeded") {
          await page.route("**/admin/api/activity/requests?*", (route) =>
            route.fulfill({
              status: state === "error" ? 503 : 200,
              contentType: "application/json",
              body: JSON.stringify(
                state === "empty"
                  ? { records: [], next_cursor: null, people: [] }
                  : { error: { message: "Unavailable" } },
              ),
            }),
          );
        }
        await page.goto("/ui/activity/");
        await page.getByRole("tab", { name: "Requests", exact: true }).click();
        if (state === "seeded") {
          await expect(
            page.getByRole("button", { name: /Request details:/ }).first(),
          ).toBeVisible();
          await page
            .getByRole("button", { name: /Request details:/ })
            .first()
            .click();
          await expect(page.getByRole("dialog")).toBeVisible();
          await expect(
            page.getByRole("dialog").locator('[data-slot="sheet-description"]'),
          ).toContainText("Completed");
          await page
            .getByRole("dialog")
            .getByText("Technical details", { exact: true })
            .click();
          await expect(page.getByRole("dialog").locator("pre")).toBeVisible();
          expect(
            await page
              .getByRole("dialog")
              .evaluate(
                (element) => element.scrollWidth <= element.clientWidth,
              ),
          ).toBe(true);
          await page.screenshot({
            path: `test-results/screens/activity-request-detail-${theme}-${width}.png`,
            fullPage: true,
          });
          const detailAxe = await new AxeBuilder({ page }).analyze();
          expect(
            detailAxe.violations.filter((v) =>
              ["serious", "critical"].includes(v.impact ?? ""),
            ),
          ).toEqual([]);
          await page.keyboard.press("Escape");
          await page
            .getByRole("combobox", { name: "Person", exact: true })
            .click();
          const options = page.getByRole("option");
          await expect(options.nth(1)).toBeVisible();
          await options.nth(1).click();
          await expect(
            page.getByRole("button", { name: /Request details:/ }).first(),
          ).toBeVisible();
          await page.getByRole("button", { name: "Clear filters" }).click();
        } else {
          await expect(
            page.getByText(
              state === "empty"
                ? "No request receipts yet"
                : "Requests could not be loaded. Try again.",
              { exact: true },
            ),
          ).toBeVisible();
        }
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
        ).toBe(true);
        await page.screenshot({
          path: `test-results/screens/activity-requests-${state}-${theme}-${width}.png`,
          fullPage: true,
        });
        const axe = await new AxeBuilder({ page }).analyze();
        expect(
          axe.violations.filter((v) =>
            ["serious", "critical"].includes(v.impact ?? ""),
          ),
        ).toEqual([]);
      });
    }
  }
}

test("request errors retry and audit details use readable timestamps", async ({
  page,
}) => {
  let fail = true;
  await page.route("**/admin/api/activity/requests?*", (route) =>
    fail ? route.fulfill({ status: 503, body: "{}" }) : route.continue(),
  );
  await page.goto("/ui/activity/");
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
    page.getByText("Requests could not be loaded. Try again.", { exact: true }),
  ).toBeVisible();
  fail = false;
  await page.getByRole("button", { name: /Retry|Try again/ }).click();
  await expect(
    page.getByRole("button", { name: /Request details:/ }).first(),
  ).toBeVisible();
});

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390]) {
    for (const state of ["seeded", "empty", "error"] as const) {
      test(`activity audit ${state}: ${theme} ${width}`, async ({ page }) => {
        await page.setViewportSize({
          width,
          height: width === 390 ? 844 : 1000,
        });
        await page.emulateMedia({
          colorScheme: theme,
          reducedMotion: "reduce",
        });
        await page.addInitScript(
          (value) => localStorage.setItem("theme", value),
          theme,
        );
        if (state !== "seeded") {
          await page.route("**/admin/api/audit?*", (route) =>
            route.fulfill({
              status: state === "error" ? 503 : 200,
              contentType: "application/json",
              body: JSON.stringify(
                state === "empty"
                  ? { records: [], next_cursor: null }
                  : { error: { code: "unavailable", message: "Unavailable" } },
              ),
            }),
          );
        }
        await page.goto("/ui/activity/");
        if (state === "seeded")
          await expect(
            page.getByRole("button", { name: /Details:/ }).first(),
          ).toBeVisible();
        else
          await expect(
            page.getByText(
              state === "empty"
                ? "No changes yet"
                : "Changes could not be loaded. Try again.",
              { exact: true },
            ),
          ).toBeVisible();
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
        ).toBe(true);
        await page.screenshot({
          path: `test-results/screens/activity-audit-${state}-${theme}-${width}.png`,
          fullPage: true,
        });
        const axe = await new AxeBuilder({ page }).analyze();
        expect(
          axe.violations.filter((v) =>
            ["serious", "critical"].includes(v.impact ?? ""),
          ),
        ).toEqual([]);
      });
    }
  }
}
