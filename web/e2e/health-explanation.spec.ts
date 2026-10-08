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
  // D1: the health line counts capability issues for a signed-in person;
  // the popover keeps reachability on its own "Gateway" row.
  await page.getByRole("button", { name: "Gateway status: 1 issue" }).click();
  const explanation = page.getByLabel("Gateway health");
  await expect(explanation).toContainText(
    "Image engine is down. Check its connection on Status.",
  );
  await expect(explanation).toContainText("GatewayReachable");
  expect(
    (await new AxeBuilder({ page }).analyze()).violations.filter((violation) =>
      ["serious", "critical"].includes(violation.impact ?? ""),
    ),
  ).toEqual([]);
  await expect(
    explanation.getByRole("link", { name: "View status" }),
  ).toHaveAttribute("href", "/ui/status/");
  await page.keyboard.press("Escape");
  // Off-page owner alerts hand keyboard focus to the affected section. The
  // target page already shows those diagnostics and must not repeat the banner.
  const banner = page.getByRole("status").filter({
    hasText: "Image engine is down. Check its connection on Status.",
  });
  await expect(banner).toBeVisible();
  const action = banner.getByRole("link", { name: "View status", exact: true });
  await action.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/ui\/status\/?#engines$/);
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
  // D1: member health reads the capability rows the member is allowed to
  // use (the gateway derives "status" from the same rows).
  await page.route("**/me/api/home", (route) =>
    route.fulfill({
      json: {
        authenticated: true,
        user: {
          id: "member-health",
          name: "Member",
          role: "member",
          email: "member@example.test",
        },
        status: {
          state: "unavailable",
          reason: "Images are unavailable right now.",
        },
        aliases: [],
        receipts: [],
        capabilities: [
          {
            capability: "chat",
            capability_title: "Chat",
            icon_key: "chat",
            intent: "chat.completions",
            model: "default",
            available: true,
            status: "ready",
            reason_code: "ready",
            availability: {
              evidence: {
                source: "health_check",
                observed_at_s: Math.floor(Date.now() / 1000),
              },
            },
          },
          {
            capability: "image",
            capability_title: "Images",
            icon_key: "image",
            intent: "image.generate",
            model: "image",
            available: false,
            status: "unavailable",
            reason_code: "unavailable",
          },
        ],
      },
    }),
  );
  await page.goto("/ui/models/");
  await page.getByRole("button", { name: "Gateway status: 1 issue" }).click();
  const explanation = page.getByLabel("Gateway health");
  await expect(explanation).toContainText("Your AIImages down");
  await expect(explanation).toContainText("GatewayReachable");
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
  // D1.1: the popover separates reachability ("Gateway" row) from "Your AI".
  await expect(page.getByLabel("Gateway health")).toContainText(
    "GatewayNot reachable",
  );
});
