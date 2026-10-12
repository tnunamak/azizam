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
  await expect
    .poll(
      () =>
        page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
      { message: "Models page must fit the viewport" },
    )
    .toBe(true);
}

// Display states, copy, filters and details are asserted in vitest
// (src/app/models/models-seeded-catalog.spec.tsx). The browser keeps what
// jsdom cannot compute: real layout at 390 px, theme contrast (axe), overflow.
// One representative per concept, not per route x theme x width.
test("models seeded catalog: dark at 390px keeps rows on two lines, fits and passes axe", async ({
  page,
}) => {
  await preparePage(page, "dark", 390, seededCatalog);
  await expect(
    page.getByRole("button", { name: "Copy model name chat" }),
  ).toBeVisible();
  // A phone row is two lines (task, then alias); the facts are in Details.
  const chat = page.getByRole("rowgroup", { name: "Chat models" });
  await expect(chat.getByRole("list", { name: "Model facts" })).toHaveCount(0);
  await assertNoOverflow(page);
  await assertNoSeriousAxe(page);
  await page.screenshot({
    path: "test-results/screens/models-seeded-dark-390.png",
  });
});

test("models seeded catalog: light at 1440px shows row facts and passes axe", async ({
  page,
}) => {
  await preparePage(page, "light", 1440, seededCatalog);
  const chat = page.getByRole("rowgroup", { name: "Chat models" });
  const chatFacts = chat.getByRole("list", { name: "Model facts" });
  await expect(chatFacts.getByText("Reasoning")).toBeVisible();
  await expect(chatFacts.getByText("Vision")).toBeVisible();
  await assertNoOverflow(page);
  await assertNoSeriousAxe(page);
  await page.screenshot({
    path: "test-results/screens/models-seeded-light-1440.png",
    fullPage: true,
  });
});

test("models empty catalog: dark at 1440px passes axe", async ({ page }) => {
  await preparePage(page, "dark", 1440, emptyCatalog);
  await expect(page.getByText("No models are available yet")).toBeVisible();
  await assertNoOverflow(page);
  await assertNoSeriousAxe(page);
});

test("models error catalog: light at 390px fits and passes axe", async ({
  page,
}) => {
  await preparePage(
    page,
    "light",
    390,
    { error: { message: "Unavailable for this test" } },
    503,
  );
  await expect(
    page.getByText("Models catalog could not be loaded. Try again."),
  ).toBeVisible();
  await assertNoOverflow(page);
  await assertNoSeriousAxe(page);
});
