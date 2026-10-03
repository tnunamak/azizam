import { expect, test, type Page } from "@playwright/test";

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
      id: "video.edit.remix",
      short_id: "remix",
      title: "Remix video",
      description: "Upload-driven video edit workflow.",
      engine_binding: "comfyui",
      native_route: "/gateway/v1/workflows/remix/run",
      inputs: [
        { name: "prompt", type: "string", required: true, accepted: true },
        { name: "strength", type: "number", default: 0.35, accepted: true },
        {
          name: "source_image",
          request_field: "source",
          type: "asset",
          required: true,
          accepted: true,
        },
      ],
      example_request: {
        method: "POST",
        path: "/gateway/v1/workflows/remix/run",
        content_type: "multipart/form-data",
        form: { prompt: "Turn the clip into a teal product shot.", strength: 0.35 },
        files: { source: "./source.mp4" },
        curl:
          "curl -X POST http://localhost:5000/gateway/v1/workflows/remix/run -H 'Authorization: Bearer $GATEWAY_API_KEY' -F prompt='Turn the clip into a teal product shot.' -F strength='0.35' -F source=@./source.mp4",
        python:
          "import requests\nfiles = {'source': open('./source.mp4', 'rb')}\ndata = {'prompt': 'Turn the clip into a teal product shot.', 'strength': '0.35'}\nprint(requests.post('http://localhost:5000/gateway/v1/workflows/remix/run', data=data, files=files).text)",
        javascript:
          "const form = new FormData();\nform.set('prompt', 'Turn the clip into a teal product shot.');\nform.set('strength', '0.35');\nform.set('source', file);\nawait fetch('/gateway/v1/workflows/remix/run', { method: 'POST', body: form });",
      },
    },
    {
      id: "image.generate.poster",
      short_id: "poster",
      title: "Poster image",
      description: "Image workflow served through the OpenAI image route.",
      engine_binding: "comfyui",
      native_route: "/v1/images/generations",
      inputs: [{ name: "prompt", type: "string", required: true, accepted: true }],
      example_request: {
        method: "POST",
        path: "/v1/images/generations",
        content_type: "application/json",
        body: { model: "poster", prompt: "A teal poster." },
        form: {},
        files: {},
        curl:
          "curl http://localhost:5000/v1/images/generations -H 'Authorization: Bearer $GATEWAY_API_KEY' -H 'Content-Type: application/json' -d '{\"model\":\"poster\",\"prompt\":\"A teal poster.\"}'",
      },
    },
    {
      id: "audio.generate.no-example",
      short_id: "no-example",
      title: "No example audio",
      description: "Workflow missing runnable example request.",
      engine_binding: "comfyui",
      native_route: null,
      inputs: [{ name: "prompt", type: "string", required: true, accepted: true }],
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
  await page.getByRole("button", { name: "Prepare Try key" }).first().click();
  await expect(page.getByText("Try app key", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "I’ve saved it" }).click();
  await expect(page.getByText("Session key ready")).toBeVisible();
  await expect(page.getByText("Catalog loaded")).toBeVisible();
}

test("workflow deep link selects the workflow and submits multipart to its advertised run route", async ({
  page,
}) => {
  await prepareTryKey(page, "video.edit.remix");

  await expect(page.getByRole("tab", { name: "Workflow" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page.getByLabel("Model", { exact: true })).toContainText(
    "Remix video",
  );
  await expect(page.getByText("Typed inputs", { exact: true })).toBeVisible();
  await expect(page.getByRole("cell", { name: "strength" })).toBeVisible();
  await expect(page.getByLabel("Inputs JSON")).toHaveValue(
    /Turn the clip into a teal product shot\./,
  );
  await expect(page.getByText("./source.mp4", { exact: true })).toBeVisible();

  let uploadedBody = "";
  await page.route("**/gateway/v1/workflows/remix/run", async (route) => {
    expect(route.request().method()).toBe("POST");
    expect(route.request().headers()["content-type"]).toContain(
      "multipart/form-data",
    );
    const requestBody = await route.request().postDataBuffer();
    uploadedBody = requestBody?.toString("utf8") ?? "";
    await route.fulfill({
      headers: {
        "content-type": "image/png",
        "x-ai-gateway-request-id": "req_workflow_try",
      },
      body: Buffer.from(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
        "base64",
      ),
    });
  });

  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(
    page.getByText("Choose a file for workflow input source."),
  ).toBeVisible();

  await page.setInputFiles('input[type="file"]', {
    name: "clip.mp4",
    mimeType: "video/mp4",
    buffer: Buffer.from("fake mp4"),
  });
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Workflow result", { exact: true })).toBeVisible();
  await expect(page.getByAltText("Generated result")).toBeVisible();
  await expect(page.getByText("Example request", { exact: true })).toBeVisible();
  await expect(
    page.getByText("/gateway/v1/workflows/remix/run", { exact: false }),
  ).toBeVisible();
  await expect(page.getByText("/openai/v1/gateway", { exact: false })).toHaveCount(
    0,
  );
  expect(uploadedBody).toContain('name="source"; filename="clip.mp4"');
  expect(uploadedBody).toContain('name="prompt"');
  expect(uploadedBody).toContain("Turn the clip into a teal product shot.");
  expect(uploadedBody).toContain('name="strength"');
  expect(uploadedBody).toContain("0.35");
});

test("workflow deep link uses the canonical image example request route", async ({
  page,
}) => {
  await prepareTryKey(page, "image.generate.poster");

  await expect(page.getByRole("tab", { name: "Workflow" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page.getByLabel("Model", { exact: true })).toContainText(
    "Poster image",
  );
  await expect(page.getByLabel("Inputs JSON")).toHaveValue(/A teal poster\./);

  let postedBody: Record<string, unknown> | null = null;
  await page.route("**/v1/images/generations", async (route) => {
    expect(route.request().headers()["content-type"]).toContain(
      "application/json",
    );
    postedBody = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({
      status: 400,
      json: { error: { message: "Image workflow unavailable." } },
    });
  });

  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Image workflow unavailable.")).toBeVisible();
  expect(postedBody).toEqual({ model: "poster", prompt: "A teal poster." });
});

test("workflow deep link reports missing runnable examples without guessing", async ({
  page,
}) => {
  await prepareTryKey(page, "audio.generate.no-example");

  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(
    page.getByText("This workflow does not include a runnable example request yet."),
  ).toBeVisible();
});
