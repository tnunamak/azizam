import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const seededCatalog = {
  schema_version: 1,
  object: "models.catalog",
  aliases: [
    {
      id: "chat",
      object: "model_catalog.alias",
      display_name: "chat",
      alias: true,
      operations: ["chat"],
      resolves_to: { chat: "demo-chat" },
      description: "Best default for short conversations and app setup tests.",
      capabilities: [
        {
          intent: "chat.completions",
          label: "Chat",
          input_modalities: ["text"],
          output_modalities: ["text"],
          streaming: true,
          available: true,
        },
      ],
      provider: "Local test engine",
      locality: "local",
      cost: { kind: "free", price: null },
      timing: { typical_seconds: 0.8, source: "receipts", sample_count: 2 },
      availability: {
        available: true,
        reason:
          "Allowed by your access plan; live availability is not measured yet",
        measured: false,
      },
      io: { input: ["text"], output: ["text"] },
      examples: [
        {
          kind: "chat",
          body: {
            model: "chat",
            messages: [{ role: "user", content: "Hello" }],
          },
        },
      ],
      recent_receipts: [
        {
          request_id: "req_1",
          href: "/r/req_1",
          status: "succeeded",
          latency_ms: 820,
        },
      ],
      use: {
        model: "chat",
        curl: 'curl $AI_GATEWAY_BASE/v1/chat/completions -H "Authorization: Bearer $AI_GATEWAY_API_KEY" -H "Content-Type: application/json" -d \'{"model":"chat","messages":[{"role":"user","content":"Hello"}]}\'',
        python: { path: "/v1/chat/completions", model: "chat" },
        javascript: { path: "/v1/chat/completions", model: "chat" },
      },
    },
    {
      id: "image",
      object: "model_catalog.alias",
      display_name: "image",
      alias: true,
      operations: ["image"],
      resolves_to: { image: "provider/image-fast" },
      description: "Image generation through the configured remote provider.",
      capabilities: [
        {
          intent: "image.generate",
          label: "Images",
          input_modalities: ["text"],
          output_modalities: ["image"],
          available: true,
        },
      ],
      provider: "OpenRouter",
      locality: "remote",
      cost: { kind: "paid", price: "$0.04 per image" },
      timing: { typical_seconds: null, source: null },
      availability: {
        available: false,
        reason: "Two requests are ahead of yours.",
        measured: true,
        status: "busy",
      },
      io: { input: ["text"], output: ["image"] },
      examples: [
        {
          kind: "image",
          body: { model: "image", prompt: "A small red cabin" },
        },
      ],
      recent_receipts: [],
      use: {
        model: "image",
        curl: 'curl $AI_GATEWAY_BASE/v1/images/generations -H "Authorization: Bearer $AI_GATEWAY_API_KEY" -H "Content-Type: application/json" -d \'{"model":"image","prompt":"A small red cabin"}\'',
        python: { path: "/v1/images/generations", model: "image" },
        javascript: { path: "/v1/images/generations", model: "image" },
      },
    },
  ],
  models: [
    {
      id: "demo-chat",
      object: "model_catalog.model",
      display_name: "demo-chat",
      alias: false,
      operations: [],
      resolves_to: {},
      description: "Low latency chat model",
      capabilities: [
        {
          intent: "chat.completions",
          label: "Chat",
          input_modalities: ["text"],
          output_modalities: ["text"],
          streaming: true,
          available: true,
        },
      ],
      provider: "Local test engine",
      locality: "local",
      cost: { kind: "free", price: null },
      timing: { typical_seconds: 0.8, source: "receipts", sample_count: 2 },
      availability: {
        available: true,
        reason:
          "Allowed by your access plan; live availability is not measured yet",
        measured: false,
      },
      io: { input: ["text"], output: ["text"] },
      examples: [],
      recent_receipts: [],
      use: {
        model: "demo-chat",
        curl: 'curl $AI_GATEWAY_BASE/v1/chat/completions -H "Authorization: Bearer $AI_GATEWAY_API_KEY"',
        python: { path: "/v1/chat/completions", model: "demo-chat" },
        javascript: { path: "/v1/chat/completions", model: "demo-chat" },
      },
    },
    {
      id: "provider/image-fast",
      object: "model_catalog.model",
      display_name: "provider/image-fast",
      alias: false,
      operations: [],
      resolves_to: {},
      description: null,
      capabilities: [
        {
          intent: "image.generate",
          label: "Images",
          input_modalities: ["text"],
          output_modalities: ["image"],
          available: true,
        },
      ],
      provider: "OpenRouter",
      locality: "remote",
      cost: { kind: "paid", price: "$0.04 per image" },
      timing: { typical_seconds: null, source: null },
      availability: {
        available: false,
        reason: "Two requests are ahead of yours.",
        measured: true,
        status: "busy",
      },
      io: { input: ["text"], output: ["image"] },
      examples: [],
      recent_receipts: [],
      use: {
        model: "provider/image-fast",
        curl: 'curl $AI_GATEWAY_BASE/v1/images/generations -H "Authorization: Bearer $AI_GATEWAY_API_KEY"',
        python: {
          path: "/v1/images/generations",
          model: "provider/image-fast",
        },
        javascript: {
          path: "/v1/images/generations",
          model: "provider/image-fast",
        },
      },
    },
  ],
  counts: { aliases: 2, models: 2, capabilities: 2 },
};

const emptyCatalog = {
  schema_version: 1,
  object: "models.catalog",
  aliases: [],
  models: [],
  counts: { aliases: 0, models: 0, capabilities: 0 },
};

async function routeCatalog(page: Page, payload: unknown, status = 200) {
  await page.route("**/me/api/models/catalog", (route) =>
    route.fulfill({ status, json: payload }),
  );
}

async function preparePage(
  page: Page,
  theme: "light" | "dark",
  width: 1440 | 390,
  payload: unknown,
  status = 200,
) {
  await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
  await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
  await page.addInitScript((value) => {
    localStorage.setItem("theme", value);
  }, theme);
  await routeCatalog(page, payload, status);
  const response = await page.goto("/ui/models/");
  expect(response?.status()).toBe(200);
  await expect(
    page.getByRole("heading", { name: "Models", exact: true }),
  ).toBeVisible();
}

async function assertNoSeriousAxe(page: Page) {
  const a11y = await new AxeBuilder({ page }).analyze();
  expect(
    a11y.violations
      .filter((violation) =>
        ["serious", "critical"].includes(violation.impact ?? ""),
      )
      .map((violation) => violation.id),
  ).toEqual([]);
}

async function assertNoOverflow(page: Page) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
    "Models page must fit the viewport",
  ).toBe(true);
}

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390] as const) {
    test(`models seeded catalog: ${theme} at ${width}px`, async ({ page }) => {
      await preparePage(page, theme, width, seededCatalog);
      await expect(
        page.getByText("Best default for short conversations"),
      ).toBeVisible();
      await expect(page.getByText("Image generation through")).toBeVisible();
      await expect(
        page.getByText("Local · no provider charge", { exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("button", { name: "No provider charge", exact: true }),
      ).toHaveAttribute("aria-pressed", "true");
      await expect(page.getByText("Ready", { exact: true })).toHaveCount(0);
      await expect(
        page.getByText("Paid · $0.04 per image", { exact: true }),
      ).toBeVisible();
      await assertNoOverflow(page);
      await assertNoSeriousAxe(page);
      await page.screenshot({
        path: `test-results/screens/models-seeded-${theme}-${width}.png`,
        fullPage: true,
      });
    });

    test(`models empty catalog: ${theme} at ${width}px`, async ({ page }) => {
      await preparePage(page, theme, width, emptyCatalog);
      await expect(page.getByText("No models are available yet")).toBeVisible();
      await assertNoOverflow(page);
      await assertNoSeriousAxe(page);
      await page.screenshot({
        path: `test-results/screens/models-empty-${theme}-${width}.png`,
        fullPage: true,
      });
    });

    test(`models error catalog: ${theme} at ${width}px`, async ({ page }) => {
      await preparePage(
        page,
        theme,
        width,
        { error: { message: "Unavailable for this test" } },
        503,
      );
      await expect(
        page.getByText("Models catalog could not be loaded. Try again."),
      ).toBeVisible();
      await assertNoOverflow(page);
      await assertNoSeriousAxe(page);
      await page.screenshot({
        path: `test-results/screens/models-error-${theme}-${width}.png`,
        fullPage: true,
      });
    });
  }
}

test("models filters, snippets, table and detail sheet work", async ({
  page,
}) => {
  await preparePage(page, "light", 1440, seededCatalog);
  await page
    .getByPlaceholder("Search models, aliases or providers")
    .fill("image");
  await expect(page.getByText("Image generation through")).toBeVisible();
  await expect(
    page.getByText("Best default for short conversations"),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Paid" }).click();
  await expect(
    page.getByRole("button", { name: "No provider charge", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByText("No models match these filters")).toBeVisible();
  await page.getByRole("button", { name: "Paid" }).click();
  await page.getByRole("button", { name: "Use", exact: true }).click();
  await expect(
    page.getByText("Copy the alias or a starter request."),
  ).toBeVisible();
  await expect(page.getByText(/\/v1\/images\/generations/)).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: /All models/ }).click();
  await expect(
    page.getByRole("cell", { name: "provider/image-fast" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Details" }).first().click();
  const sheet = page.getByRole("dialog");
  await expect(sheet).toBeVisible();
  await expect(sheet).toContainText("Typical time");
  await expect(sheet).toContainText("Not measured yet");
});
