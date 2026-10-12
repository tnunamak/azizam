import { test, expect } from "./fixtures/isolation";
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
  // The fixture gateway measures every capability it offers (its composed
  // workflows read their own engines). Add one Images capability with no
  // route evidence, as an engine fault leaves it.
  await page.route("**/me/api/home", async (route) => {
    const home = await (await route.fetch()).json();
    home.capabilities = [
      ...(home.capabilities ?? []),
      {
        capability: "image",
        capability_title: "Images",
        icon_key: "image",
        intent: "image.generate",
        model: "default",
        available: false,
        status: "unknown",
        reason_code: "unknown",
      },
    ];
    await route.fulfill({ json: home });
  });
  await page.goto("/models/");
  // The owner alert reports the engine fault, while the capability has no
  // measured route evidence: the health line says it is not checked yet.
  await page
    .getByRole("button", { name: "Gateway status: Not checked yet" })
    .click();
  const explanation = page.getByLabel("Gateway health");
  await expect(explanation).toContainText(
    "Image engine can’t take requests right now.",
  );
  await expect(explanation).toContainText("GatewayReachable");
  await expect
    .poll(async () =>
      (await new AxeBuilder({ page }).analyze()).violations.filter(
        (violation) => ["serious", "critical"].includes(violation.impact ?? ""),
      ),
    )
    .toEqual([]);
  await expect(
    explanation.getByRole("link", { name: "See why" }),
  ).toHaveAttribute("href", "/status/");
  await page.keyboard.press("Escape");
  // Off-page owner alerts hand keyboard focus to the affected section. The
  // target page already shows those diagnostics and must not repeat the banner.
  const banner = page.getByRole("status").filter({
    hasText: "Image engine can’t take requests right now.",
  });
  await expect(banner).toBeVisible();
  const action = banner.getByRole("link", { name: "See Status", exact: true });
  await action.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/status\/?#engines$/);
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
