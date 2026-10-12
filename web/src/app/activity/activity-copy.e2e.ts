import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mkdirSync } from "node:fs";

const reviewRoot =
  process.env.ACTIVITY_REVIEW_DIR ?? "/home/tnunamak/.tmp/wp-7-18-shots/activity";
mkdirSync(reviewRoot, { recursive: true });

const auditRecord = {
  id: "aud-1",
  actor_id: "owner",
  actor_label: "Owner",
  actor_kind: "user",
  action: "app.create",
  target_kind: "app",
  target_id: "app-a",
  created_at: 1790870400,
  metadata: { name: "Open WebUI" },
};
const accessAuditRecord = {
  ...auditRecord,
  id: "aud-access-view",
  actor_label: "owner@example.test",
  action: "usage.summary.everyone",
};

function requestRecord(status: string, extra: Record<string, unknown> = {}) {
  return {
    receipt: {
      request_id: `req_${status || "missing"}`,
      intent: "chat.completions",
      status,
      requested_model: "fast",
      resolved_model: status === "stale_incomplete" ? undefined : "fast",
      fallback_model: "safe-chat",
      latency_ms: 104,
      ...extra,
    },
    person_label: "Owner",
    app_label: "Open WebUI",
    created_at: 1790870400,
    source: { label: "Open WebUI", kind: "app" },
  };
}

test("owner access views stay out of Activity until revealed", async ({
  page,
}) => {
  const auditUrls: string[] = [];
  await page.route("**/admin/api/audit?*", (route) => {
    const url = route.request().url();
    auditUrls.push(url);
    const includeAccess = !url.includes("exclude_access_audit=true");
    return route.fulfill({
      json: {
        records: includeAccess ? [accessAuditRecord, auditRecord] : [auditRecord],
        next_cursor: null,
      },
    });
  });
  await page.goto("/activity/");
  await page.getByRole("tab", { name: "Changes" }).click();
  await expect(page.getByText("Viewed everyone’s usage")).toHaveCount(0);
  await expect(page.getByText("owner@example.test")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Show access history" })).toBeVisible();
  await page.getByRole("button", { name: "Show access history" }).click();
  await expect(page.getByText("Viewed everyone’s usage")).toBeVisible();
  await expect(page.getByText("owner@example.test")).toHaveCount(0);
  expect(auditUrls.some((url) => url.includes("exclude_access_audit=true"))).toBe(true);
  expect(auditUrls.some((url) => !url.includes("exclude_access_audit=true"))).toBe(true);
});

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
    test(`Activity seeded ${theme} ${width}`, async ({ page }) => {
      await setDisplay(page, theme, width);
      await page.route("**/admin/api/audit?*", (route) =>
        route.fulfill({ json: { records: [auditRecord], next_cursor: null } }),
      );
      await page.route("**/admin/api/activity/requests?*", (route) =>
        route.fulfill({
          json: {
            records: [
              requestRecord("succeeded", { metadata: { status_code: 200 } }),
              {
                ...requestRecord("succeeded", {
                  request_id: "req_nested",
                  intent: "audio.sfx",
                  parent_request_id: "req_succeeded",
                }),
                created_at: 1790860000,
              },
            ],
            next_cursor: null,
            people: [{ id: "owner", label: "Owner" }],
            sources: [{ id: "app-a", label: "Open WebUI" }],
          },
        }),
      );

      await page.goto("/activity/");
      // Activity opens on Requests; Changes is one tap away.
      await expect(page.getByRole("tab", { name: "Requests" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      await page.getByRole("tab", { name: "Changes" }).click();
      await expect(page.getByText("Created an app")).toBeVisible();
      await page.getByRole("button", { name: /Details: Created an app/ }).click();
      const change = page.getByRole("dialog");
      await expect(change).toContainText("Changed by");
      await expect(change).toContainText("Open WebUI");
      // Who and when are fields; the title area does not repeat them.
      await expect(change.getByText("Owner", { exact: true })).toHaveCount(1);
      await inspect(page, `seeded-change-${theme}-${width}`);
      await page.keyboard.press("Escape");
      await page.getByRole("tab", { name: "Requests" }).click();
      await expect(page.getByText(/^Sound effect/)).toBeVisible();
      await expect(page.getByText("Part of another request")).toBeVisible();
      await page.screenshot({
        path: `${reviewRoot}/requests-list-${theme}-${width}.png`,
      });
      await page.getByRole("button", { name: /Request details:/ }).first().click();
      const dialog = page.getByRole("dialog");
      await expect(dialog).toContainText("Completed");
      await expect(dialog.getByText("Declared fallback")).toBeHidden();
      await inspect(page, `seeded-${theme}-${width}`);
    });

    test(`Activity empty ${theme} ${width}`, async ({ page }) => {
      await setDisplay(page, theme, width);
      await page.route("**/admin/api/audit?*", (route) =>
        route.fulfill({ json: { records: [], next_cursor: null } }),
      );
      await page.route("**/admin/api/activity/requests?*", (route) =>
        route.fulfill({ json: { records: [], next_cursor: null, people: [] } }),
      );
      await page.goto("/activity/");
      await expect(page.getByText("No request receipts yet")).toBeVisible();
      await page.getByRole("tab", { name: "Changes" }).click();
      await expect(page.getByText("No changes yet")).toBeVisible();
      await inspect(page, `empty-${theme}-${width}`);
    });

    test(`Activity error ${theme} ${width}`, async ({ page }) => {
      await setDisplay(page, theme, width);
      await page.route("**/admin/api/audit?*", (route) =>
        route.fulfill({ status: 503, json: { error: "Unavailable" } }),
      );
      await page.route("**/admin/api/activity/requests?*", (route) =>
        route.fulfill({ status: 503, json: { error: "Unavailable" } }),
      );
      await page.goto("/activity/");
      await expect(
        page.getByText(/The gateway.s request list could not be loaded/),
      ).toBeVisible();
      await page.getByRole("tab", { name: "Changes" }).click();
      await expect(
        page.getByText(/The gateway.s changes list could not be loaded/),
      ).toBeVisible();
      await inspect(page, `error-${theme}-${width}`);
    });

    test(`Activity loading ${theme} ${width}`, async ({ page }) => {
      await setDisplay(page, theme, width);
      await page.route("**/admin/api/audit?*", () => new Promise(() => {}));
      await page.goto("/activity/");
      await page.getByRole("tab", { name: "Changes" }).click();
      await expect(page.getByLabel("Loading changes")).toBeVisible();
      await page.screenshot({
        path: `${reviewRoot}/loading-${theme}-${width}.png`,
        fullPage: true,
      });
    });
  }
}

for (const [status, expected, summary] of [
  ["succeeded", "Completed", null],
  ["rejected", "Rejected", "The gateway rejected this request."],
  [
    "backend_failed",
    "Failed",
    "We couldn't tell where this stopped. Try again.",
  ],
  [
    "stale_incomplete",
    "Outcome not recorded",
    "We couldn't tell where this stopped. Check for a newer receipt, then try again.",
  ],
] as const) {
  test(`Activity request outcome ${status}`, async ({ page }) => {
    await page.route("**/admin/api/audit?*", (route) =>
      route.fulfill({ json: { records: [], next_cursor: null } }),
    );
    await page.route("**/admin/api/activity/requests?*", (route) =>
      route.fulfill({
        json: {
          records: [requestRecord(status)],
          next_cursor: null,
          people: [{ id: "owner", label: "Owner" }],
          sources: [{ id: "app-a", label: "Open WebUI" }],
        },
      }),
    );

    await page.goto("/activity/");
    await page.getByRole("button", { name: /Request details:/ }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText(expected);
    if (summary) await expect(dialog).toContainText(summary);
    // Routing stays out of the plain view: one model, no fallback names.
    await expect(dialog.getByText("Declared fallback")).toBeHidden();
    await expect(dialog.getByText("safe-chat")).toBeHidden();
  });
}

test("Activity receipt shows both models only when a different one served, and keeps routing in Technical details", async ({
  page,
}) => {
  await page.route("**/admin/api/audit?*", (route) =>
    route.fulfill({ json: { records: [], next_cursor: null } }),
  );
  await page.route("**/admin/api/activity/requests?*", (route) =>
    route.fulfill({
      json: {
        records: [
          requestRecord("succeeded", { resolved_model: "backup-chat" }),
          {
            ...requestRecord("succeeded", {
              request_id: "req_child",
              parent_request_id: "req_parent",
            }),
            created_at: 1790870000,
          },
        ],
        next_cursor: null,
        people: [{ id: "owner", label: "Owner" }],
        sources: [{ id: "app-a", label: "Open WebUI" }],
      },
    }),
  );
  await page.goto("/activity/");
  await expect(page.getByText("Part of another request")).toBeVisible();
  await expect(page.getByText("Internal execution")).toHaveCount(0);
  await page.getByRole("button", { name: /Request details:/ }).first().click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("A different model was used.");
  await expect(dialog).toContainText("Requested model");
  await expect(dialog).toContainText("Served model");
  const technical = dialog.locator("details");
  await expect(dialog.getByText("safe-chat")).toBeHidden();
  await technical.locator("summary").click();
  await expect(dialog.getByText(/safe-chat/)).toBeVisible();
});

test("Activity request filters preserve person and source", async ({
  page,
}) => {
  const urls: string[] = [];
  await page.route("**/admin/api/audit?*", (route) =>
    route.fulfill({ json: { records: [], next_cursor: null } }),
  );
  await page.route("**/admin/api/activity/requests?*", (route) => {
    urls.push(route.request().url());
    return route.fulfill({
      json: {
        records: [requestRecord("succeeded")],
        next_cursor: null,
        people: [{ id: "owner", label: "Owner" }],
        sources: [{ id: "app-a", label: "Open WebUI" }],
      },
    });
  });
  await page.goto("/activity/");
  await page.getByRole("combobox", { name: "Person" }).click();
  await page.getByRole("option", { name: "Owner" }).click();
  await page.getByRole("combobox", { name: "Source" }).click();
  await page.getByRole("option", { name: "Open WebUI" }).click();
  await expect
    .poll(() => urls.some((url) => url.includes("person_id=owner")))
    .toBe(true);
  expect(urls.some((url) => url.includes("source=app-a"))).toBe(true);
});
