import { expect, test } from "@playwright/test";
import { workflowsPolishCatalog } from "./fixtures/workflows-polish";

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
  const card = page
    .locator("article")
    .filter({
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
