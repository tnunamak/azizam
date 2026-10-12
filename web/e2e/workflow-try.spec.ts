import { expect, test, type Page, type Route } from "@playwright/test";

const emptyModelsCatalog = {
  schema_version: 1,
  object: "models.catalog",
  aliases: [],
  models: [],
  counts: { aliases: 0, models: 0, capabilities: 0 },
};

const workflowsCatalog = {
  schema_version: 1,
  object: "workflow.catalog",
  discovery_url: "/gateway/v1/workflows",
  workflows: [
    {
      id: "image.generate.poster",
      short_id: "poster",
      title: "Poster image",
      description: "Image workflow served through the OpenAI image route.",
      engine_binding: "comfyui",
      native_route: "/v1/images/generations",
      inputs: [
        { name: "prompt", type: "string", required: true, accepted: true },
        {
          name: "style",
          type: "string",
          enum: ["studio", "flatlay"],
          default: "studio",
          required: true,
          accepted: true,
        },
        {
          name: "steps",
          type: "integer",
          default: 8,
          minimum: 1,
          maximum: 12,
          required: true,
          accepted: true,
        },
        {
          name: "strength",
          type: "number",
          default: 0.35,
          minimum: 0,
          maximum: 1,
          required: false,
          accepted: true,
        },
        {
          name: "width",
          type: "integer",
          default: 1024,
          minimum: 64,
          maximum: 2048,
          required: true,
          accepted: true,
        },
        {
          name: "height",
          type: "integer",
          default: 1024,
          minimum: 64,
          maximum: 2048,
          required: true,
          accepted: true,
        },
      ],
      example_request: {
        method: "POST",
        path: "/v1/images/generations",
        content_type: "application/json",
        body: {
          model: "poster",
          prompt: "A teal poster.",
          style: "studio",
          strength: 0.35,
          size: "1024x1024",
          extensions: { "com.azizam.workflow": { seed: 42, steps: 8 } },
        },
        form: {},
        files: {},
        curl: 'curl http://localhost:5000/v1/images/generations -H \'Authorization: Bearer $GATEWAY_API_KEY\' -H \'Content-Type: application/json\' -d \'{"model":"poster","prompt":"A teal poster."}\'',
      },
      openapi: {
        execution: {
          sync: {
            path: "/v1/images/generations",
            fields: {
              prompt: { in: "body", field: "prompt" },
              style: { in: "body", field: "style" },
              width: {
                in: "body",
                field: "size",
                combine: ["width", "height"],
              },
              height: {
                in: "body",
                field: "size",
                combine: ["width", "height"],
              },
              seed: { in: "extension", field: "seed" },
              steps: { in: "extension", field: "steps" },
              strength: { in: "body", field: "strength" },
            },
          },
        },
      },
    },
    {
      id: "video.edit.remix",
      short_id: "remix",
      title: "Remix video",
      description: "Upload-driven video edit workflow.",
      engine_binding: "comfyui",
      native_route: "/gateway/v1/workflows/remix/run",
      inputs: [
        { name: "prompt", type: "string", required: true, accepted: true },
        {
          name: "mode",
          type: "string",
          enum: ["cinematic", "product"],
          default: "cinematic",
          required: true,
          accepted: true,
        },
        {
          name: "frames",
          type: "integer",
          default: 12,
          minimum: 1,
          maximum: 24,
          required: true,
          accepted: true,
        },
        {
          name: "strength",
          type: "number",
          default: 0.35,
          minimum: 0,
          maximum: 1,
          required: true,
          accepted: true,
        },
        {
          name: "source_image",
          request_field: "source",
          type: "string",
          asset: true,
          media_type: "video/*",
          applies: "File upload",
          required: true,
          accepted: true,
        },
      ],
      example_request: {
        method: "POST",
        path: "/gateway/v1/workflows/remix/run",
        content_type: "multipart/form-data",
        form: {
          prompt: "Turn the clip into a teal product shot.",
          mode: "cinematic",
          frames: 12,
          strength: 0.35,
          preserve_detail: true,
        },
        files: { source: "./source.mp4" },
        curl: "curl -X POST http://localhost:5000/gateway/v1/workflows/remix/run -H 'Authorization: Bearer $GATEWAY_API_KEY' -F prompt='Turn the clip into a teal product shot.' -F source=@./source.mp4",
        python:
          "import requests\nfiles = {'source': open('./source.mp4', 'rb')}\nprint(requests.post('http://localhost:5000/gateway/v1/workflows/remix/run', files=files).text)",
        javascript:
          "const form = new FormData();\nform.set('source', file);\nawait fetch('/gateway/v1/workflows/remix/run', { method: 'POST', body: form });",
      },
      openapi: {
        execution: {
          sync: {
            path: "/gateway/v1/workflows/remix/run",
            fields: {
              prompt: { in: "form", field: "prompt" },
              mode: { in: "form", field: "mode" },
              frames: { in: "form", field: "frames" },
              strength: { in: "form", field: "strength" },
              source_image: { in: "file", field: "source" },
            },
          },
        },
      },
    },
    {
      id: "audio.generate.voiceover",
      short_id: "voiceover",
      title: "Voiceover",
      description: "Audio workflow that returns bytes.",
      engine_binding: "comfyui",
      native_route: "/gateway/v1/workflows/voiceover/run",
      inputs: [
        { name: "prompt", type: "string", required: true, accepted: true },
      ],
      example_request: {
        method: "POST",
        path: "/gateway/v1/workflows/voiceover/run",
        content_type: "application/json",
        body: { model: "voiceover", prompt: "Say hello." },
        form: {},
        files: {},
      },
      outputs: [{ kind: "audio", media_type: "audio/mpeg" }],
    },
    {
      id: "video.generate.preview",
      short_id: "preview",
      title: "Video preview",
      description: "Video workflow that returns bytes.",
      engine_binding: "comfyui",
      native_route: "/gateway/v1/workflows/preview/run",
      inputs: [
        { name: "prompt", type: "string", required: true, accepted: true },
      ],
      example_request: {
        method: "POST",
        path: "/gateway/v1/workflows/preview/run",
        content_type: "application/json",
        body: { model: "preview", prompt: "A short teal animation." },
        form: {},
        files: {},
      },
      outputs: [{ kind: "video", media_type: "video/mp4" }],
    },
    {
      id: "video.generate.async",
      short_id: "async",
      title: "Async video",
      description: "Workflow returning a queued job.",
      engine_binding: "comfyui",
      native_route: "/gateway/v1/workflows/async/run",
      inputs: [
        { name: "prompt", type: "string", required: true, accepted: true },
      ],
      example_request: {
        method: "POST",
        path: "/v1/videos",
        content_type: "application/json",
        body: { model: "async", prompt: "A teal launch clip." },
        form: {},
        files: {},
      },
      openapi: {
        execution: {
          async: {
            submit: { path: "/v1/videos" },
            fields: {
              prompt: { in: "body", field: "prompt" },
            },
            poll: { path: "/v1/videos/{video_id}" },
            content: { path: "/v1/videos/{video_id}/content" },
          },
        },
      },
    },
    {
      id: "video.generate.media-job",
      short_id: "media-job",
      title: "Media job",
      inputs: [{ name: "prompt", type: "string", required: true }],
      example_request: {
        path: "/gateway/v1/media/jobs",
        content_type: "application/json",
        body: {
          intent: "video.generate",
          request: { model: "media-job", prompt: "A teal fish." },
        },
      },
      openapi: {
        execution: {
          async: {
            submit: { path: "/gateway/v1/media/jobs" },
            fields: { prompt: { in: "body", field: "prompt" } },
            poll: { path: "/gateway/v1/media/jobs/{job_id}" },
            content: { path: "/gateway/v1/media/jobs/{job_id}/content" },
          },
        },
      },
    },
    {
      id: "audio.generate.no-example",
      short_id: "no-example",
      title: "No example audio",
      description: "Workflow missing runnable example request.",
      engine_binding: "comfyui",
      native_route: null,
      inputs: [
        { name: "prompt", type: "string", required: true, accepted: true },
      ],
    },
  ],
};

async function routeCatalogs(page: Page) {
  await page.route("**/me/api/models/catalog", (route) =>
    route.fulfill({ json: emptyModelsCatalog }),
  );
  await page.route("**/me/api/workflows", (route) =>
    route.fulfill({ json: workflowsCatalog }),
  );
}

async function prepareTryKey(page: Page, workflowId: string) {
  await routeCatalogs(page);
  await page.goto(`/ui/try/?workflow=${encodeURIComponent(workflowId)}`);
  await expect(page.getByRole("tab", { name: "Workflow" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
}

function jsonBody(route: Route) {
  return route.request().postDataJSON() as Record<string, unknown>;
}

async function openAdvancedJson(page: Page) {
  await page.locator("summary").filter({ hasText: "Advanced JSON" }).click();
  await expect(inputsJson(page)).toBeVisible();
}

function inputsJson(page: Page) {
  return page.locator("textarea").last();
}

function fieldName(name: string, required = false) {
  return new RegExp(`^${name}${required ? " Required" : ""}$`, "i");
}

function textbox(page: Page, name: string, required = false) {
  return page.getByRole("textbox", { name: fieldName(name, required) });
}

function spinbutton(page: Page, name: string, required = false) {
  return page.getByRole("spinbutton", { name: fieldName(name, required) });
}

function combobox(page: Page, name: string, required = false) {
  return page.getByRole("combobox", { name: fieldName(name, required) });
}

async function updateAdvancedJson(
  page: Page,
  mutate: (body: Record<string, unknown>) => Record<string, unknown>,
) {
  await openAdvancedJson(page);
  const current = JSON.parse(await inputsJson(page).inputValue()) as Record<
    string,
    unknown
  >;
  await inputsJson(page).fill(JSON.stringify(mutate(current), null, 2));
}

async function expectTechnicalCollapsed(page: Page) {
  const details = page
    .locator("details")
    .filter({ hasText: "Technical details" });
  await expect(details).toBeVisible();
  await expect(details.locator("pre")).toBeHidden();
}

async function openTechnicalDetails(page: Page) {
  await page
    .locator("summary")
    .filter({ hasText: "Technical details" })
    .click();
  await expect(page.getByText("Copy as", { exact: true })).toBeVisible();
}

async function openCopyAs(page: Page) {
  await page.locator("summary").filter({ hasText: "Copy as" }).click();
  await expect(page.locator("pre").filter({ hasText: "curl" })).toBeVisible();
}

// Optional inputs wait under "More options"; open it once.
async function openMoreOptions(page: Page) {
  const more = page.locator("summary").filter({ hasText: "More options" });
  if (!(await more.evaluate((el) => (el.parentElement as HTMLDetailsElement).open))) {
    await more.click();
  }
}

async function fillGuidedImageInputs(page: Page) {
  await textbox(page, "prompt", true).fill("A red poster.");
  await combobox(page, "style", true).click();
  await page.getByRole("option", { name: "flatlay" }).click();
  await spinbutton(page, "steps", true).fill("11");
  await openMoreOptions(page);
  await spinbutton(page, "strength").fill("0.8");
  await spinbutton(page, "width", true).fill("768");
  await spinbutton(page, "height", true).fill("512");
}

// A workflow form starts empty (no prompt is made up for the person), so a
// run needs a description first.
async function fillRequiredPrompt(page: Page, text = "A teal fish.") {
  await page
    .getByRole("textbox", { name: /^(prompt|Describe the \w+) Required$/i })
    .fill(text);
}

async function selectWorkflowModel(page: Page, name: string) {
  await page.getByRole("combobox").first().click();
  await page.getByRole("option", { name }).click();
}

test("workflow deep link edits guided JSON fields and preserves advanced extras", async ({
  page,
}) => {
  await prepareTryKey(page, "image.generate.poster");

  await expect(page.getByRole("combobox").first()).toContainText(
    "Poster image",
  );
  // The catalog's example prompt is an illustration, not the person's input.
  await expect(textbox(page, "prompt", true)).toHaveValue("");
  await expect(combobox(page, "style", true)).toContainText("studio");
  await expect(spinbutton(page, "steps", true)).toHaveValue("8");
  await openMoreOptions(page);
  await expect(spinbutton(page, "strength")).toHaveValue("0.35");
  await expect(spinbutton(page, "width", true)).toHaveValue("1024");
  await expect(spinbutton(page, "height", true)).toHaveValue("1024");
  await expect(inputsJson(page)).toBeHidden();

  await fillGuidedImageInputs(page);
  await updateAdvancedJson(page, (body) => ({
    ...body,
    scheduler: "karras",
    nested: { preserve: true },
    extensions: {
      ...((body.extensions as Record<string, unknown> | undefined) ?? {}),
      "com.azizam.workflow": {
        ...(((body.extensions as Record<string, unknown> | undefined)?.[
          "com.azizam.workflow"
        ] as Record<string, unknown> | undefined) ?? {}),
        seed: 99,
      },
    },
  }));
  await page.locator("summary").filter({ hasText: "Advanced JSON" }).click();
  await expect(inputsJson(page)).toBeHidden();

  let postedBody: Record<string, unknown> | null = null;
  await page.route("**/v1/images/generations", async (route) => {
    expect(route.request().headers()["content-type"]).toContain(
      "application/json",
    );
    postedBody = jsonBody(route);
    await route.fulfill({
      headers: {
        "content-type": "application/json",
        "x-azizam-receipt-id": "receipt_json_workflow",
      },
      json: {
        data: [
          {
            url: "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=",
            media_type: "image/png",
          },
        ],
      },
    });
  });
  await page.route("**/me/api/receipts/receipt_json_workflow", (route) =>
    route.fulfill({ json: { request_id: "receipt_json_workflow" } }),
  );

  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(
    page.getByText("Workflow result", { exact: true }),
  ).toBeVisible();
  await expect(page.getByAltText("Generated result")).toBeVisible();
  await expect(
    page.locator('a[href="/r/receipt_json_workflow"]'),
  ).toBeVisible();
  await expectTechnicalCollapsed(page);
  await openTechnicalDetails(page);
  expect(postedBody).toEqual({
    model: "poster",
    prompt: "A red poster.",
    style: "flatlay",
    size: "768x512",
    strength: 0.8,
    extensions: { "com.azizam.workflow": { seed: 99, steps: 11 } },
    scheduler: "karras",
    nested: { preserve: true },
  });
});

test("workflow multipart guided fields include file, numeric edits, and preserved extras", async ({
  page,
}) => {
  await prepareTryKey(page, "video.edit.remix");

  await page
    .getByRole("textbox", { name: /^prompt Required$/i })
    .fill("Turn this source into a crisp launch clip.");
  await combobox(page, "mode", true).click();
  await page.getByRole("option", { name: "product" }).click();
  await spinbutton(page, "frames", true).fill("18");
  await spinbutton(page, "strength", true).fill("0.65");
  await updateAdvancedJson(page, (body) => ({
    ...body,
    preserve_detail: false,
    colorGrade: "teal",
  }));
  await page.locator("summary").filter({ hasText: "Advanced JSON" }).click();

  let uploadedBody = "";
  await page.route("**/gateway/v1/workflows/remix/run", async (route) => {
    expect(route.request().method()).toBe("POST");
    expect(route.request().headers()["content-type"]).toContain(
      "multipart/form-data",
    );
    uploadedBody =
      (await route.request().postDataBuffer())?.toString("utf8") ?? "";
    await route.fulfill({
      headers: {
        "content-type": "image/png",
        "x-azizam-request-id": "req_workflow_multipart",
      },
      body: Buffer.from(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
        "base64",
      ),
    });
  });

  await page.getByRole("button", { name: "Run", exact: true }).click();
  // The missing video is named on its own picker; nothing was sent.
  const source = page.getByRole("group", { name: /Source image/ });
  await expect(source.getByRole("alert")).toHaveText("Choose a video first");
  expect(uploadedBody).toBe("");
  // One picker for the one file: no second upload control, no made-up path.
  await expect(page.locator('input[type="file"]')).toHaveCount(1);
  await expect(page.getByText("./source.mp4")).toHaveCount(0);

  await page.setInputFiles('input[type="file"]', {
    name: "clip.mp4",
    mimeType: "video/mp4",
    buffer: Buffer.from("fake mp4"),
  });
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(
    page.getByText("Workflow result", { exact: true }),
  ).toBeVisible();
  await expect(page.getByAltText("Generated result")).toBeVisible();
  await expectTechnicalCollapsed(page);
  await openTechnicalDetails(page);
  await expect(page.getByText("Copy as", { exact: true })).toBeVisible();
  await openCopyAs(page);
  await expect(
    page.getByText("/gateway/v1/workflows/remix/run", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByText("/openai/v1/gateway", { exact: false }),
  ).toHaveCount(0);

  expect(uploadedBody).toContain('name="source"; filename="clip.mp4"');
  expect(uploadedBody).toContain('name="prompt"');
  expect(uploadedBody).toContain("Turn this source into a crisp launch clip.");
  expect(uploadedBody).toContain('name="mode"');
  expect(uploadedBody).toContain("product");
  expect(uploadedBody).toContain('name="frames"');
  expect(uploadedBody).toContain("18");
  expect(uploadedBody).toContain('name="strength"');
  expect(uploadedBody).toContain("0.65");
  expect(uploadedBody).toContain('name="preserve_detail"');
  expect(uploadedBody).toContain("false");
  expect(uploadedBody).toContain('name="colorGrade"');
  expect(uploadedBody).toContain("teal");
  await expect(page.locator("pre").filter({ hasText: "curl" })).toContainText(
    "clip.mp4",
  );
  await expect(page.locator("pre").filter({ hasText: "curl" })).toContainText(
    "Turn this source into a crisp launch clip.",
  );
  await expect(page.locator("pre").filter({ hasText: "curl" })).toContainText(
    "colorGrade=teal",
  );
});

test("workflow guided validation blocks missing, range, enum, and invalid JSON inputs", async ({
  page,
}) => {
  await prepareTryKey(page, "image.generate.poster");

  await textbox(page, "prompt", true).fill("");
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Write a prompt first")).toBeVisible();
  await expect(textbox(page, "prompt", true)).toBeFocused();

  await textbox(page, "prompt", true).fill("A valid poster.");
  await spinbutton(page, "steps", true).fill("13");
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Steps must be at most 12.")).toBeVisible();

  await spinbutton(page, "steps", true).fill("8");
  await spinbutton(page, "width", true).fill("63");
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Width must be at least 64.")).toBeVisible();

  await spinbutton(page, "width", true).fill("1024");
  await openAdvancedJson(page);
  await inputsJson(page).fill("[1,2,3]");
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(
    page.getByText("Workflow inputs must be a JSON object.").last(),
  ).toBeVisible();
});

test("workflow results preview JSON images, image bytes, audio bytes, and video bytes", async ({
  page,
}) => {
  await prepareTryKey(page, "image.generate.poster");
  await fillGuidedImageInputs(page);
  await page.route("**/v1/images/generations", (route) =>
    route.fulfill({
      json: {
        data: [
          {
            url: "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=",
            media_type: "image/png",
          },
        ],
      },
    }),
  );
  await fillRequiredPrompt(page);
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByAltText("Generated result")).toBeVisible();
  await page.unroute("**/v1/images/generations");

  await selectWorkflowModel(page, "Remix video");
  await page.setInputFiles('input[type="file"]', {
    name: "clip.mp4",
    mimeType: "video/mp4",
    buffer: Buffer.from("fake mp4"),
  });
  await page.route("**/gateway/v1/workflows/remix/run", (route) =>
    route.fulfill({
      headers: { "content-type": "image/png" },
      body: Buffer.from(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
        "base64",
      ),
    }),
  );
  await fillRequiredPrompt(page);
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByAltText("Generated result")).toBeVisible();

  await selectWorkflowModel(page, "Voiceover");
  await page.route("**/gateway/v1/workflows/voiceover/run", (route) =>
    route.fulfill({
      headers: { "content-type": "audio/mpeg" },
      body: Buffer.from("fake audio bytes"),
    }),
  );
  await fillRequiredPrompt(page);
  await page.getByRole("button", { name: "Run", exact: true }).click();
  // Fake bytes cannot play; the result and its download still appear.
  await expect(
    page.getByRole("link", { name: "Download audio" }),
  ).toHaveAttribute("href", /^blob:/);

  await selectWorkflowModel(page, "Video preview");
  await page.route("**/gateway/v1/workflows/preview/run", (route) =>
    route.fulfill({
      headers: { "content-type": "video/mp4" },
      body: Buffer.from("fake video bytes"),
    }),
  );
  await fillRequiredPrompt(page);
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(
    page.getByRole("link", { name: "Download video" }),
  ).toHaveAttribute("href", /^blob:/);
});

test("workflow async video jobs can complete with byte outputs or fail from Check job", async ({
  page,
}) => {
  await prepareTryKey(page, "video.generate.async");

  await page.route("**/v1/videos", (route) =>
    route.fulfill({
      json: {
        id: "job_async_try",
        object: "video",
        status: "queued",
        request_id: "receipt_async_try",
      },
    }),
  );
  await page.route("**/v1/videos/job_async_try", (route) =>
    route.fulfill({
      json: {
        id: "job_async_try",
        object: "video",
        status: "completed",
      },
    }),
  );
  await page.route("**/v1/videos/job_async_try/content", (route) =>
    route.fulfill({
      headers: { "content-type": "video/mp4" },
      body: Buffer.from("fake completed video bytes"),
    }),
  );
  await page.route("**/me/api/receipts/receipt_async_try", (route) =>
    route.fulfill({ json: { request_id: "receipt_async_try" } }),
  );

  await fillRequiredPrompt(page);
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Waiting to start", { exact: true })).toBeVisible();
  await expectTechnicalCollapsed(page);
  await openTechnicalDetails(page);
  await expect(page.getByText('"status": "queued"')).toBeVisible();
  await page.getByRole("button", { name: "Check again" }).click();
  await expect(
    page.getByRole("link", { name: "Download video" }),
  ).toHaveAttribute("href", /^blob:/);

  await page.route("**/v1/videos", (route) =>
    route.fulfill({
      json: {
        id: "job_async_failed",
        object: "video",
        status: "queued",
      },
    }),
  );
  await page.route("**/v1/videos/job_async_failed", (route) =>
    route.fulfill({
      json: {
        id: "job_async_failed",
        object: "video",
        status: "failed",
        error: { message: "Renderer ran out of memory." },
      },
    }),
  );
  await fillRequiredPrompt(page);
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Waiting to start", { exact: true })).toBeVisible();
  await page.route("**/v1/videos/job_async_try", (route) =>
    route.fulfill({
      json: {
        id: "job_async_try",
        object: "video",
        status: "failed",
        error: { message: "Renderer ran out of memory." },
      },
    }),
  );
  await page.getByRole("button", { name: "Check again" }).click();
  await expect(
    page.getByText("Renderer ran out of memory.", { exact: true }),
  ).toBeVisible();
});

test("workflow denied receipt does not create a link, while verified receipt links /r/:id", async ({
  page,
}) => {
  await prepareTryKey(page, "image.generate.poster");
  await fillGuidedImageInputs(page);

  await page.route("**/v1/images/generations", (route) =>
    route.fulfill({
      json: {
        data: [
          {
            asset_id: "img_fixture",
            url: "data:image/png;base64,iVBORw0KGgo=",
            media_type: "image/png",
          },
        ],
      },
    }),
  );
  await page.route("**/me/api/media/img_fixture", (route) =>
    route.fulfill({
      json: { asset: { id: "img_fixture", request_id: "receipt_from_asset" } },
    }),
  );
  await page.route("**/me/api/receipts/receipt_from_asset", (route) =>
    route.fulfill({ json: { request_id: "receipt_from_asset" } }),
  );
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.locator('a[href="/r/receipt_from_asset"]')).toBeVisible();

  await page.unroute("**/v1/images/generations");
  await page.route("**/v1/images/generations", (route) =>
    route.fulfill({
      headers: { "x-azizam-receipt-id": "receipt denied/value" },
      json: { data: [{ url: "data:image/png;base64,iVBORw0KGgo=" }] },
    }),
  );
  await page.route("**/me/api/receipts/receipt%20denied%2Fvalue", (route) =>
    route.fulfill({ status: 404, json: { error: "not found" } }),
  );
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("No receipt was returned.")).toBeVisible();
  await expect(
    page.locator('a[href="/r/receipt%20denied%2Fvalue"]'),
  ).toHaveCount(0);

  await page.unroute("**/v1/images/generations");
  await page.route("**/v1/images/generations", (route) =>
    route.fulfill({
      headers: { "x-azizam-receipt-id": "receipt verified/value" },
      json: { data: [{ url: "data:image/png;base64,iVBORw0KGgo=" }] },
    }),
  );
  await page.route("**/me/api/receipts/receipt%20verified%2Fvalue", (route) =>
    route.fulfill({ json: { request_id: "receipt verified/value" } }),
  );
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(
    page.locator('a[href="/r/receipt%20verified%2Fvalue"]'),
  ).toHaveAttribute("href", "/r/receipt%20verified%2Fvalue");
});

test("workflow deep link reports missing runnable examples without guessing", async ({
  page,
}) => {
  await prepareTryKey(page, "audio.generate.no-example");

  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(
    page.getByText(
      "This workflow does not include a runnable example request yet.",
    ),
  ).toBeVisible();
});

test("workflow JSON media renders signed audio and video outputs", async ({
  page,
}) => {
  await prepareTryKey(page, "audio.generate.voiceover");
  await page.route("**/gateway/v1/workflows/voiceover/run", (route) =>
    route.fulfill({
      json: {
        assets: [
          {
            kind: "audio",
            signed_url: "https://media.example.test/audio?sig=fixture",
            media_type: "audio/mpeg",
          },
          {
            kind: "video",
            signed_url: "https://media.example.test/video?sig=fixture",
            media_type: "video/mp4",
          },
        ],
      },
    }),
  );
  // The signed host is not reachable here: each file says it could not
  // load and offers Retry, and its download keeps the signed address.
  await page.route("https://media.example.test/**", (route) => route.abort());
  await fillRequiredPrompt(page);
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("The audio couldn’t be loaded.")).toBeVisible();
  await expect(page.getByText("The video couldn’t be loaded.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry" })).toHaveCount(2);
  await expect(
    page.getByRole("link", { name: "Download video 2" }),
  ).toHaveAttribute("href", "https://media.example.test/video?sig=fixture");
  await expect(
    page.getByRole("link", { name: "Download audio 1" }),
  ).toHaveAttribute("href", "https://media.example.test/audio?sig=fixture");
  await expectTechnicalCollapsed(page);
});

test("workflow media jobs use declared routes and load signed output after retry", async ({
  page,
}) => {
  await prepareTryKey(page, "video.generate.media-job");
  let body: unknown;
  await page.route("**/gateway/v1/media/jobs", (route) => {
    body = route.request().postDataJSON();
    return route.fulfill({
      json: { object: "media.job", job_id: "signed_job", status: "queued" },
    });
  });
  await page.route("**/gateway/v1/media/jobs/signed_job", (route) =>
    route.fulfill({
      status: 503,
      json: { error: { message: "Job temporarily unavailable." } },
    }),
  );
  await fillRequiredPrompt(page);
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Waiting to start", { exact: true })).toBeVisible();
  expect(body).toEqual({
    intent: "video.generate",
    request: { model: "media-job", prompt: "A teal fish." },
  });
  await page.getByRole("button", { name: "Check again", exact: true }).click();
  await expect(page.getByText("Job temporarily unavailable.")).toBeVisible();
  await page.unroute("**/gateway/v1/media/jobs/signed_job");
  await page.route("**/gateway/v1/media/jobs/signed_job", (route) =>
    route.fulfill({
      json: { object: "media.job", job_id: "signed_job", status: "succeeded" },
    }),
  );
  await page.route("**/gateway/v1/media/jobs/signed_job/content", (route) =>
    route.fulfill({
      json: {
        assets: [
          {
            kind: "video",
            signed_url: "https://media.example.test/completed?sig=fixture",
            media_type: "video/mp4",
          },
        ],
      },
    }),
  );
  await page.route("https://media.example.test/**", (route) => route.abort());
  await page.getByRole("button", { name: "Try again", exact: true }).click();
  await expect(page.getByText("The video couldn’t be loaded.")).toBeVisible();
  await expect(page.getByText("Ready", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Download video", exact: true }),
  ).toHaveAttribute("href", "https://media.example.test/completed?sig=fixture");
  await expect(page.getByText("No receipt was returned.")).toBeVisible();
});

// Composed workflows: Try names the engine a workflow waits on, the same way
// the Workflows page does (web/src/lib/workflow-availability.ts).
const composedEvidence = { source: "dependencies", kind: "required" };
const composedBase = {
  description: "A workflow the gateway composes from engines.",
  engine_binding: "gateway",
  native_route: null,
  inputs: [{ name: "prompt", type: "string", required: true, accepted: true }],
};
const composedCatalog = {
  schema_version: 1,
  object: "workflow.catalog",
  discovery_url: "/gateway/v1/workflows",
  workflows: [
    {
      ...composedBase,
      id: "image.edit.image-identity",
      title: "Identity Scene",
      availability: { status: "ready", reason_code: "ready", available: true,
                      reason: "Every engine it needs is ready.", engine: "comfyui", evidence: composedEvidence },
    },
    {
      ...composedBase,
      id: "image.analyze.face-likeness",
      title: "Face Likeness Check",
      availability: { status: "idle", reason_code: "idle", available: true,
                      reason: "Face service starts when you make a request.", engine: "faces", evidence: composedEvidence },
    },
    {
      ...composedBase,
      id: "image.generate.flux2-klein-4b-seamless-tile",
      title: "Seamless Tile",
      availability: { status: "unavailable", reason_code: "unavailable", available: false,
                      reason: "ComfyUI is down.", engine: "comfyui", evidence: composedEvidence },
    },
  ],
};

async function openComposed(page: Page, workflowId: string, member = false) {
  if (member)
    await page.route("**/me/api/session", (route) =>
      route.fulfill({
        json: {
          authenticated: true,
          csrf_token: "fixture",
          apps: [],
          user: { id: "member-try", name: "Member", role: "member", email: "member@example.test" },
        },
      }),
    );
  await page.route("**/me/api/models/catalog", (route) => route.fulfill({ json: emptyModelsCatalog }));
  await page.route("**/me/api/workflows", (route) => route.fulfill({ json: composedCatalog }));
  await page.goto(`/ui/try/?workflow=${encodeURIComponent(workflowId)}`);
  await expect(page.getByRole("tab", { name: "Workflow" })).toHaveAttribute("aria-selected", "true");
}

test("Try runs a ready composed workflow without an availability note", async ({ page }) => {
  await openComposed(page, "image.edit.image-identity");
  await expect(page.getByRole("combobox").first()).toHaveText(/^Identity Scene$/);
  await expect(page.getByTestId("workflow-availability-note")).toHaveCount(0);
});

test("Try reads a stopped on-demand engine as Ready to start and lets it run", async ({ page }) => {
  await openComposed(page, "image.analyze.face-likeness");
  await expect(page.getByRole("combobox").first()).toContainText("Face Likeness Check · Ready to start");
  await expect(page.getByTestId("workflow-availability-note")).toHaveText(
    "Face service starts when you make a request.",
  );
  await expect(page.getByText("Unavailable")).toHaveCount(0);
});

for (const viewer of ["owner", "member"] as const) {
  test(`Try explains a down engine by name to the ${viewer}`, async ({ page }) => {
    await openComposed(page, "image.generate.flux2-klein-4b-seamless-tile", viewer === "member");
    await expect(page.getByRole("combobox").first()).toContainText("Seamless Tile · ComfyUI is down.");
    await expect(page.getByRole("button", { name: "Run", exact: true })).toBeDisabled();
    const note = page.getByTestId("workflow-availability-note");
    await expect(note).toContainText("ComfyUI is down.");
    if (viewer === "owner") {
      await expect(note.getByRole("link", { name: "Open Engines" })).toHaveAttribute(
        "href", /\/engines\/?\?service=comfyui$/,
      );
    } else {
      await expect(note.getByRole("link")).toHaveCount(0);
    }
  });
}
