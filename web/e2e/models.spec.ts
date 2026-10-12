import { type Page } from "@playwright/test";
import { test, expect } from "./fixtures/isolation";
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
      type: "language",
      architecture: {
        input_modalities: ["text", "image"],
        output_modalities: ["text"],
      },
      tags: ["tool-use", "reasoning", "vision"],
      capabilities: [
        {
          capability: "chat",
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
        status: "configured",
        available: true,
        reason_code: "configured",
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
          href: "/usage/?receipt=req_1",
          status: "succeeded",
          latency_ms: 820,
        },
      ],
      use: {
        model: "chat",
        curl: 'curl $AZIZAM_BASE/v1/chat/completions -H "Authorization: Bearer $AZIZAM_API_KEY" -H "Content-Type: application/json" -d \'{"model":"chat","messages":[{"role":"user","content":"Hello"}]}\'',
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
      type: "image",
      architecture: {
        input_modalities: ["text"],
        output_modalities: ["image"],
      },
      capabilities: [
        {
          capability: "image",
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
        curl: 'curl $AZIZAM_BASE/v1/images/generations -H "Authorization: Bearer $AZIZAM_API_KEY" -H "Content-Type: application/json" -d \'{"model":"image","prompt":"A small red cabin"}\'',
        python: { path: "/v1/images/generations", model: "image" },
        javascript: { path: "/v1/images/generations", model: "image" },
      },
    },
    {
      id: "decide",
      object: "model_catalog.alias",
      display_name: "decide",
      alias: true,
      operations: ["decide"],
      resolves_to: { decide: "local/decider" },
      description: "On-demand decision model for routing choices.",
      capabilities: [
        {
          capability: "decide",
          intent: "decision.systemone",
          label: "Decide",
          input_modalities: ["text"],
          output_modalities: ["json"],
          available: true,
        },
      ],
      provider: "Local test engine",
      locality: "local",
      cost: { kind: "free", price: null },
      timing: { typical_seconds: null, source: null },
      availability: {
        available: true,
        reason_code: "ready_on_request",
        measured: false,
        status: "idle_startable",
      },
      io: { input: ["text"], output: ["json"] },
      examples: [],
      recent_receipts: [],
      use: {
        model: "decide",
        python: { path: "/v1/systemone", model: "decide" },
        javascript: { path: "/v1/systemone", model: "decide" },
      },
    },
    {
      id: "tts",
      object: "model_catalog.alias",
      display_name: "tts",
      alias: true,
      operations: ["speech"],
      resolves_to: { speech: "local/speech" },
      description: "Speech synthesis when a backend is configured.",
      capabilities: [
        {
          capability: "speech",
          intent: "speech.synthesis",
          label: "Speech",
          input_modalities: ["text"],
          output_modalities: ["audio"],
          available: true,
        },
      ],
      provider: "Local test engine",
      locality: "local",
      cost: { kind: "free", price: null },
      timing: { typical_seconds: null, source: null },
      availability: {
        available: true,
        reason_code: "unknown",
        measured: true,
        status: "unknown",
      },
      io: { input: ["text"], output: ["audio"] },
      examples: [],
      recent_receipts: [],
      use: {
        model: "tts",
        python: { path: "/v1/audio/speech", model: "tts" },
        javascript: { path: "/v1/audio/speech", model: "tts" },
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
          capability: "chat",
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
        status: "configured",
        available: true,
        reason_code: "configured",
        measured: false,
      },
      io: { input: ["text"], output: ["text"] },
      examples: [],
      recent_receipts: [],
      use: {
        model: "demo-chat",
        curl: 'curl $AZIZAM_BASE/v1/chat/completions -H "Authorization: Bearer $AZIZAM_API_KEY"',
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
          capability: "image",
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
        curl: 'curl $AZIZAM_BASE/v1/images/generations -H "Authorization: Bearer $AZIZAM_API_KEY"',
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
  counts: { aliases: 4, models: 2, capabilities: 4 },
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
  const response = await page.goto("/models/");
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
        page.getByRole("button", { name: "Copy model name chat" }),
      ).toBeVisible();
      await expect(
        page.getByRole("heading", { name: "Make a picture", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("heading", { name: "Choose an option", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("heading", { name: "Read text aloud", exact: true }),
      ).toBeVisible();
      await expect(page.getByText(/Other options/)).toHaveCount(0);
      const chat = page.getByRole("rowgroup", { name: "Chat models" });
      const chatFacts = chat.getByRole("list", { name: "Model facts" });
      if (width < 640) {
        // A phone row is two lines (task, then alias); the facts are in Details.
        await expect(chatFacts).toHaveCount(0);
      } else {
        await expect(chatFacts.getByText("Reasoning")).toBeVisible();
        await expect(chatFacts.getByText("Vision")).toBeVisible();
        await expect(chatFacts.getByRole("listitem").first()).toHaveText(
          "text, image→totext",
        );
      }
      await expect(
        page.getByRole("button", { name: "Copy model name image" }),
      ).toBeVisible();
      // Rows make no local-cost claim; the page note below the table may.
      await expect(
        page
          .getByLabel("Available model names")
          .getByText("Local · no provider charge", { exact: true }),
      ).toHaveCount(0);
      await expect(
        page.getByRole("button", { name: "No provider charge", exact: true }),
      ).toHaveAttribute("aria-pressed", "true");
      await expect(page.getByText("Ready", { exact: true })).toHaveCount(0);
      await expect(
        page.getByText("Ready to start", { exact: true }),
      ).toBeVisible();
      await expect(page.getByText("Unknown", { exact: true })).toHaveCount(0);
      await expect(
        page.getByText("Ready to start when you make a request."),
      ).toHaveCount(0);
      await expect(page.getByText("Unknown", { exact: true })).toHaveCount(0);
      const decision = page.getByRole("rowgroup", { name: "Choose an option models" });
      await decision.getByRole("button", { name: "Details" }).click();
      await expect(
        page
          .getByRole("dialog")
          .getByText("Ready to start when you make a request."),
      ).toBeVisible();
      await page.keyboard.press("Escape");
      const speech = page.getByRole("rowgroup", { name: "Speech models" });
      await speech.getByRole("button", { name: "Details" }).click();
      await expect(
        page.getByRole("dialog").getByText("Unknown", { exact: true }),
      ).toHaveCount(0);
      await page.keyboard.press("Escape");
      await expect(
        page.getByText("Paid · $0.04 per image", { exact: true }),
      ).toBeVisible();
      await assertNoOverflow(page);
      await assertNoSeriousAxe(page);
      await page.screenshot({
        path: `test-results/screens/models-seeded-${theme}-${width}.png`,
        fullPage: width !== 390,
      });
    });

    test(`models empty catalog: ${theme} at ${width}px`, async ({ page }) => {
      await preparePage(page, theme, width, emptyCatalog);
      await expect(page.getByText("No models are available yet")).toBeVisible();
      await assertNoOverflow(page);
      await assertNoSeriousAxe(page);
      await page.screenshot({
        path: `test-results/screens/models-empty-${theme}-${width}.png`,
        fullPage: width !== 390,
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
  await page.getByPlaceholder("Search model names or providers").fill("image");
  await expect(
    page.getByRole("button", { name: "Copy model name image" }),
  ).toBeVisible();
  await expect(
    page.getByText("Best default for short conversations"),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Paid" }).click();
  await expect(
    page.getByRole("button", { name: "No provider charge", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByText("No models match these filters")).toBeVisible();
  await page.getByRole("button", { name: "Paid" }).click();
  await page.getByRole("button", { name: "Details" }).first().click();
  const imageDetails = page.getByRole("dialog");
  await expect(imageDetails.getByText("Example request")).toBeVisible();
  await expect(
    imageDetails.getByText(/\/v1\/images\/generations/),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: /All models/ }).click();
  await expect(
    page.getByRole("button", { name: "All models", exact: true }),
  ).toBeVisible();
  await expect(
    page
      .getByRole("table")
      .locator("code")
      .filter({ hasText: "provider/image-fast" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Details" }).first().click();
  const sheet = page.getByRole("dialog");
  await expect(sheet).toBeVisible();
  await expect(sheet).not.toContainText("Typical time");
  await expect(sheet).not.toContainText("Not measured yet");
});
