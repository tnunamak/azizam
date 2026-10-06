import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

async function prepareTryKey(page: import("@playwright/test").Page) {
  await page.goto("/ui/try/");
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Using a Try key")).toBeVisible();
}

async function waitForCatalog(page: import("@playwright/test").Page) {
  await expect(page.getByRole("combobox").first()).not.toContainText(
    "Not in the catalog",
  );
}

test("try creates a session key, runs public chat, and links a receipt", async ({
  page,
}) => {
  const catalogResponse = page.waitForResponse((response) =>
    response.url().includes("/me/api/models/catalog"),
  );
  await page.goto("/ui/try/");
  const chatResponse = page.waitForResponse(
    (response) =>
      response.url().includes("/openai/v1/chat/completions") &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Run", exact: true }).click();
  const catalog = await catalogResponse;
  expect(catalog.ok(), `catalog HTTP ${catalog.status()}`).toBe(true);
  await waitForCatalog(page);
  const response = await chatResponse;
  expect(response.request().postDataJSON().model).not.toBe("");
  const receiptId = response.headers()["x-azizam-receipt-id"] ?? "";
  expect(receiptId).not.toBe("");
  expect(response.headers()["x-azizam-request-id"]).toBe(receiptId);
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
    page.getByRole("link", { name: /Open receipt/ }),
  ).toHaveAttribute("title", receiptId);
});

test("try shows empty, public API error, decision 404, and non-chat results", async ({
  page,
}) => {
  await page.goto("/ui/try/");
  await prepareTryKey(page);
  await waitForCatalog(page);

  await page.route("**/openai/v1/chat/completions", (route) =>
    route.fulfill({
      status: 403,
      json: { error: { message: "This key cannot use demo-chat." } },
    }),
  );
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("This key cannot use demo-chat.")).toBeVisible();
  await page.unroute("**/openai/v1/chat/completions");

  await page.route("**/me/api/receipts/receipt_preferred", (route) =>
    route.fulfill({ json: { id: "receipt_preferred" } }),
  );
  await page.route("**/me/api/receipts/generic_request", (route) =>
    route.fulfill({ status: 404, json: { error: "not found" } }),
  );
  await page.route("**/openai/v1/chat/completions", (route) =>
    route.fulfill({
      headers: {
        "x-azizam-receipt-id": "receipt_preferred",
        "x-request-id": "generic_request",
      },
      json: {
        choices: [{ message: { content: "Receipt header priority works." } }],
      },
    }),
  );
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Receipt header priority works.")).toBeVisible();
  await expect(
    page.getByRole("link", { name: /Open receipt/ }),
  ).toHaveAttribute("title", "receipt_preferred");
  await expect(page.getByRole("link", { name: "generic_request" })).toHaveCount(
    0,
  );
  await page.unroute("**/openai/v1/chat/completions");
  await page.unroute("**/me/api/receipts/receipt_preferred");
  await page.unroute("**/me/api/receipts/generic_request");

  await page.getByRole("tab", { name: "Image" }).click();
  await page.route("**/openai/v1/images/generations", (route) =>
    route.fulfill({
      headers: { "x-azizam-request-id": "req_image_try" },
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
        "x-azizam-request-id": "req_speech_try",
      },
      body: "audio",
    }),
  );
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Speech result", { exact: true })).toBeVisible();

  await page.getByRole("tab", { name: "Transcription" }).click();
  await page.setInputFiles("input[type=file]", {
    name: "sample.wav",
    mimeType: "audio/wav",
    buffer: Buffer.from("RIFF....WAVEfmt "),
  });
  await page.route("**/openai/v1/audio/transcriptions", (route) =>
    route.fulfill({
      headers: { "x-azizam-request-id": "req_transcribe_try" },
      json: { text: "The gateway is ready.", request_id: "req_transcribe_try" },
    }),
  );
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("The gateway is ready.")).toBeVisible();

  await page.getByRole("tab", { name: "Decisions" }).click();
  await page.route("**/v1/systemone", async (route) => {
    const body = route.request().postDataJSON();
    expect(body.state).toBe("Gateway smoke test is starting.");
    expect(body.questions.route.type).toBe("choice");
    expect(body.questions.route.criteria).toEqual({
      "Run chat": "Run chat",
      "Run embeddings": "Run embeddings",
    });
    await route.fulfill({
      status: 404,
      json: { error: { message: "not found" } },
    });
  });
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(
    page.getByText("Decisions not enabled on this gateway", { exact: true }),
  ).toBeVisible();
  await page.unroute("**/v1/systemone");
  await page.route("**/v1/systemone", (route) =>
    route.fulfill({
      headers: { "x-azizam-request-id": "req_decide_try" },
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

  await page.getByRole("tab", { name: "Embeddings" }).click();
  await page.route("**/openai/v1/embeddings", (route) =>
    route.fulfill({
      headers: {
        "x-azizam-request-id": "req_embed_try",
        "x-litellm-response-cost": "0.0025",
      },
      json: { data: [{ embedding: [0.1, -0.2, 0.4] }] },
    }),
  );
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Dimensions")).toBeVisible();
  await expect(page.getByText("3", { exact: true })).toBeVisible();
  await expect(page.getByText("$0.002500")).toBeVisible();
  await expect(page.getByRole("link", { name: "req_embed_try" })).toHaveCount(
    0,
  );
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
          catalogRow("completion-only", "text.completions"),
          catalogRow("edit-only", "image.edit"),
          catalogRow("music-only", "audio.generate"),
          catalogRow("chat", "chat.completions", {
            locality: "remote",
            provider: "OpenRouter",
            cost: { kind: "paid", price: "$0.01/request" },
          }),
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
  await expect(page.getByRole("combobox").first()).toContainText(
    "chat model · Paid · $0.01/request",
  );
  await expect(page.getByRole("combobox").first()).not.toContainText(
    "completion-only",
  );
  await page.getByRole("tab", { name: "Image", exact: true }).click();
  await expect(page.getByRole("combobox").first()).not.toContainText(
    "edit-only",
  );
  await page.getByRole("tab", { name: "Speech", exact: true }).click();
  await expect(page.getByRole("combobox").first()).not.toContainText(
    "music-only",
  );
  await page.getByRole("tab", { name: "Chat", exact: true }).click();

  await page.getByRole("tab", { name: "Transcription" }).click();
  await expect(page.getByRole("combobox").first()).toContainText("stt model");
  await expect(page.getByRole("combobox").first()).not.toContainText(
    "tts model",
  );

  await page.getByRole("tab", { name: "Embeddings" }).click();
  await expect(page.getByRole("combobox").first()).toContainText(
    "embeddings model",
  );
  await expect(page.getByRole("combobox").first()).not.toContainText(
    "chat model",
  );
});

test("try query params select capability and preserve requested model", async ({
  page,
}) => {
  await page.route("**/me/api/models/catalog", (route) =>
    route.fulfill({
      json: {
        schema_version: 1,
        object: "models.catalog",
        aliases: [
          catalogRow("coder", "chat.completions"),
          catalogRow("chat", "chat.completions"),
          catalogRow("fast", "chat.completions"),
          catalogRow("decide", "decision.systemone"),
        ],
        models: [],
      },
    }),
  );
  await page.goto("/ui/try/?capability=decide");
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Using a Try key")).toBeVisible();
  await expect(page.getByRole("tab", { name: "Decisions" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page.getByRole("combobox").first()).toContainText(
    "decide model",
  );

  await page.goto("/ui/try/?capability=chat&model=fast");
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Using a Try key")).toBeVisible();
  await expect(page.getByRole("tab", { name: "Chat" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page.getByRole("combobox").first()).toContainText("fast");

  await page.goto("/ui/try/?capability=chat&model=chat");
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Using a Try key")).toBeVisible();
  await expect(page.getByRole("combobox").first()).toContainText("chat model");
  await expect(page.getByRole("combobox").first()).not.toContainText(
    "coder model",
  );
});

test("try records audio, uploads it, and stops microphone tracks", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const recordingState = { starts: 0, stops: 0, trackStops: 0 };
    Object.defineProperty(window, "__tryRecordingState", {
      value: recordingState,
      configurable: true,
    });

    const track = {
      stop: () => {
        recordingState.trackStops += 1;
      },
    };
    const fakeStream = { getTracks: () => [track] };
    Object.defineProperty(navigator, "mediaDevices", {
      value: { getUserMedia: async () => fakeStream },
      configurable: true,
    });

    class FakeMediaRecorder {
      state = "inactive";
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;

      start() {
        recordingState.starts += 1;
        this.state = "recording";
      }

      stop() {
        if (this.state !== "recording") {
          throw new Error("Fake recorder was not recording.");
        }
        recordingState.stops += 1;
        this.state = "inactive";
        this.ondataavailable?.({
          data: new Blob(["fake webm audio"], { type: "audio/webm" }),
        } as BlobEvent);
        this.onstop?.();
      }
    }

    Object.defineProperty(window, "MediaRecorder", {
      value: FakeMediaRecorder,
      configurable: true,
    });
  });

  await prepareTryKey(page);
  await waitForCatalog(page);
  await page.getByRole("tab", { name: "Transcription" }).click();

  await page.getByRole("button", { name: "Record" }).click();
  await expect(page.getByRole("button", { name: "Stop" })).toBeEnabled();
  await page.getByRole("button", { name: "Stop" }).click();
  await expect(page.getByText("recording.webm", { exact: true })).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as typeof window & {
              __tryRecordingState: { trackStops: number };
            }
          ).__tryRecordingState.trackStops,
      ),
    )
    .toBe(1);

  let uploadedBody = "";
  await page.route("**/openai/v1/audio/transcriptions", async (route) => {
    const requestBody = await route.request().postDataBuffer();
    expect(requestBody).not.toBeNull();
    uploadedBody = requestBody!.toString("utf8");
    await route.fulfill({
      json: { text: "Recorded text." },
    });
  });
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Recorded text.")).toBeVisible();
  expect(uploadedBody).toContain('name="file"; filename="recording.webm"');
  expect(uploadedBody).toContain('name="model"');

  await page.getByRole("button", { name: "Record" }).click();
  await expect(page.getByRole("button", { name: "Stop" })).toBeEnabled();
  await page.getByRole("tab", { name: "Chat" }).click();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as typeof window & {
              __tryRecordingState: { trackStops: number };
            }
          ).__tryRecordingState.trackStops,
      ),
    )
    .toBe(2);
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
      await expect(
        page.getByRole("heading", { name: "Try", exact: true }),
      ).toBeVisible();
      await checkState(page, `try-empty-${theme}-${width}.png`);

      await prepareTryKey(page);
      await waitForCatalog(page);
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
  if (file.includes("try-result-") && page.viewportSize()?.width === 390) {
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  }
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: `test-results/screens/${file}`,
    fullPage: page.viewportSize()?.width !== 390,
  });
  const violations = (await new AxeBuilder({ page }).analyze()).violations
    .filter((violation) =>
      ["serious", "critical"].includes(violation.impact ?? ""),
    )
    .map((violation) => ({
      id: violation.id,
      nodes: violation.nodes.map((node) => ({
        target: node.target,
        summary: node.failureSummary,
      })),
    }));
  if (violations.length) {
    console.log(JSON.stringify(violations, null, 2));
  }
  expect(violations).toEqual([]);
}

function catalogRow(
  id: string,
  intent: string,
  overrides: Record<string, unknown> = {},
) {
  return {
    id,
    display_name: `${id} model`,
    provider: "Local test",
    locality: "local",
    cost: { kind: "free", price: null },
    capabilities: [{ intent, label: intent }],
    use: { model: id },
    ...overrides,
  };
}
