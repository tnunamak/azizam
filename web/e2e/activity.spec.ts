import { test, expect } from "@playwright/test";
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

  await page.goto("/activity/");
  await page.getByRole("tab", { name: "Changes", exact: true }).click();
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

  await page.getByRole("button", { name: "Show access history" }).click();
  await expect(
    page.getByRole("button", { name: /Viewed everyone’s usage/ }),
  ).toBeVisible();
  expect(requestedFilters).toContain("false");
  await page.getByRole("button", { name: "Hide access history" }).click();
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

  await page.goto("/activity/");
  await page.getByRole("tab", { name: "Changes", exact: true }).click();
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

test("system change labels stay generic until event details are opened", async ({
  page,
}) => {
  const rawLabel =
    "registry-reconstruct-20261001 (restore Sep 30 pre-damage + phase5b/6 changes)";
  await page.route("**/admin/api/audit**", (route) =>
    route.fulfill({
      json: {
        records: [
          {
            id: "system-reconstruction",
            action: "operator.service_registry.backfill",
            actor_kind: "system",
            actor_id: "startup-task",
            actor_label: rawLabel,
            target_kind: "operator_setting",
            target_id: "service_registry",
            metadata: { updated_by: rawLabel },
            created_at: 1_760_000_000,
          },
        ],
        next_cursor: null,
        automated_count: 0,
      },
    }),
  );

  await page.goto("/activity/");
  await page.getByRole("tab", { name: "Changes", exact: true }).click();
  const change = page.getByRole("button", {
    name: /Updated engine configuration by System/,
  });
  await expect(change).toContainText("System · Gateway settings");
  await expect(page.getByText(rawLabel)).toHaveCount(0);

  await change.click();
  await page.getByText("Technical details", { exact: true }).click();
  await expect(page.getByLabel("Technical change details")).toContainText(rawLabel);
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
        await page.goto("/activity/");
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
          // The outcome sits in the sheet body; the description names the person.
          await expect(page.getByRole("dialog")).toContainText("Completed");
          await expect(
            page.getByRole("dialog").locator('[data-slot="sheet-description"]'),
          ).not.toContainText("Completed");
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
                : "Couldn't load requests",
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
        await page.goto("/activity/");
        await page.getByRole("tab", { name: "Changes", exact: true }).click();
        if (state === "seeded")
          await expect(
            page.getByRole("button", { name: /Details:/ }).first(),
          ).toBeVisible();
        else
          await expect(
            page.getByText(
              state === "empty"
                ? "No changes yet"
                : "Couldn't load changes",
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

for (const status of ["disconnected", "timed_out", "incomplete"]) {
  test(`receipt keeps routing details private and offers a next action: ${status}`, async ({ page }) => {
    await page.route("**/admin/api/activity/requests?*", (route) => route.fulfill({ json: {
      records: [{ person_label: "Mira", app_label: "Notes", created_at: 1790870400000,
        receipt: { request_id: "private-request-id", intent: "chat", status,
          requested_model: "demo-chat", resolved_model: "demo-chat", parent_request_id: "parent",
          stage: "private-stage", error_code: "private-code", warnings: ["private-warning"] } }],
      people: [], next_cursor: null,
    } }));
    await page.goto("/activity/");
    await page.getByRole("tab", { name: "Requests", exact: true }).click();
    await page.getByRole("button", { name: /Request details:/ }).click();
    const sheet = page.getByRole("dialog");
    await expect(sheet.getByText("Model", { exact: true })).toBeVisible();
    await expect(sheet.getByText("Requested model", { exact: true })).toHaveCount(0);
    await expect(sheet.getByText("Served model", { exact: true })).toHaveCount(0);
    await expect(sheet.getByText("Mira", { exact: true })).toHaveCount(1);
    await expect(sheet.getByText("Notes", { exact: true })).toHaveCount(1);
    await expect(sheet.getByText(status === "incomplete" ? /Check for a newer receipt, then try again/ : /before we recorded a result\. Try again\./)).toBeVisible();
    const technical = sheet.locator("details");
    await expect(technical).not.toHaveAttribute("open");
    await expect(technical.locator("pre")).not.toBeVisible();
    await technical.locator("summary").click();
    await expect(technical).toContainText("private-code");
    await expect(technical.getByText("Part of another request", { exact: true })).toBeVisible();
  });
}
