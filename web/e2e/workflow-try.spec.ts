import { readFile } from "node:fs/promises";

import { type Page, type Route } from "@playwright/test";
import { expect, test } from "./fixtures/isolation";
import { fulfilJson, fulfilBytes } from "./fixtures/intercept";


// Try mints one key per sign-in and revokes the session's older Try keys
// (gateway/user_runtime.py ensure_try_session_key). Cases that open Try share the
// lane's sign-in, so the cases of this file stay in one worker, one after another.
test.describe.configure({ mode: "default" });

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
    fulfilJson(route, emptyModelsCatalog),
  );
  await page.route("**/me/api/workflows", (route) =>
    fulfilJson(route, workflowsCatalog),
  );
}

async function prepareTryKey(page: Page, workflowId: string) {
  await routeCatalogs(page);
  await page.goto(`/try/?workflow=${encodeURIComponent(workflowId)}`);
  await expect(page.getByRole("tab", { name: "Workflow" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
}

async function fillRequiredPrompt(page: Page, text = "A teal fish.") {
  await page
    .getByRole("textbox", { name: /^(prompt|Describe the \w+) Required$/i })
    .fill(text);
}

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
      fulfilJson(route, {
          authenticated: true,
          csrf_token: "fixture",
          apps: [],
          user: { id: "member-try", name: "Member", role: "member", email: "member@example.test" },
        }),
    );
  await page.route("**/me/api/models/catalog", (route) => fulfilJson(route, emptyModelsCatalog));
  await page.route("**/me/api/workflows", (route) => fulfilJson(route, composedCatalog));
  await page.goto(`/try/?workflow=${encodeURIComponent(workflowId)}`);
  await expect(page.getByRole("tab", { name: "Workflow" })).toHaveAttribute("aria-selected", "true");
}

test("successive video runs poll their own jobs and never reuse old output", async ({ page }) => {
  const clip = await readFile("e2e/fixtures/preview-test.mp4");
  const firstId = "job_async_try";
  const secondId = "job_async_failed";
  const firstPrompt = "First recorded video.";
  const secondPrompt = "Second video must use a new job.";
  const failure = "The second renderer ran out of memory.";
  let submissions = 0;
  const seen: string[] = [];
  const posted: unknown[] = [];

  await page.route("**/v1/videos**", (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    seen.push(request.method() + " " + path);

    if (request.method() === "POST" && path === "/v1/videos") {
      submissions += 1;
      posted.push(request.postDataJSON());
      return fulfilJson(route, {
          id: submissions === 1 ? firstId : secondId,
          object: "video",
          status: "queued",
        }, { status: 202 });
    }
    if (request.method() === "GET" && path === "/v1/videos/" + firstId) {
      return fulfilJson(route, submissions === 1
          ? { id: firstId, object: "video", status: "completed" }
          : {
              id: firstId,
              object: "video",
              status: "failed",
              error: { message: "WRONG: polled the previous job" },
            });
    }
    if (request.method() === "GET" && path === "/v1/videos/" + firstId + "/content") {
      return fulfilBytes(route, { contentType: "video/mp4", body: clip });
    }
    if (request.method() === "GET" && path === "/v1/videos/" + secondId) {
      return fulfilJson(route, {
          id: secondId,
          object: "video",
          status: "failed",
          error: { message: failure },
        });
    }
    return fulfilJson(route, { error: { message: "Unexpected video route: " + path } }, { status: 404 });
  });

  await prepareTryKey(page, "video.generate.async");
  await fillRequiredPrompt(page, firstPrompt);
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Waiting to start", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Check again", exact: true }).click();

  await expect(page.getByRole("link", { name: "Download video", exact: true }))
    .toHaveAttribute("href", /^blob:/);
  const video = page.locator("video");
  await expect(video).toHaveCount(1);
  await expect.poll(() => video.evaluate((element: HTMLVideoElement) =>
    Number.isFinite(element.duration) && element.duration > 0 && element.readyState >= 1,
  )).toBe(true);
  await expect.poll(() => video.evaluate((element: HTMLVideoElement) => element.error)).toBeNull();

  await fillRequiredPrompt(page, secondPrompt);
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Waiting to start", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Download video", exact: true }))
    .toHaveCount(0);
  await page.getByRole("button", { name: "Check again", exact: true }).click();
  await expect(page.getByText(failure, { exact: true })).toBeVisible();
  await expect(page.getByText("WRONG: polled the previous job", { exact: true }))
    .toHaveCount(0);
  await expect(video).toHaveCount(0);

  expect(submissions).toBe(2);
  expect(posted).toEqual([
    expect.objectContaining({ model: "async", prompt: firstPrompt }),
    expect.objectContaining({ model: "async", prompt: secondPrompt }),
  ]);
  expect(seen).toEqual([
    "POST /v1/videos",
    "GET /v1/videos/" + firstId,
    "GET /v1/videos/" + firstId + "/content",
    "POST /v1/videos",
    "GET /v1/videos/" + secondId,
  ]);
});

// A 1x1 PNG and the overall verdict of a passing likeness check, as the real
// handlers return them (scripts/ui/identity_contract.py).
const PIXEL_PNG =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=";
const PASSING_OVERALL = {
  status: "pass",
  all_meet_threshold: true,
  min_score: 1,
  mean_score: 1,
  needs_human_check: false,
  extra_faces: 0,
  facing_each_other: null,
};

for (const scenario of [
  {
    id: "image.edit.image-identity",
    route: "/gateway/v1/images/identity",
    // The scene returns its image under `data` and its likeness report under `likeness`.
    response: (proof: string) => ({
      created: 1,
      data: [{ asset_id: "img_1", asset_url: "/assets/img_1", media_type: "image/png", b64_json: PIXEL_PNG }],
      likeness: { object: "likeness.check", pooling: proof, overall: PASSING_OVERALL },
    }),
    summary: "1 output returned.",
    title: "Identity Scene",
    note: null,
  },
  {
    id: "image.analyze.face-likeness",
    route: "/gateway/v1/faces/likeness",
    response: (proof: string) => ({ object: "likeness.check", pooling: proof, overall: PASSING_OVERALL }),
    summary: "Workflow returned a response. Open Technical details to inspect it.",
    title: "Face Likeness Check",
    note: "Face service starts when you make a request.",
  },
] as const) {
  test("Try submits " + scenario.title + " and renders its response", async ({ page }) => {
    // The routes the gateway really serves for these two workflows (the catalog's
    // native_route); /gateway/v1/workflows/{id}/run only runs video-edit workflows.
    const runPath = scenario.route;
    const prompt = "A deterministic composed-workflow request.";
    const proof = "response-for-" + scenario.id;
    const posts: Array<{ method: string; body: unknown }> = [];

    await page.route("**/me/api/models/catalog", (route) =>
      fulfilJson(route, emptyModelsCatalog),
    );
    await page.route("**/me/api/workflows", (route) =>
      fulfilJson(route, {
          ...composedCatalog,
          workflows: composedCatalog.workflows.map((workflow) => ({
            ...workflow,
            example_request: {
              method: "POST",
              path: scenario.route,
              content_type: "application/json",
              body: { model: workflow.id, prompt: "" },
            },
          })),
        }),
    );
    await page.route("**" + runPath, (route) => {
      posts.push({
        method: route.request().method(),
        body: route.request().postDataJSON(),
      });
      return fulfilJson(route, scenario.response(proof));
    });

    await page.goto("/try/?workflow=" + encodeURIComponent(scenario.id));
    await expect(page.getByRole("tab", { name: "Workflow", exact: true }))
      .toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("combobox").first()).toContainText(scenario.title);

    const note = page.getByTestId("workflow-availability-note");
    if (scenario.note === null) {
      await expect(note).toHaveCount(0);
    } else {
      await expect(note).toHaveText(scenario.note);
    }

    await fillRequiredPrompt(page, prompt);
    const run = page.getByRole("button", { name: "Run", exact: true });
    await expect(run).toBeEnabled();
    const submitted = page.waitForRequest((request) =>
      new URL(request.url()).pathname === runPath &&
      request.method() === "POST",
    );
    await run.click();
    const request = await submitted;
    expect(request.postDataJSON()).toMatchObject({ model: scenario.id, prompt });

    await expect(page.getByText("Workflow result", { exact: true })).toBeVisible();
    await expect(
      page.getByText(scenario.summary, { exact: true }),
    ).toBeVisible();
    const technical = page.locator("details").filter({ hasText: "Technical details" });
    await expect(technical.locator("pre")).toBeHidden();
    await technical.locator("summary").click();
    await expect(technical.locator("pre")).toContainText(JSON.stringify(proof));
    expect(posts).toHaveLength(1);
    expect(posts[0]).toMatchObject({
      method: "POST",
      body: { model: scenario.id, prompt },
    });
  });
}

// Browser representative: jsdom cannot build the browser's own multipart encoding
// (boundary header, file part), so the wire contract is read from the real request.
test("workflow multipart run sends a browser-encoded file part without a JSON content type", async ({
  page,
}) => {
  await prepareTryKey(page, "video.edit.remix");
  await page
    .getByRole("textbox", { name: /^prompt Required$/i })
    .fill("Turn this source into a crisp launch clip.");

  let contentType = "";
  let method = "";
  let uploadedBody = "";
  await page.route("**/gateway/v1/workflows/remix/run", async (route) => {
    const request = route.request();
    method = request.method();
    contentType = request.headers()["content-type"] ?? "";
    uploadedBody = (await request.postDataBuffer())?.toString("utf8") ?? "";
    await fulfilBytes(route, {
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

  await page.setInputFiles('input[type="file"]', {
    name: "clip.mp4",
    mimeType: "video/mp4",
    buffer: Buffer.from("fake mp4"),
  });
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Workflow result", { exact: true })).toBeVisible();

  expect(method).toBe("POST");
  expect(contentType).toMatch(/^multipart\/form-data; boundary=/);
  expect(uploadedBody).toContain('name="source"; filename="clip.mp4"');
  expect(uploadedBody).toContain("Turn this source into a crisp launch clip.");
});
