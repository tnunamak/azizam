import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import TryPage from "@/app/try/page";
import { ViewerContext, type Viewer } from "@/components/shell/session";

// Running a workflow from Try: guided fields, validation, what each kind of
// answer looks like, jobs, receipts and availability. These cases used to run
// in a browser (e2e/workflow-try.spec.ts); the request goes through a stubbed
// fetch here, with the catalog rows and answers copied from that spec.

const account = vi.hoisted(() => ({
  session: vi.fn(),
  tryKey: vi.fn(),
  revokeKey: vi.fn(),
  promptMemory: vi.fn(),
  params: new URLSearchParams(),
}));
vi.mock("next/navigation", () => ({ useSearchParams: () => account.params }));
vi.mock("@/lib/account-api", () => ({ accountApi: account }));

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
        { name: "style", type: "string", enum: ["studio", "flatlay"], default: "studio", required: true, accepted: true },
        { name: "steps", type: "integer", default: 8, minimum: 1, maximum: 12, required: true, accepted: true },
        { name: "strength", type: "number", default: 0.35, minimum: 0, maximum: 1, required: false, accepted: true },
        { name: "width", type: "integer", default: 1024, minimum: 64, maximum: 2048, required: true, accepted: true },
        { name: "height", type: "integer", default: 1024, minimum: 64, maximum: 2048, required: true, accepted: true },
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
        curl: "curl http://localhost:5000/v1/images/generations",
      },
      openapi: {
        execution: {
          sync: {
            path: "/v1/images/generations",
            fields: {
              prompt: { in: "body", field: "prompt" },
              style: { in: "body", field: "style" },
              width: { in: "body", field: "size", combine: ["width", "height"] },
              height: { in: "body", field: "size", combine: ["width", "height"] },
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
        { name: "mode", type: "string", enum: ["cinematic", "product"], default: "cinematic", required: true, accepted: true },
        { name: "frames", type: "integer", default: 12, minimum: 1, maximum: 24, required: true, accepted: true },
        { name: "strength", type: "number", default: 0.35, minimum: 0, maximum: 1, required: true, accepted: true },
        { name: "source_image", request_field: "source", type: "string", asset: true, media_type: "video/*", applies: "File upload", required: true, accepted: true },
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
        curl: "curl -X POST http://localhost:5000/gateway/v1/workflows/remix/run -F prompt='Turn the clip into a teal product shot.' -F source=@./source.mp4",
        python: "import requests",
        javascript: "const form = new FormData();",
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
      inputs: [{ name: "prompt", type: "string", required: true, accepted: true }],
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
      inputs: [{ name: "prompt", type: "string", required: true, accepted: true }],
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
      inputs: [{ name: "prompt", type: "string", required: true, accepted: true }],
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
            fields: { prompt: { in: "body", field: "prompt" } },
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
        body: { intent: "video.generate", request: { model: "media-job", prompt: "A teal fish." } },
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
      inputs: [{ name: "prompt", type: "string", required: true, accepted: true }],
    },
  ],
};

// Composed workflows: Try names the engine a workflow waits on, the same way
// the Workflows page does (web/src/lib/workflow-availability.ts).
const composedEvidence = { source: "dependencies", kind: "required" };
const composedBase = {
  description: "A workflow the gateway composes from engines.",
  engine_binding: "gateway",
  native_route: null,
  inputs: [{ name: "prompt", type: "string", required: true, accepted: true }],
  // Added to the browser fixture so a blocked run is stopped by the availability guard, not by a missing route.
  example_request: { method: "POST", path: "/gateway/v1/workflows/composed/run", content_type: "application/json", body: { prompt: "x" } },
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
      availability: { status: "ready", reason_code: "ready", available: true, reason: "Every engine it needs is ready.", engine: "comfyui", evidence: composedEvidence },
    },
    {
      ...composedBase,
      id: "image.analyze.face-likeness",
      title: "Face Likeness Check",
      availability: { status: "idle", reason_code: "idle", available: true, reason: "Face service starts when you make a request.", engine: "faces", evidence: composedEvidence },
    },
    {
      ...composedBase,
      id: "image.generate.flux2-klein-4b-seamless-tile",
      title: "Seamless Tile",
      availability: { status: "unavailable", reason_code: "unavailable", available: false, reason: "ComfyUI is down.", engine: "comfyui", evidence: composedEvidence },
    },
  ],
};

const PIXEL_PNG =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==";
const DATA_URL_PNG =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=";

const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
const bytes = (text: string, type: string, headers: Record<string, string> = {}) =>
  new Response(new Blob([text], { type }), {
    headers: { "content-type": type, ...headers },
  });

const fetchMock = vi.fn();
type Answer = (url: string, init?: RequestInit) => Response | undefined;
let answer: Answer = () => undefined;
let catalog: unknown = workflowsCatalog;

const member: Viewer = {
  authenticated: true,
  name: "Robin",
  email: "robin@example.test",
  role: "member",
  admin: false,
};
const owner: Viewer = { ...member, name: "Sam", role: "owner", admin: true };

function posts() {
  return fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");
}
function postedJson(index = -1) {
  return JSON.parse(posts().at(index)![1].body as string) as Record<string, unknown>;
}

beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, "hasPointerCapture", { configurable: true, value: () => false });
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: () => {} });
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: () => "blob:result" });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: () => {} });
  account.params = new URLSearchParams();
  account.session.mockResolvedValue({ authenticated: true, apps: [{ id: "try", name: "Try" }] });
  account.tryKey.mockResolvedValue({ id: "try-key", api_key: "test-key", reused: false, app_name: "Try", expires_at: 0 });
  account.promptMemory.mockResolvedValue({
    prompt_memory: { remember: true, retention: "never", effective_remember: true, effective_retention: "never", household: { default_remember: true, never_keep: false, retention_cap: "never" } },
  });
  catalog = workflowsCatalog;
  answer = () => undefined;
  fetchMock.mockReset().mockImplementation(async (url: string, init?: RequestInit) => {
    const given = answer(url, init);
    if (given) return given;
    if (init?.method === "POST") return json({ data: [] });
    if (url.includes("/me/api/receipts/")) return json({}, 404);
    return json(url.includes("/me/api/workflows") ? catalog : emptyModelsCatalog);
  });
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

async function openWorkflow(workflowId: string, viewer: Viewer = owner) {
  account.params = new URLSearchParams({ workflow: workflowId });
  render(
    <ViewerContext.Provider value={viewer}>
      <TryPage />
    </ViewerContext.Provider>,
  );
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent(/\S/));
}

const field = (role: "textbox" | "spinbutton" | "combobox", name: string, required = false) =>
  screen.getByRole(role, { name: new RegExp(`^${name}${required ? " Required" : ""}$`, "i") });

function openSummary(label: string) {
  const details = screen.getByText(label, { selector: "summary" }).closest("details")!;
  if (!details.open) fireEvent.click(screen.getByText(label, { selector: "summary" }));
  return details;
}
function advancedJson() {
  return openSummary("Advanced JSON").querySelector("textarea")!;
}
function setValue(element: Element, value: string) {
  fireEvent.change(element, { target: { value } });
}
async function pick(user: ReturnType<typeof userEvent.setup>, name: string, option: string) {
  await user.click(field("combobox", name, true));
  await user.click(await screen.findByRole("option", { name: option }));
}
const runButton = () => screen.getByRole("button", { name: "Run" });

describe("guided JSON fields", () => {
  it("start from the declared defaults, edit the posted body and keep advanced extras", async () => {
    const user = userEvent.setup();
    answer = (url, init) => {
      if (init?.method === "POST")
        return json({ data: [{ url: DATA_URL_PNG, media_type: "image/png" }] }, 200, { "x-azizam-receipt-id": "receipt_json_workflow" });
      if (url.includes("/me/api/receipts/receipt_json_workflow")) return json({ request_id: "receipt_json_workflow" });
      return undefined;
    };
    await openWorkflow("image.generate.poster");

    expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Poster image");
    // The catalog's example prompt is an illustration, not the person's input.
    expect(field("textbox", "prompt", true)).toHaveValue("");
    expect(field("combobox", "style", true)).toHaveTextContent("studio");
    expect(field("spinbutton", "steps", true)).toHaveValue(8);
    openSummary("More options");
    expect(field("spinbutton", "strength")).toHaveValue(0.35);
    expect(field("spinbutton", "width", true)).toHaveValue(1024);
    expect(field("spinbutton", "height", true)).toHaveValue(1024);
    expect(screen.getByText("Advanced JSON").closest("details")).not.toHaveAttribute("open");

    setValue(field("textbox", "prompt", true), "A red poster.");
    await pick(user, "style", "flatlay");
    setValue(field("spinbutton", "steps", true), "11");
    setValue(field("spinbutton", "strength"), "0.8");
    setValue(field("spinbutton", "width", true), "768");
    setValue(field("spinbutton", "height", true), "512");

    const current = JSON.parse(advancedJson().value) as {
      extensions?: { "com.azizam.workflow"?: Record<string, unknown> };
    };
    setValue(
      advancedJson(),
      JSON.stringify({
        ...current,
        scheduler: "karras",
        nested: { preserve: true },
        extensions: {
          ...current.extensions,
          "com.azizam.workflow": { ...current.extensions?.["com.azizam.workflow"], seed: 99 },
        },
      }),
    );
    fireEvent.click(screen.getByText("Advanced JSON", { selector: "summary" }));
    expect(screen.getByText("Advanced JSON").closest("details")).not.toHaveAttribute("open");

    await user.click(runButton());
    expect(await screen.findByText("Workflow result")).toBeVisible();
    expect(screen.getByAltText("Generated result")).toBeVisible();
    expect(screen.getByRole("link", { name: "View receipt" })).toHaveAttribute("href", "/usage/?receipt=receipt_json_workflow");
    // The raw answer stays folded under Technical details.
    expect(screen.getByText("Technical details").closest("details")).not.toHaveAttribute("open");

    expect(posts()).toHaveLength(1);
    expect(new Headers(posts()[0][1].headers).get("content-type")).toContain("application/json");
    expect(postedJson()).toEqual({
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

  it("block a run and name the problem for a missing, out-of-range and non-object input", async () => {
    const user = userEvent.setup();
    await openWorkflow("image.generate.poster");

    setValue(field("textbox", "prompt", true), "");
    await user.click(runButton());
    expect(await screen.findByText("Write a prompt first")).toBeVisible();
    await waitFor(() => expect(field("textbox", "prompt", true)).toHaveFocus());

    setValue(field("textbox", "prompt", true), "A valid poster.");
    setValue(field("spinbutton", "steps", true), "13");
    await user.click(runButton());
    expect(await screen.findByText("Steps must be at most 12.")).toBeVisible();

    setValue(field("spinbutton", "steps", true), "8");
    openSummary("More options");
    setValue(field("spinbutton", "width", true), "63");
    await user.click(runButton());
    expect(await screen.findByText("Width must be at least 64.")).toBeVisible();

    setValue(field("spinbutton", "width", true), "1024");
    setValue(advancedJson(), "[1,2,3]");
    await user.click(runButton());
    expect((await screen.findAllByText("Workflow inputs must be a JSON object.")).at(-1)).toBeVisible();
    expect(posts()).toHaveLength(0);
  });

  it("send a multipart request with the chosen file, edited numbers and preserved extras", async () => {
    const user = userEvent.setup();
    answer = (_url, init) =>
      init?.method === "POST" ? bytes(atob(PIXEL_PNG), "image/png", { "x-azizam-request-id": "req_workflow_multipart" }) : undefined;
    await openWorkflow("video.edit.remix");

    setValue(field("textbox", "prompt", true), "Turn this source into a crisp launch clip.");
    await pick(user, "mode", "product");
    setValue(field("spinbutton", "frames", true), "18");
    setValue(field("spinbutton", "strength", true), "0.65");
    const current = JSON.parse(advancedJson().value) as Record<string, unknown>;
    setValue(advancedJson(), JSON.stringify({ ...current, preserve_detail: false, colorGrade: "teal" }));
    fireEvent.click(screen.getByText("Advanced JSON", { selector: "summary" }));

    await user.click(runButton());
    // The missing video is named on its own picker; nothing was sent.
    const source = screen.getByRole("group", { name: /Source image/ });
    expect(await within(source).findByRole("alert")).toHaveTextContent("Choose a video first");
    expect(posts()).toHaveLength(0);
    // One picker for the one file: no second upload control, no made-up path.
    expect(document.querySelectorAll('input[type="file"]')).toHaveLength(1);
    expect(screen.queryByText("./source.mp4")).toBeNull();

    await user.upload(
      source.querySelector<HTMLInputElement>("input[data-media-file-input]")!,
      new File(["fake mp4"], "clip.mp4", { type: "video/mp4" }),
    );
    await user.click(runButton());
    expect(await screen.findByText("Workflow result")).toBeVisible();
    expect(screen.getByAltText("Generated result")).toBeVisible();

    const [url, init] = posts()[0];
    expect(String(url)).toContain("/gateway/v1/workflows/remix/run");
    const form = init.body as FormData;
    expect((form.get("source") as File).name).toBe("clip.mp4");
    expect(form.get("prompt")).toBe("Turn this source into a crisp launch clip.");
    expect(form.get("mode")).toBe("product");
    expect(form.get("frames")).toBe("18");
    expect(form.get("strength")).toBe("0.65");
    expect(form.get("preserve_detail")).toBe("false");
    expect(form.get("colorGrade")).toBe("teal");

    expect(screen.getByText("Technical details").closest("details")).not.toHaveAttribute("open");
    fireEvent.click(screen.getByText("Technical details"));
    fireEvent.click(screen.getByText("Copy as"));
    const curl = [...document.querySelectorAll("pre")].find((pre) => pre.textContent?.includes("curl"))!;
    expect(curl).toBeTruthy();
    expect(curl).toHaveTextContent("/gateway/v1/workflows/remix/run");
    expect(curl).toHaveTextContent("clip.mp4");
    expect(curl).toHaveTextContent("Turn this source into a crisp launch clip.");
    expect(curl).toHaveTextContent("colorGrade=teal");
    expect(screen.queryByText("/openai/v1/gateway", { exact: false })).toBeNull();
  });
});

describe("what each kind of answer shows", () => {
  it.each([
    {
      name: "JSON with an image data URL",
      id: "image.generate.poster",
      reply: () => json({ data: [{ url: DATA_URL_PNG, media_type: "image/png" }] }),
      expectIt: async () => expect(await screen.findByAltText("Generated result")).toBeVisible(),
    },
    {
      name: "image bytes",
      id: "video.edit.remix",
      reply: () => bytes(atob(PIXEL_PNG), "image/png"),
      file: new File(["fake mp4"], "clip.mp4", { type: "video/mp4" }),
      expectIt: async () => expect(await screen.findByAltText("Generated result")).toBeVisible(),
    },
    {
      name: "audio bytes",
      id: "audio.generate.voiceover",
      reply: () => bytes("fake audio bytes", "audio/mpeg"),
      // Fake bytes cannot play; the result and its download still appear.
      // (The <audio> element also holds a fallback link; the download is the one outside it.)
      expectIt: async () => {
        const links = await screen.findAllByRole("link", { name: "Download audio" });
        expect(links.find((link) => !link.closest("audio"))).toHaveAttribute("href", expect.stringMatching(/^blob:/));
      },
    },
    {
      name: "video bytes",
      id: "video.generate.preview",
      reply: () => bytes("fake video bytes", "video/mp4"),
      expectIt: async () =>
        expect(await screen.findByRole("link", { name: "Download video" })).toHaveAttribute("href", expect.stringMatching(/^blob:/)),
    },
  ])("a $name answer previews the result", async ({ id, reply, file, expectIt }) => {
    const user = userEvent.setup();
    answer = (_url, init) => (init?.method === "POST" ? reply() : undefined);
    await openWorkflow(id);
    if (id === "image.generate.poster") setValue(field("textbox", "prompt", true), "A teal fish.");
    else setValue(screen.getByRole("textbox", { name: /^(prompt|Describe the \w+) Required$/i }), "A teal fish.");
    if (file) await user.upload(document.querySelector<HTMLInputElement>("input[data-media-file-input]")!, file);
    await user.click(runButton());
    await expectIt();
  });

  it("JSON with signed audio and video URLs says each file could not load, offers Retry and keeps the signed download", async () => {
    const user = userEvent.setup();
    answer = (_url, init) =>
      init?.method === "POST"
        ? json({
            assets: [
              { kind: "audio", signed_url: "https://media.example.test/audio?sig=fixture", media_type: "audio/mpeg" },
              { kind: "video", signed_url: "https://media.example.test/video?sig=fixture", media_type: "video/mp4" },
            ],
          })
        : undefined;
    await openWorkflow("audio.generate.voiceover");
    setValue(screen.getByRole("textbox", { name: /^(prompt|Describe the \w+) Required$/i }), "A teal fish.");
    await user.click(runButton());
    // The signed host is not reachable: each file fails to load.
    const audio = await waitFor(() => {
      const element = document.querySelector("audio");
      expect(element).not.toBeNull();
      return element!;
    });
    fireEvent.error(audio);
    fireEvent.error(document.querySelector("video")!);
    expect(await screen.findByText("The audio couldn’t be loaded.")).toBeVisible();
    expect(screen.getByText("The video couldn’t be loaded.")).toBeVisible();
    expect(screen.getAllByRole("button", { name: "Retry" })).toHaveLength(2);
    expect(screen.getByRole("link", { name: "Download video 2" })).toHaveAttribute("href", "https://media.example.test/video?sig=fixture");
    expect(screen.getByRole("link", { name: "Download audio 1" })).toHaveAttribute("href", "https://media.example.test/audio?sig=fixture");
    expect(screen.getByText("Technical details").closest("details")).not.toHaveAttribute("open");
  });

  it("a workflow with no runnable example request says so and sends nothing", async () => {
    const user = userEvent.setup();
    await openWorkflow("audio.generate.no-example");
    await user.click(runButton());
    expect(await screen.findByText("This workflow does not include a runnable example request yet.")).toBeVisible();
    expect(posts()).toHaveLength(0);
  });
});

describe("workflow jobs", () => {
  it("a queued video job completes into a download from Check again, and a failed job shows its message", async () => {
    const user = userEvent.setup();
    let pollBody: unknown = { id: "job_async_try", object: "video", status: "completed" };
    let submitted: unknown = { id: "job_async_try", object: "video", status: "queued", request_id: "receipt_async_try" };
    answer = (url, init) => {
      if (init?.method === "POST") return json(submitted);
      if (url.endsWith("/v1/videos/job_async_try/content")) return bytes("fake completed video bytes", "video/mp4");
      if (url.endsWith("/v1/videos/job_async_try") || url.endsWith("/v1/videos/job_async_failed")) return json(pollBody);
      if (url.includes("/me/api/receipts/receipt_async_try")) return json({ request_id: "receipt_async_try" });
      return undefined;
    };
    await openWorkflow("video.generate.async");

    setValue(screen.getByRole("textbox", { name: /^prompt Required$/i }), "A teal fish.");
    await user.click(runButton());
    expect(await screen.findByText("Waiting to start")).toBeVisible();
    expect(screen.getByText("Technical details").closest("details")).not.toHaveAttribute("open");
    fireEvent.click(screen.getByText("Technical details"));
    expect(screen.getByText('"status": "queued"', { exact: false })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Check again" }));
    expect(await screen.findByRole("link", { name: "Download video" })).toHaveAttribute("href", expect.stringMatching(/^blob:/));

    submitted = { id: "job_async_failed", object: "video", status: "queued" };
    pollBody = { id: "job_async_failed", object: "video", status: "failed", error: { message: "Renderer ran out of memory." } };
    await user.click(runButton());
    expect(await screen.findByText("Waiting to start")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Check again" }));
    expect(await screen.findByText("Renderer ran out of memory.")).toBeVisible();
    expect(postedJson(0)).toMatchObject({ model: "async", prompt: "A teal fish." });
  });

  it("a media job uses its declared routes, shows a poll error, then loads the signed output after Try again", async () => {
    const user = userEvent.setup();
    let pollStatus = 503;
    answer = (url, init) => {
      if (init?.method === "POST") return json({ object: "media.job", job_id: "signed_job", status: "queued" });
      if (url.endsWith("/gateway/v1/media/jobs/signed_job/content"))
        return json({ assets: [{ kind: "video", signed_url: "https://media.example.test/completed?sig=fixture", media_type: "video/mp4" }] });
      if (url.endsWith("/gateway/v1/media/jobs/signed_job"))
        return pollStatus === 503
          ? json({ error: { message: "Job temporarily unavailable." } }, 503)
          : json({ object: "media.job", job_id: "signed_job", status: "succeeded" });
      return undefined;
    };
    await openWorkflow("video.generate.media-job");
    setValue(screen.getByRole("textbox", { name: /^prompt Required$/i }), "A teal fish.");
    await user.click(runButton());
    expect(await screen.findByText("Waiting to start")).toBeVisible();
    expect(String(posts()[0][0])).toContain("/gateway/v1/media/jobs");
    expect(postedJson()).toEqual({ intent: "video.generate", request: { model: "media-job", prompt: "A teal fish." } });

    await user.click(screen.getByRole("button", { name: "Check again" }));
    expect(await screen.findByText("Job temporarily unavailable.")).toBeVisible();
    pollStatus = 200;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    fireEvent.error(await waitFor(() => {
      const element = document.querySelector("video");
      expect(element).not.toBeNull();
      return element!;
    }));
    expect(await screen.findByText("The video couldn’t be loaded.")).toBeVisible();
    expect(screen.getByText("Ready")).toBeVisible();
    expect(screen.getByRole("link", { name: "Download video" })).toHaveAttribute("href", "https://media.example.test/completed?sig=fixture");
    expect(screen.getByText("No receipt was returned.")).toBeVisible();
  });
});

describe("the receipt link", () => {
  it("is found from the asset when the answer has no receipt header, and only for a receipt the gateway can show", async () => {
    const user = userEvent.setup();
    let reply: () => Response = () =>
      json({ data: [{ asset_id: "img_fixture", url: "data:image/png;base64,iVBORw0KGgo=", media_type: "image/png" }] });
    answer = (url, init) => {
      if (init?.method === "POST") return reply();
      if (url.includes("/me/api/media/img_fixture")) return json({ asset: { id: "img_fixture", request_id: "receipt_from_asset" } });
      if (url.includes("/me/api/receipts/receipt_from_asset")) return json({ request_id: "receipt_from_asset" });
      if (url.includes("receipt%20denied%2Fvalue")) return json({ error: "not found" }, 404);
      if (url.includes("receipt%20verified%2Fvalue")) return json({ request_id: "receipt verified/value" });
      return undefined;
    };
    await openWorkflow("image.generate.poster");
    setValue(field("textbox", "prompt", true), "A teal fish.");

    await user.click(runButton());
    expect(await screen.findByRole("link", { name: "View receipt" })).toHaveAttribute("href", "/usage/?receipt=receipt_from_asset");

    // A receipt the gateway answers 404 for makes no link.
    reply = () => json({ data: [{ url: "data:image/png;base64,iVBORw0KGgo=" }] }, 200, { "x-azizam-receipt-id": "receipt denied/value" });
    await user.click(runButton());
    expect(await screen.findByText("No receipt was returned.")).toBeVisible();
    expect(screen.queryByRole("link", { name: "View receipt" })).toBeNull();

    // A verified receipt links to Usage with its id encoded.
    reply = () => json({ data: [{ url: "data:image/png;base64,iVBORw0KGgo=" }] }, 200, { "x-azizam-receipt-id": "receipt verified/value" });
    await user.click(runButton());
    expect(await screen.findByRole("link", { name: "View receipt" })).toHaveAttribute("href", "/usage/?receipt=receipt%20verified%2Fvalue");
  });
});

describe("composed workflow availability on Try", () => {
  beforeEach(() => {
    catalog = composedCatalog;
  });

  it("a ready workflow shows no availability note", async () => {
    await openWorkflow("image.edit.image-identity");
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent(/^Identity Scene$/);
    expect(screen.queryByTestId("workflow-availability-note")).toBeNull();
  });

  it("a stopped on-demand engine reads as Ready to start and Run stays on", async () => {
    await openWorkflow("image.analyze.face-likeness");
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Face Likeness Check · Ready to start");
    expect(screen.getByTestId("workflow-availability-note")).toHaveTextContent(/^Face service starts when you make a request\.$/);
    expect(screen.queryByText("Unavailable")).toBeNull();
    expect(runButton()).toBeEnabled();
  });

  it.each([owner, member])("a down engine is named to the $role and Run stays off", async (viewer) => {
    await openWorkflow("image.generate.flux2-klein-4b-seamless-tile", viewer);
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Seamless Tile · ComfyUI is down.");
    expect(runButton()).toBeDisabled();
    // Enter in a field still submits the form; a workflow that cannot run sends nothing.
    setValue(screen.getByRole("textbox", { name: /^prompt Required$/i }), "A teal fish.");
    await act(async () => {
      fireEvent.submit(runButton().closest("form")!);
    });
    expect(posts()).toHaveLength(0);
    const note = within(screen.getByTestId("workflow-availability-note"));
    expect(screen.getByTestId("workflow-availability-note")).toHaveTextContent("ComfyUI is down.");
    if (viewer.role === "owner")
      expect(note.getByRole("link", { name: "Open Engines" })).toHaveAttribute("href", "/engines?service=comfyui");
    else expect(note.queryByRole("link")).toBeNull();
  });
});
