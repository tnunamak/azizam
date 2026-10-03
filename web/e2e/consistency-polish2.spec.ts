import { expect, test } from "@playwright/test";
import { workflowsPolishCatalog } from "./fixtures/workflows-polish";
import AxeBuilder from "@axe-core/playwright";

test("Providers presents the current API shape without inventing readiness or names", async ({
  page,
}) => {
  await page.route("**/admin/api/providers", (route) =>
    route.fulfill({
      json: {
        providers: [
          {
            id: "internal-provider-record",
            name: "internal-provider-record",
            enabled: true,
            key_status: { state: "set" },
            spend: { status: "known", daily_usd: 1, monthly_usd: 12.5 },
            caps: { daily_usd: 2, monthly_usd: 50 },
          },
          {
            id: "missing-key",
            name: "Missing key provider",
            enabled: true,
            key_status: { state: "missing" },
            spend: { status: "unknown", monthly_usd: 999 },
          },
        ],
      },
    }),
  );
  for (const theme of ["light", "dark"]) {
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      await page.addInitScript(
        (value) => localStorage.setItem("theme", value),
        theme,
      );
      await page.goto("/ui/providers/");
      const provider = page.locator("article").filter({
        has: page.getByRole("heading", {
          name: "Remote provider",
          exact: true,
        }),
      });
      await expect(provider.locator("code")).toHaveText(
        "internal-provider-record",
      );
      await expect(provider.getByText("Set", { exact: true })).toBeVisible();
      await expect(
        provider.getByText("Availability not checked", { exact: true }),
      ).toBeVisible();
      await expect(provider).toContainText("$12.50 of $50.00 this month");
      const missing = page.locator("article").filter({
        has: page.getByRole("heading", {
          name: "Missing key provider",
          exact: true,
        }),
      });
      await expect(
        missing.getByText("Unavailable", { exact: true }),
      ).toBeVisible();
      await expect(missing.getByText("Missing", { exact: true })).toBeVisible();
      await expect(
        missing.getByText("Not measured yet", { exact: true }),
      ).toBeVisible();
      await expect(missing).not.toContainText("999");
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      expect(
        (await new AxeBuilder({ page }).analyze()).violations.filter((v) =>
          ["serious", "critical"].includes(v.impact ?? ""),
        ),
      ).toEqual([]);
      await page.screenshot({
        path: `test-results/screens/providers-contract-${theme}-${width}.png`,
        fullPage: true,
      });
    }
  }
});

test("unknown provider readiness stays distinct from observed unavailability", async ({
  page,
}) => {
  await page.route("**/admin/api/providers", (route) =>
    route.fulfill({
      json: {
        providers: [
          {
            id: "unknown",
            name: "Unchecked provider",
            enabled: true,
            reachable: null,
            key_set: true,
          },
          {
            id: "down",
            name: "Unreachable provider",
            enabled: true,
            reachable: false,
            key_set: true,
          },
          {
            id: "ready",
            name: "Reachable provider",
            enabled: true,
            reachable: true,
            key_set: true,
          },
        ],
      },
    }),
  );
  await page.goto("/ui/providers/");
  const unknown = page.locator("article").filter({
    has: page.getByRole("heading", {
      name: "Unchecked provider",
      exact: true,
    }),
  });
  await expect(
    unknown.getByText("Availability not checked", { exact: true }),
  ).toBeVisible();
  await expect(unknown.getByText("Unavailable", { exact: true })).toHaveCount(
    0,
  );
  const down = page.locator("article").filter({
    has: page.getByRole("heading", {
      name: "Unreachable provider",
      exact: true,
    }),
  });
  await expect(down.getByText("Unavailable", { exact: true })).toBeVisible();
  const ready = page.locator("article").filter({
    has: page.getByRole("heading", {
      name: "Reachable provider",
      exact: true,
    }),
  });
  await expect(ready.getByText("Ready", { exact: true })).toBeVisible();
});

test("workflow cards keep internal IDs behind an explicit copy or details action", async ({
  page,
}) => {
  await page.route("**/me/api/workflows", (route) =>
    route.fulfill({ json: workflowsPolishCatalog }),
  );
  await page.route("**/me/api/media?**", (route) =>
    route.fulfill({ json: { assets: [], models: [], next_cursor: null } }),
  );

  await page.goto("/ui/workflows/");
  const card = page
    .locator("article")
    .filter({ has: page.getByRole("heading", { name: "Product render" }) });
  await expect(
    card.getByRole("heading", { name: "Product render" }),
  ).toBeVisible();
  await expect(
    card.getByText("image.product-render", { exact: true }),
  ).toHaveCount(0);
  await expect(
    card.getByRole("button", { name: "Copy workflow id for Product render" }),
  ).toBeVisible();

  await card.getByRole("button", { name: "Details", exact: true }).click();
  const sheet = page.getByRole("dialog");
  await expect(
    sheet.getByText("image.product-render", { exact: true }),
  ).toBeVisible();
});

test("an observed-up engine with a bad health tone is shown as unavailable", async ({
  page,
}) => {
  await page.route("**/admin/api/operator/service-registry", (route) =>
    route.fulfill({
      json: {
        service_registry: {
          schema_version: 1,
          services: {
            engine_chat: {
              id: "engine_chat",
              name: "Chat engine",
              kind: "upstream",
              source: "ui",
              enabled: true,
              editable: true,
              capabilities: ["chat"],
              role: "primary",
              config: { protocol: "openai-compatible" },
            },
          },
        },
      },
      headers: { etag: "consistency-test" },
    }),
  );
  await page.route("**/admin/api/operator/service-presets", (route) =>
    route.fulfill({ json: { presets: [] } }),
  );
  await page.route("**/admin/api/status", (route) =>
    route.fulfill({
      json: {
        status: {
          current_health: {
            observation_available: true,
            services: [
              {
                service_id: "engine_chat",
                name: "Chat engine",
                enabled: true,
                observation: "up",
                tone: "bad",
              },
            ],
          },
          backends: [],
        },
      },
    }),
  );

  await page.goto("/ui/engines/");
  const card = page.locator("article").filter({ hasText: "Chat engine" });
  await expect(card.getByText("Unavailable", { exact: true })).toBeVisible();
  await expect(card.getByText("Ready", { exact: true })).toHaveCount(0);
});

test("empty provider status offers connection review without promising provider creation", async ({
  page,
}) => {
  await page.route("**/admin/api/providers", (route) =>
    route.fulfill({ json: { providers: [] } }),
  );

  await page.goto("/ui/providers/");
  const configure = page.getByRole("link", { name: "Review connections" });
  await expect(configure).toHaveAttribute("href", "/ui/engines/");
  await page.unroute("**/admin/api/providers");
  await page.route("**/admin/api/providers", (route) =>
    route.fulfill({ status: 404, json: {} }),
  );
  await page.reload();
  await expect(
    page.getByText("Provider controls are not installed", { exact: true }),
  ).toBeVisible();
  await expect(configure).toHaveAttribute("href", "/ui/engines/");
});

test("Status offers a local action when no remote providers are configured", async ({
  page,
}) => {
  await page.route("**/admin/api/providers", (route) =>
    route.fulfill({ json: { providers: [] } }),
  );
  await page.goto("/ui/status/");
  await expect(
    page.getByText("No remote providers are configured.", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Review connections", exact: true }),
  ).toHaveAttribute("href", "/ui/engines/");
});

test("legacy email names remain contact details on People and in search", async ({
  page,
}) => {
  const person = {
    id: "email-person",
    name: "person@example.test",
    email: "person@example.test",
    profile_id: null,
    plan_name: null,
    disabled: false,
    last_active: null,
    requests_this_month: null,
  };
  await page.route("**/admin/api/people", (route) =>
    route.fulfill({ json: { people: [person], profiles: [] } }),
  );
  await page.route("**/admin/api/users", (route) =>
    route.fulfill({
      json: { users: [{ ...person, display_name: person.email }] },
    }),
  );
  await page.goto("/ui/people/");
  await expect(page.getByText("Unnamed person", { exact: true })).toBeVisible();
  await expect(page.getByText(person.email, { exact: true })).toHaveCount(1);
  await page.keyboard.press("Control+k");
  await page
    .getByRole("combobox", { name: "Search your gateway" })
    .fill(person.email);
  const result = page.getByRole("option", {
    name: `Person ${person.email}`,
    exact: true,
  });
  await expect(result).toBeVisible();
  await expect(result.locator("span.text-muted-foreground")).toHaveText(
    person.email,
  );
});

test("personal access app hides only its exact generated email-based name", async ({
  page,
}) => {
  const email = "owner@example.test";
  const personalApp = {
    id: "app-personal",
    name: `${email} personal access`,
    kind: "personal",
    disabled_at: null,
    created_at: 1_760_000_000,
    profile: { name: "Default" },
    can_mint_keys: true,
    credentials: [],
  };
  const customApp = {
    ...personalApp,
    id: "app-custom",
    name: `${email} personal access app`,
  };
  await page.route("**/me/api/session", (route) =>
    route.fulfill({
      json: {
        authenticated: true,
        csrf_token: "test-csrf",
        user: { id: "owner", email, name: "", role: "owner" },
        apps: [personalApp, customApp],
      },
    }),
  );
  await page.route("**/.well-known/ai-gateway", (route) =>
    route.fulfill({ json: { recipes: {} } }),
  );
  await page.route("**/me/api/apps/app-personal/keys", (route) =>
    route.fulfill({ json: { credential: { api_key: "test-key" } } }),
  );

  await page.goto("/ui/apps/");
  const card = page.locator("article").filter({
    has: page.getByRole("heading", { name: "Personal access", exact: true }),
  });
  await expect(
    card.getByRole("heading", { name: "Personal access", exact: true }),
  ).toBeVisible();
  await expect(card).not.toContainText(email);
  await expect(
    page.getByRole("heading", {
      name: `${email} personal access app`,
      exact: true,
    }),
  ).toBeVisible();
  await card
    .getByRole("button", { name: "Actions for Personal access" })
    .click();
  await page.getByRole("menuitem", { name: "Create another key" }).click();
  await expect(
    page.getByRole("dialog", { name: "Connect Personal access" }),
  ).toBeVisible();
});
