import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("health popover explains a measured outage and links to Status", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route("**/admin/api/status", (route) =>
    route.fulfill({
      json: {
        status: {
          current_health: {
            state: "warn",
            tone: "warn",
            label: "Needs attention",
            detail: "",
            observation_available: true,
            services: [
              {
                service_id: "images",
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
      },
    }),
  );
  await page.goto("/ui/models/");
  await page
    .getByRole("button", { name: "Gateway status: Gateway reachable" })
    .click();
  const explanation = page.getByLabel("Gateway health");
  await expect(explanation).toContainText(
    "Image engine is unavailable. Check its connection on Status.",
  );
  await expect(explanation).toContainText("The gateway is reachable.");
  expect(
    (await new AxeBuilder({ page }).analyze()).violations.filter((violation) =>
      ["serious", "critical"].includes(violation.impact ?? ""),
    ),
  ).toEqual([]);
  await explanation.getByRole("link", { name: "View status" }).click();
  await expect(page).toHaveURL(/\/ui\/status\/?$/);
  // The owner banner sends keyboard focus to the affected diagnostic section,
  // including when it is activated while already on Status.
  const banner = page.getByRole("status").filter({
    hasText: "Image engine is unavailable. Check its connection on Status.",
  });
  const action = banner.getByRole("link", { name: "View status", exact: true });
  await action.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/ui\/status\/?#engines$/);
  await expect(
    page.getByRole("heading", { name: "Engines", exact: true, level: 2 }),
  ).toBeFocused();
  await action.focus();
  await page.keyboard.press("Enter");
  await expect(
    page.getByRole("heading", { name: "Engines", exact: true, level: 2 }),
  ).toBeFocused();
});

test("member health uses their allowed models and never requests owner diagnostics", async ({
  page,
}) => {
  const ownerRequests: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.startsWith("/admin/api/"))
      ownerRequests.push(request.url());
  });
  await page.route("**/me/api/session", (route) =>
    route.fulfill({
      json: {
        authenticated: true,
        csrf_token: "fixture",
        apps: [],
        user: {
          id: "member-health",
          name: "Member",
          role: "member",
          email: "member@example.test",
        },
      },
    }),
  );
  await page.route("**/me/api/home", (route) =>
    route.fulfill({
      json: {
        status: {
          state: "unavailable",
          reason: "Images are unavailable right now.",
        },
      },
    }),
  );
  await page.goto("/ui/models/");
  await page
    .getByRole("button", { name: "Gateway status: Gateway reachable" })
    .click();
  const explanation = page.getByLabel("Gateway health");
  await expect(explanation).toContainText("Images are unavailable right now.");
  await expect(explanation).toContainText("The gateway is reachable.");
  await expect(
    explanation.getByRole("link", { name: "View status" }),
  ).toHaveAttribute("href", "/ui/status/");
  expect(ownerRequests).toEqual([]);
});

test("an unreachable gateway takes priority over unavailable member models", async ({
  page,
}) => {
  await page.route("**/me/api/session", (route) =>
    route.fulfill({
      json: {
        authenticated: true,
        csrf_token: "fixture",
        apps: [],
        user: {
          id: "member-outage",
          name: "Member",
          role: "member",
          email: null,
        },
      },
    }),
  );
  await page.route("**/me/api/home", (route) =>
    route.fulfill({
      json: {
        status: {
          state: "unavailable",
          reason: "Images are unavailable right now.",
        },
      },
    }),
  );
  await page.route("**/health", (route) =>
    route.fulfill({ status: 503, json: { error: "unavailable" } }),
  );
  await page.goto("/ui/models/");
  await page
    .getByRole("button", { name: "Gateway status: Gateway unreachable" })
    .click();
  await expect(page.getByLabel("Gateway health")).toContainText(
    "The gateway could not be reached.",
  );
});
