import { test, expect } from "./fixtures/isolation";
import { fulfilJson } from "./fixtures/intercept";

// The banner truth table (disabled, unavailable, unobserved, hardware, unused,
// unknown-usage) is asserted in src/components/shell/app-shell.health.spec.tsx.
// The browser keeps the real journey: banner -> See Status -> focused section.
test("global owner alert: a down engine links to its Status section", async ({
  page,
}) => {
  await page.route("**/admin/api/status", (route) =>
    fulfilJson(route, {
      status: {
        current_health: {
          state: "warn",
          tone: "warn",
          label: "Needs attention",
          detail: "",
          observation_available: true,
          signals: [
            { source: "hardware", tone: "bad", reasons: ["Disk is full"] },
          ],
          services: [
            {
              service_id: "image",
              name: "Image engine",
              configured: true,
              enabled: true,
              in_use: true,
              role: "required",
              routable: false,
              observation: "down",
              tone: "bad",
              last_error: "Connection refused",
            },
          ],
        },
      },
    }),
  );
  await page.goto("/models/");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  const alert = page.getByText("Image engine can’t take requests right now.", {
    exact: false,
  });
  await expect(page.getByText("Disk is full", { exact: true })).toHaveCount(0);
  await expect(alert).toBeVisible();
  await page.getByRole("link", { name: "See Status", exact: true }).click();
  await expect(page).toHaveURL(/\/status\/?#engines$/);
  await expect(
    page.getByRole("heading", {
      name: "Engines and providers",
      exact: true,
      level: 2,
    }),
  ).toBeFocused();
  await expect(alert).toHaveCount(0);
});

test("provider alerts use explicit enabled and reachability data", async ({
  page,
}) => {
  await page.route("**/admin/api/status", (route) =>
    fulfilJson(route, { status: {} }),
  );
  await page.route("**/admin/api/providers", (route) =>
    fulfilJson(route, {
      providers: [
        {
          name: "Remote provider",
          enabled: true,
          reachable: false,
          key_status: { state: "set" },
        },
      ],
    }),
  );
  await page.goto("/models/");
  await expect(
    page.getByText("Remote provider can’t take requests right now.", {
      exact: false,
    }),
  ).toBeVisible();
  const banner = page.getByRole("status").filter({
    hasText: "Remote provider can’t take requests right now.",
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
