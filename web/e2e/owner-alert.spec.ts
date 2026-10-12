import { test, expect } from "@playwright/test";

for (const mode of [
  "down",
  "disabled",
  "unavailable",
  "hardware",
  "unused",
  "unknown-usage",
] as const) {
  test(`global owner alert: ${mode}`, async ({ page }) => {
    await page.route("**/admin/api/status", (route) =>
      route.fulfill({
        json: {
          status: {
            current_health: {
              state: "warn",
              tone: "warn",
              label: "Needs attention",
              detail: "",
              observation_available: mode !== "unavailable",
              signals: [
                { source: "hardware", tone: "bad", reasons: ["Disk is full"] },
              ],
              services:
                mode === "hardware"
                  ? []
                  : [
                      {
                        service_id: "image",
                        name: "Image engine",
                        configured: true,
                        enabled: mode !== "disabled",
                        in_use:
                          mode === "unknown-usage" ? null : mode !== "unused",
                        role: "required",
                        routable: false,
                        observation:
                          mode === "unavailable" ? "unavailable" : "down",
                        tone: "bad",
                        last_error: "Connection refused",
                      },
                    ],
            },
          },
        },
      }),
    );
    await page.goto("/models/");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    const alert = page.getByText(
      "Image engine can’t take requests right now.",
      { exact: false },
    );
    // Hardware signals never become an owner-wide banner. Checked on the
    // page with the banner; Status itself rightly lists the disk signal.
    await expect(page.getByText("Disk is full", { exact: true })).toHaveCount(
      0,
    );
    if (mode === "down") {
      await expect(alert).toBeVisible();
      await page
        .getByRole("link", { name: "See Status", exact: true })
        .click();
      await expect(page).toHaveURL(/\/status\/?#engines$/);
      await expect(
        page.getByRole("heading", {
          name: "Engines and providers",
          exact: true,
          level: 2,
        }),
      ).toBeFocused();
      await expect(alert).toHaveCount(0);
    } else await expect(alert).toHaveCount(0);
  });
}

test("provider alerts use explicit enabled and reachability data", async ({
  page,
}) => {
  await page.route("**/admin/api/status", (route) =>
    route.fulfill({ json: { status: {} } }),
  );
  await page.route("**/admin/api/providers", (route) =>
    route.fulfill({
      json: {
        providers: [
          {
            name: "Remote provider",
            enabled: true,
            reachable: false,
            key_set: true,
          },
        ],
      },
    }),
  );
  await page.goto("/models/");
  await expect(
    page.getByText(
      "Remote provider can’t take requests right now.",
      { exact: false },
    ),
  ).toBeVisible();
  const banner = page
    .getByRole("status")
    .filter({
      hasText:
        "Remote provider can’t take requests right now.",
    });
  const action = banner.getByRole("link", { name: "See Status", exact: true });
  await action.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/status\/?#providers$/);
  // D1: engines and providers share one section on Status.
  await expect(
    page.getByRole("heading", {
      name: "Engines and providers",
      exact: true,
      level: 2,
    }),
  ).toBeFocused();
  await expect(banner).toHaveCount(0);
});
