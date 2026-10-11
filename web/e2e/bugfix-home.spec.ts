import { expect, test, type Page } from "@playwright/test";

const homeApi = "**/me/api/home";
const setupApi = "**/admin/api/setup-progress";
const mediaApi = "**/me/api/media?limit=*";

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
            id: "renamed-paid-chat",
            operation: "chat.completions",
            available: true,
            status: "ready",
            reason: "Chat alias is ready.",
          },
        ],
        capabilities: [
          ["chat", "Chat"],
          ["image", "Images"],
          ["video", "Video"],
          ["speech", "Speech"],
          ["transcribe", "Transcription"],
          ["decide", "Decisions"],
          ["rerank", "Reranking"],
        ].map(([capability, capability_title]) => ({
          capability,
          capability_title,
          intent:
            capability === "chat"
              ? "chat.completions"
              : `${capability}.generate`,
          model: capability === "chat" ? "fast" : capability,
          use_name: capability === "chat" ? "fast" : capability,
          icon_key: capability,
          available: capability !== "rerank",
          status: capability === "rerank" ? "unavailable" : "ready",
          reason:
            capability === "rerank"
              ? "Reranking is not available."
              : `${capability_title} is ready.`,
        })),
        credential_aliases: {},
        new_app_aliases: [
          {
            id: "renamed-paid-chat",
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

test("Home capability link carries chat into Models", async ({ page }) => {
  await routeFocusedHome(page);
  await routeTryCatalog(page);

  await page.goto("/ui/home/");
  // D1.1: Home lists tasks ("Make something") that open Try in the right
  // mode, not capabilities that open Models. The chat entry point must still
  // carry the chat capability and the home's chat model ("fast", not the
  // catalog's first alias).
  await expect(
    page.getByRole("heading", { name: "Make something", exact: true }),
  ).toBeVisible();

  const chatLink = page
    .locator('a[href*="/try/"][href*="capability=chat"]')
    .first();
  await expect(chatLink).toHaveCount(1);
  await expect(chatLink).toHaveAttribute("href", /[?&]model=fast(&|$)/);
  await chatLink.click();
  await expect(page).toHaveURL(/\/ui\/try\/\?capability=chat&model=fast$/);
});

test("Home picks the app recipe before creating a key", async ({ page }) => {
  await routeFocusedHome(page);

  await page.goto("/ui/home/");
  await expect(page.getByRole("textbox", { name: "Base URL" })).toHaveCount(0);
  await page.getByRole("button", { name: "Open WebUI" }).click();
  await expect(
    page.getByRole("button", { name: "Create key for Open WebUI" }),
  ).toBeVisible();
  // The "Other" choice reads "Another app" since D1.1.
  await page.getByRole("button", { name: "Another app", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Create key for Python" }),
  ).toBeVisible();
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

test("Home connects a renamed paid chat model without a default-model warning", async ({ page }) => {
  await routeFocusedHome(page);
  await page.goto("/ui/home/");
  await page.getByRole("button", { name: "Open WebUI", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Model", exact: true })).toHaveValue("renamed-paid-chat");
  await expect(page.getByText(/This key has no chat models|default model/)).toHaveCount(0);
  await expect(page.getByText("Your creations will appear here.")).toBeVisible();
});

test("Home explains a failed recent fetch and retries it", async ({ page }) => {
  await routeFocusedHome(page);
  let failed = true;
  await page.route(mediaApi, (route) => route.fulfill(
    failed ? { status: 503, json: {} } : { json: { assets: [], next_cursor: null } },
  ));
  await page.goto("/ui/home/");
  const recent = page.getByRole("region", { name: "Recent", exact: true });
  await expect(recent).toContainText("We couldn't tell why your recent creations didn't load. Try again.");
  await expect(recent).not.toContainText("503");
  failed = false;
  await recent.getByRole("button", { name: "Try again", exact: true }).click();
  await expect(recent).toContainText("Your creations will appear here.");
});
