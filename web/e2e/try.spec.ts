import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import ltxCatalog from "../tests/fixtures/workflows-ltx-i2v.json";

async function prepareTryKey(page: import("@playwright/test").Page) {
  await page.goto("/ui/try/");
  // Minting the temporary key can take a while on a busy machine; wait for
  // it, so a slow mint fails here rather than as missing copy.
  await expect(page.getByText("Creating Try key")).toBeHidden({ timeout: 30_000 });
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Try uses a temporary key")).toBeVisible();
}

const TINY_PNG =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=";

async function routeImageJob(page: import("@playwright/test").Page) {
  await page.route("**/gateway/v1/media/jobs/image-generations", (route) =>
    route.fulfill({
      status: 202,
      json: {
        object: "media.job",
        id: "job_image_try",
        status: "queued",
        request_id: "req_image_try",
      },
    }),
  );
  await page.route("**/gateway/v1/media/jobs/job_image_try", (route) =>
    route.fulfill({
      json: {
        object: "media.job",
        id: "job_image_try",
        status: "succeeded",
        request_id: "req_image_try",
      },
    }),
  );
  await page.route("**/gateway/v1/media/jobs/job_image_try/content", (route) =>
    route.fulfill({
      headers: { "x-azizam-request-id": "req_image_try" },
      json: { id: "req_image_try", data: [{ url: TINY_PNG }] },
    }),
  );
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

  await page.getByRole("tab", { name: "Make a picture" }).click();
  // Pictures run as pollable media jobs, so a dropped connection cannot
  // lose the result.
  await routeImageJob(page);
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Image result", { exact: true })).toBeVisible();

  await page.getByRole("tab", { name: "Read text aloud" }).click();
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

  await page.getByRole("tab", { name: "Transcribe a recording" }).click();
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

  await page.getByRole("tab", { name: "Choose an option" }).click();
  await page.route("**/v1/systemone", async (route) => {
    const body = route.request().postDataJSON();
    expect(body.state).toBe("It is raining and the bus is 20 minutes late.");
    expect(body.questions.route.type).toBe("choice");
    expect(body.questions.route.criteria).toEqual({
      "Take a taxi": "Take a taxi",
      "Wait for the bus": "Wait for the bus",
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
            choice: "Take a taxi",
            confidence: 0.7,
            probabilities: { "Take a taxi": 0.7, "Wait for the bus": 0.3 },
          },
        },
      },
    }),
  );
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("70%")).toBeVisible();

  await page.getByRole("tab", { name: "Turn text into numbers" }).click();
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
  await page.getByRole("tab", { name: "Make a picture", exact: true }).click();
  await expect(page.getByRole("combobox").first()).not.toContainText(
    "edit-only",
  );
  await page.getByRole("tab", { name: "Read text aloud", exact: true }).click();
  await expect(page.getByRole("combobox").first()).not.toContainText(
    "music-only",
  );
  await page.getByRole("tab", { name: "Chat", exact: true }).click();

  await page.getByRole("tab", { name: "Transcribe a recording" }).click();
  await expect(page.getByRole("combobox").first()).toContainText("stt model");
  await expect(page.getByRole("combobox").first()).not.toContainText(
    "tts model",
  );

  await page.getByRole("tab", { name: "Turn text into numbers" }).click();
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
  await expect(page.getByText("Try uses a temporary key")).toBeVisible();
  await expect(
    page.getByRole("tab", { name: "Choose an option" }),
  ).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("combobox").first()).toContainText(
    "decide model",
  );

  await page.goto("/ui/try/?capability=chat&model=fast");
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Try uses a temporary key")).toBeVisible();
  await expect(page.getByRole("tab", { name: "Chat" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page.getByRole("combobox").first()).toContainText("fast");

  await page.goto("/ui/try/?capability=chat&model=chat");
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Try uses a temporary key")).toBeVisible();
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
  await page.getByRole("tab", { name: "Transcribe a recording" }).click();

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

test("capability chips keep full labels and never overlap on a phone", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/ui/try/");
  const tabs = page.getByRole("tablist", { name: "Try capability" });
  await expect(tabs.getByRole("tab")).toHaveCount(7);
  const layout = await tabs.evaluate((list) =>
    [...list.querySelectorAll<HTMLElement>('[role="tab"]')].map((tab) => {
      const box = tab.getBoundingClientRect();
      return {
        text: tab.textContent?.trim() ?? "",
        left: box.left,
        right: box.right,
        // Text wider than its chip means it spills into the next one.
        clipped: tab.scrollWidth > tab.clientWidth + 1,
        // A label that ends mid-word has an ellipsis or a cut-off box.
        overflow: getComputedStyle(tab).textOverflow,
      };
    }),
  );
  expect(layout.map((tab) => tab.text)).toEqual([
    "Chat",
    "Make a picture",
    "Read text aloud",
    "Transcribe a recording",
    "Choose an option",
    "Turn text into numbers",
    "Workflow",
  ]);
  for (const [index, tab] of layout.entries()) {
    expect(tab.clipped, `${tab.text} is clipped`).toBe(false);
    expect(tab.overflow).not.toBe("ellipsis");
    const next = layout[index + 1];
    if (next) {
      expect(tab.right, `${tab.text} overlaps ${next.text}`).toBeLessThanOrEqual(
        next.left + 0.5,
      );
    }
  }
  // The strip scrolls; the page itself does not.
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.getByRole("tab", { name: "Workflow" }).click();
  await expect(page.getByRole("tab", { name: "Workflow" })).toBeInViewport();
});

test("an image-to-video workflow offers a picture picker, checks before it runs and keeps values after a failure", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route("**/me/api/models/catalog", (route) =>
    route.fulfill({
      json: { schema_version: 1, aliases: [], models: [], counts: {} },
    }),
  );
  await page.route("**/me/api/workflows", (route) =>
    route.fulfill({ json: ltxCatalog }),
  );
  await page.goto("/ui/try/?workflow=video.generate.ltx-2.5-i2v");
  await expect(page.getByRole("tab", { name: "Workflow" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page.getByRole("combobox").first()).toContainText(
    "LTX 2.5 Distilled I2V",
  );

  // One heading for the tab: the chip, not the chip and a card title.
  await expect(page.getByRole("heading", { name: "Workflow" })).toHaveCount(0);
  await expect(page.getByText("Workflow", { exact: true })).toHaveCount(1);

  // A picture picker, in plain words; nothing reads as developer output.
  const group = page.getByRole("group", { name: /Starting picture/ });
  await expect(group.getByRole("button", { name: "Choose a file" })).toBeVisible();
  await expect(
    group.getByRole("button", { name: "Choose from Library" }),
  ).toBeVisible();
  await expect(group).toContainText("up to 10 MB");
  const prompt = page.getByRole("textbox", { name: /Describe the video/ });
  await expect(prompt).toHaveValue("");
  await expect(prompt).toHaveAttribute("placeholder", /boat|camera/i);
  // An example must not read as typed text: it is set apart in style.
  expect(
    await prompt.evaluate((el) => getComputedStyle(el, "::placeholder").fontStyle),
  ).toBe("italic");
  const visible = (await page.locator("main").innerText()).replace(/\s+/g, " ");
  for (const jargon of [
    "Request body",
    "Writes prompt",
    "Guided controls update",
    "Media input:",
    "string",
    "A small teal fish mark",
  ]) {
    expect(visible, jargon).not.toContain(jargon);
  }
  expect(await page.getByRole("textbox", { name: /Avoid/ }).inputValue()).toBe("");

  await page.screenshot({ path: "test-results/screens/try-workflow-media-top-390.png" });

  // Run is enabled, but nothing is sent: the missing parts are named.
  let posted = 0;
  await page.route("**/v1/videos", async (route) => {
    posted += 1;
    await route.fulfill({
      status: 400,
      json: { error: { message: "The gateway could not start this video." } },
    });
  });
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Choose a picture to start from")).toBeVisible();
  await expect(page.getByText("Describe the video first")).toBeVisible();
  await page.screenshot({ path: "test-results/screens/try-workflow-media-missing-390.png" });
  expect(posted).toBe(0);
  await expect(prompt).toBeFocused();

  // Choose a picture and describe the video.
  await page.setInputFiles("input[data-media-file-input]", {
    name: "boat.png",
    mimeType: "image/png",
    buffer: Buffer.from(TINY_PNG.split(",")[1], "base64"),
  });
  await expect(page.getByRole("img", { name: "Chosen picture" })).toBeVisible();
  await prompt.fill("A paper boat drifts across a pond.");
  await expect(page.getByText("Choose a picture to start from")).toHaveCount(0);
  await page.screenshot({ path: "test-results/screens/try-workflow-media-chosen-390.png" });
  await group.screenshot({ path: "test-results/screens/try-workflow-media-picker-390.png" });

  const request = page.waitForRequest("**/v1/videos");
  await page.getByRole("button", { name: "Run", exact: true }).click();
  const body = (await request).postDataJSON() as Record<string, unknown>;
  expect(body.input_reference).toMatch(/^data:image\/png;base64,/);
  expect(body.prompt).toBe("A paper boat drifts across a pond.");
  expect(JSON.stringify(body)).not.toContain('"value"');

  // The failed run says what failed and offers to edit, not to "try again".
  await expect(
    page.getByText("The gateway could not start this video."),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Try again" })).toHaveCount(0);
  await checkState(page, "try-workflow-media-error-390.png");
  await page.getByRole("button", { name: "Edit and run again" }).click();
  await expect(prompt).toBeFocused();
  await expect(prompt).toHaveValue("A paper boat drifts across a pond.");
  await expect(page.getByRole("img", { name: "Chosen picture" })).toBeVisible();
  expect(posted).toBe(1);
});

test("a picture survives a dropped connection and never reports the browser's error", async ({
  page,
}) => {
  await page.goto("/ui/try/");
  await prepareTryKey(page);
  await waitForCatalog(page);
  await page.getByRole("tab", { name: "Make a picture" }).click();
  await page.route("**/gateway/v1/media/jobs/image-generations", (route) =>
    route.fulfill({
      status: 202,
      json: { object: "media.job", id: "job_phone", status: "queued" },
    }),
  );
  let polls = 0;
  await page.route("**/gateway/v1/media/jobs/job_phone", (route) => {
    polls += 1;
    // The phone switched apps: the first check gets no answer at all.
    if (polls === 1) return route.abort("connectionfailed");
    return route.fulfill({
      json: { object: "media.job", id: "job_phone", status: "succeeded" },
    });
  });
  await page.route("**/gateway/v1/media/jobs/job_phone/content", (route) =>
    route.fulfill({ json: { data: [{ url: TINY_PNG }] } }),
  );
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Image result", { exact: true })).toBeVisible({
    timeout: 15_000,
  });
  expect(polls).toBe(2);
  await expect(page.getByText(/Failed to fetch/i)).toHaveCount(0);
  await expect(page.getByText(/request failed/i)).toHaveCount(0);
});

test("a picture whose connection never returns says it may still be finishing", async ({
  page,
}) => {
  await page.goto("/ui/try/");
  await prepareTryKey(page);
  await waitForCatalog(page);
  await page.getByRole("tab", { name: "Make a picture" }).click();
  await page.route("**/gateway/v1/media/jobs/image-generations", (route) =>
    route.abort("connectionfailed"),
  );
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(
    page.getByText("Your picture may still be finishing"),
  ).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("link", { name: "Open Library" })).toHaveAttribute(
    "href",
    "/ui/library/",
  );
  await expect(page.getByText(/Failed to fetch/i)).toHaveCount(0);
  await expect(page.getByText("Make a picture request failed")).toHaveCount(0);
});

for (const width of [390, 1440]) {
  test(`speech voices and model loading light ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ colorScheme: "light" });
    await page.addInitScript(() => localStorage.setItem("theme", "light"));
    let release!: () => void;
    const ready = new Promise<void>((resolve) => { release = resolve; });
    const speechModel = (id: string, title: string, defaultVoice: string) => ({
      id, title, capability: "speech", locality: "local",
      capabilities: [{ intent: "speech.synthesis" }],
      speech_voices: { voices: [{ id: defaultVoice, name: `${title} default` }, { id: `${id}-warm`, name: `${title} warm` }], default_voice: defaultVoice, allow_other: false },
    });
    await page.route("**/me/api/models/catalog", async (route) => {
      await ready;
      await route.fulfill({ json: { aliases: [], models: [speechModel("speech-a", "Voice studio", "a-default"), speechModel("speech-b", "Second studio", "b-default")] } });
    });
    await page.goto("/ui/try/?capability=speech", { waitUntil: "domcontentloaded" });
    await expect(page.getByRole("status", { name: "Loading models" })).toHaveAttribute("aria-busy", "true");
    await expect(page.getByRole("combobox", { name: "Model", exact: true })).toBeDisabled();
    await expect(page.getByRole("combobox", { name: "Voice", exact: true })).toBeDisabled();
    await page.screenshot({ path: `/home/tnunamak/.tmp/uxfb-shots/try-speech-loading-light-${width}.png`, fullPage: true });
    release();
    const voice = page.getByRole("combobox", { name: "Voice", exact: true });
    await expect(voice).toHaveValue("a-default");
    await voice.focus();
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("Enter");
    await expect(voice).toHaveValue("speech-a-warm");
    await page.getByRole("combobox", { name: "Model", exact: true }).click();
    await page.getByRole("option", { name: "Second studio", exact: true }).click();
    await expect(voice).toHaveValue("b-default");
    await expect(voice.locator("option")).toHaveText(["Second studio default", "Second studio warm"]);
    await page.screenshot({ path: `/home/tnunamak/.tmp/uxfb-shots/try-speech-voices-light-${width}.png`, fullPage: true });
    expect((await new AxeBuilder({ page }).analyze()).violations.filter((item) => ["serious", "critical"].includes(item.impact ?? ""))).toEqual([]);
    await page.route("**/openai/v1/audio/speech", async (route) => {
      expect(route.request().postDataJSON()).toMatchObject({ model: "speech-b", voice: "b-default" });
      await route.fulfill({ contentType: "audio/wav", body: Buffer.from("RIFF") });
    });
    const request = page.waitForRequest("**/openai/v1/audio/speech");
    await page.getByRole("button", { name: "Run", exact: true }).click();
    await request;
  });
}
