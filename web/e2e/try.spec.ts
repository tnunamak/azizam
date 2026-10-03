import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

async function prepareTryKey(page: import("@playwright/test").Page) {
  await page.goto("/ui/try/");
  await page.getByRole("button", { name: "Prepare Try key" }).first().click();
  await expect(page.getByText("Try app key", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "I’ve saved it" }).click();
  await expect(page.getByText("Session key ready")).toBeVisible();
}

test("try creates a session key, runs public chat, and links a receipt", async ({
  page,
}) => {
  await prepareTryKey(page);
  const chatResponse = page.waitForResponse(
    (response) =>
      response.url().includes("/openai/v1/chat/completions") &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Run", exact: true }).click();
  const response = await chatResponse;
  expect(response.headers()["x-ai-gateway-request-id"]).toBeTruthy();
  expect(response.ok()).toBe(true);
  await expect(page.getByText("Chat result", { exact: true })).toBeVisible();
  await expect(
    page.getByText(
      "Your gateway is connected. This is a demonstration response from the isolated capture engine.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(page.getByText("Copy as", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("link", { name: /req_|chatcmpl|cmpl|resp/i }),
  ).toBeVisible();
});

test("try shows empty, public API error, decision 404, and non-chat results", async ({
  page,
}) => {
  await page.goto("/ui/try/");
  await expect(
    page.getByText("Prepare a session key", { exact: true }),
  ).toBeVisible();
  await prepareTryKey(page);

  await page.route("**/openai/v1/chat/completions", (route) =>
    route.fulfill({
      status: 403,
      json: { error: { message: "This key cannot use demo-chat." } },
    }),
  );
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("This key cannot use demo-chat.")).toBeVisible();
  await page.unroute("**/openai/v1/chat/completions");

  await page.getByRole("tab", { name: "Image" }).click();
  await page.route("**/openai/v1/images/generations", (route) =>
    route.fulfill({
      headers: { "x-ai-gateway-request-id": "req_image_try" },
      json: {
        id: "req_image_try",
        data: [
          {
            url: "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=",
          },
        ],
      },
    }),
  );
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Image result", { exact: true })).toBeVisible();

  await page.getByRole("tab", { name: "Speech" }).click();
  await page.route("**/openai/v1/audio/speech", (route) =>
    route.fulfill({
      headers: {
        "content-type": "audio/mpeg",
        "x-ai-gateway-request-id": "req_speech_try",
      },
      body: "audio",
    }),
  );
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Speech result", { exact: true })).toBeVisible();

  await page.getByRole("tab", { name: "Transcribe" }).click();
  await page.setInputFiles("input[type=file]", {
    name: "sample.wav",
    mimeType: "audio/wav",
    buffer: Buffer.from("RIFF....WAVEfmt "),
  });
  await page.route("**/openai/v1/audio/transcriptions", (route) =>
    route.fulfill({
      headers: { "x-ai-gateway-request-id": "req_transcribe_try" },
      json: { text: "The gateway is ready.", request_id: "req_transcribe_try" },
    }),
  );
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("The gateway is ready.")).toBeVisible();

  await page.getByRole("tab", { name: "Decide" }).click();
  await page.route("**/v1/systemone", (route) =>
    route.fulfill({
      status: 404,
      json: { error: { message: "not found" } },
    }),
  );
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(
    page.getByText("Decisions not enabled on this gateway", { exact: true }),
  ).toBeVisible();
  await page.unroute("**/v1/systemone");
  await page.route("**/v1/systemone", (route) =>
    route.fulfill({
      headers: { "x-ai-gateway-request-id": "req_decide_try" },
      json: {
        answers: {
          route: {
            type: "choice",
            choice: "Run chat",
            confidence: 0.7,
            probabilities: { "Run chat": 0.7, "Run embeddings": 0.3 },
          },
        },
      },
    }),
  );
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("70%")).toBeVisible();

  await page.getByRole("tab", { name: "Embed" }).click();
  await page.route("**/openai/v1/embeddings", (route) =>
    route.fulfill({
      headers: { "x-ai-gateway-request-id": "req_embed_try" },
      json: { data: [{ embedding: [0.1, -0.2, 0.4] }] },
    }),
  );
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Dimensions")).toBeVisible();
  await expect(page.getByText("3", { exact: true })).toBeVisible();
});

test("try catalog maps exact capability intents to the right tabs", async ({
  page,
}) => {
  await page.route("**/me/api/models/catalog", (route) =>
    route.fulfill({
      json: {
        schema_version: 1,
        object: "models.catalog",
        aliases: [
          catalogRow("chat", "chat.completions"),
          catalogRow("image", "image.generate"),
          catalogRow("tts", "speech.synthesis"),
          catalogRow("stt", "speech.transcription"),
          catalogRow("decide", "decision.systemone"),
          catalogRow("embeddings", "text.embeddings"),
        ],
        models: [],
      },
    }),
  );
  await prepareTryKey(page);
  await expect(page.getByText("Catalog loaded")).toBeVisible();

  await page.getByRole("tab", { name: "Transcribe" }).click();
  await expect(page.getByLabel("Model")).toContainText("stt model");
  await expect(page.getByLabel("Model")).not.toContainText("tts model");

  await page.getByRole("tab", { name: "Embed" }).click();
  await expect(page.getByLabel("Model")).toContainText("embeddings model");
  await expect(page.getByLabel("Model")).not.toContainText("chat model");
});

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390]) {
    test(`try states are accessible and fit at ${width}px in ${theme}`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      await page.addInitScript((value) => {
        localStorage.setItem("theme", value);
      }, theme);

      await page.goto("/ui/try/");
      await expect(page.getByRole("heading", { name: "Try" })).toBeVisible();
      await expect(
        page.getByText("Prepare a session key", { exact: true }),
      ).toBeVisible();
      await checkState(page, `try-empty-${theme}-${width}.png`);

      await prepareTryKey(page);
      await page.getByRole("button", { name: "Run", exact: true }).click();
      await expect(
        page.getByText("Chat result", { exact: true }),
      ).toBeVisible();
      await checkState(page, `try-result-${theme}-${width}.png`);

      await page.route("**/openai/v1/chat/completions", (route) =>
        route.fulfill({
          status: 403,
          json: { error: { message: "This key cannot use chat." } },
        }),
      );
      await page.getByRole("button", { name: "Run", exact: true }).click();
      await expect(page.getByText("This key cannot use chat.")).toBeVisible();
      await checkState(page, `try-error-${theme}-${width}.png`);
      await page.unroute("**/openai/v1/chat/completions");
    });
  }
}

async function checkState(page: import("@playwright/test").Page, file: string) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: `test-results/screens/${file}`,
    fullPage: true,
  });
  const violations = (await new AxeBuilder({ page }).analyze()).violations
    .filter((violation) =>
      ["serious", "critical"].includes(violation.impact ?? ""),
    )
    .map((violation) => violation.id);
  expect(violations).toEqual([]);
}

function catalogRow(id: string, intent: string) {
  return {
    id,
    display_name: `${id} model`,
    provider: "Local test",
    locality: "local",
    cost: { kind: "free", price: null },
    capabilities: [{ intent, label: intent }],
    use: { model: id },
  };
}
