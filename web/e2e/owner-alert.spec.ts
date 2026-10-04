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
    await page.goto("/ui/models/");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    const alert = page.getByText(
      "Image engine is unavailable. Check its connection on Status.",
      { exact: true },
    );
    if (mode === "down") {
      await expect(alert).toBeVisible();
      await page
        .getByRole("link", { name: "View status", exact: true })
        .click();
      await expect(page).toHaveURL(/\/ui\/status\/?$/);
    } else await expect(alert).toHaveCount(0);
    await expect(page.getByText("Disk is full", { exact: true })).toHaveCount(
      0,
    );
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
  await page.goto("/ui/models/");
  await expect(
    page.getByText(
      "Remote provider is unreachable. Check its connection on Status.",
      { exact: true },
    ),
  ).toBeVisible();
});
