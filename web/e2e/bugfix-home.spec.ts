import { expect, test, type Page } from "@playwright/test";

const homeApi = "**/me/api/home";
const setupApi = "**/admin/api/setup-progress";
const mediaApi = "**/me/api/media?limit=5";

async function routeFocusedHome(page: Page) {
  await page.route(homeApi, (route) =>
    route.fulfill({
      json: {
        authenticated: true,
        user: { email: "browser@example.test", name: "Browser" },
        api_base_url: "http://127.0.0.1:18180/openai/v1",
        public_base_url: "http://127.0.0.1:18180",
        apps: [],
        aliases: [
          {
            id: "fast",
            operation: "chat.completions",
            available: true,
            status: "ready",
            reason: "Chat alias is ready.",
          },
        ],
        capabilities: [
          {
            capability: "chat",
            intent: "chat.completions",
            model: "fast",
            available: true,
            status: "ready",
            reason: "Chat is ready.",
          },
        ],
        credential_aliases: {},
        new_app_aliases: [
          {
            id: "fast",
            operation: "chat.completions",
            available: true,
            status: "ready",
            reason: "Chat alias is ready.",
          },
        ],
        receipts: [],
        creations: null,
        status: { state: "ready", reason: "Ready" },
      },
    }),
  );
  await page.route(setupApi, (route) =>
    route.fulfill({ json: { complete: true, steps: [] } }),
  );
  await page.route(mediaApi, (route) =>
    route.fulfill({ json: { assets: [], next_cursor: null } }),
  );
}

async function routeTryCatalog(page: Page) {
  await page.route("**/me/api/models/catalog", (route) =>
    route.fulfill({
      json: {
        schema_version: 1,
        object: "models.catalog",
        aliases: [
          catalogRow("coder", "chat.completions"),
          catalogRow("fast", "chat.completions"),
        ],
        models: [],
      },
    }),
  );
  await page.route("**/me/api/workflows", (route) =>
    route.fulfill({ json: { workflows: [] } }),
  );
}

test("Home capability Try link carries chat and fast into Try", async ({
  page,
}) => {
  await routeFocusedHome(page);
  await routeTryCatalog(page);

  await page.goto("/ui/home/");
  await expect(
    page.getByRole("heading", { name: "What you can use", exact: true }),
  ).toBeVisible();

  const tryLink = page
    .locator('a[href*="/try/?capability=chat&model=fast"]')
    .first();
  await expect(tryLink).toHaveCount(1);
  await tryLink.click();
  await expect(page).toHaveURL(/\/ui\/try\/\?capability=chat&model=fast$/);

  await page.getByRole("button", { name: "Prepare Try key" }).first().click();
  await page.getByRole("button", { name: "I’ve saved it" }).click();
  await expect(page.getByText("Catalog loaded", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("tab", { name: "Chat", exact: true }),
  ).toHaveAttribute("aria-selected", "true");
  await expect(page.getByLabel("Model", { exact: true })).toContainText(
    "fast model",
  );
  await expect(page.getByLabel("Model", { exact: true })).not.toContainText(
    "coder model",
  );
});

function catalogRow(id: string, intent: string) {
  return {
    id,
    object: "model_catalog.alias",
    alias: true,
    name: `${id} model`,
    capability: intent,
    capabilities: [{ intent }],
    available: true,
    status: "ready",
  };
}
