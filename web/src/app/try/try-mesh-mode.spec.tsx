import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import TryPage from "./page";
import { MESH_BUSY_MESSAGE } from "./mesh-mode";
import { storeHomeHandoff } from "@/app/_home/handoff";
import { PICTURE_ITEM, RECORDING_ITEM, stubbedLibrary } from "../../../tests/helpers/library-stub";

vi.mock("@google/model-viewer", () => ({}));

const mocks = vi.hoisted(() => ({
  params: new URLSearchParams(),
  session: vi.fn(),
  tryKey: vi.fn(),
  promptMemory: vi.fn().mockResolvedValue({ prompt_memory: null }),
  requestHistory: vi.fn(),
}));

vi.mock("next/navigation", () => ({ useSearchParams: () => mocks.params }));
vi.mock("@/lib/account-api", () => ({
  accountApi: {
    session: mocks.session,
    tryKey: mocks.tryKey,
    revokeKey: vi.fn(),
    promptMemory: mocks.promptMemory,
    requestHistory: mocks.requestHistory,
  },
}));

const json = (body: unknown, ok = true) =>
  ({ ok, status: ok ? 200 : 500, json: async () => body }) as Response;
const models = {
  aliases: [],
  models: [
    {
      id: "chat-a",
      name: "Chat model",
      capabilities: ["chat"],
      locality: "local",
    },
  ],
};
const meshRow = (availability: Record<string, unknown>) => ({
  id: "mesh.generate.trellis-2",
  title: "TRELLIS.2 image to 3D",
  capability: "mesh",
  availability,
  outputs: [{ modality: "mesh", media_type: "model/gltf-binary" }],
  inputs: [
    {
      name: "image",
      type: "string",
      required: true,
      asset: true,
      media_type: "image/*",
      label: "picture",
    },
    { name: "seed", type: "integer", minimum: 0, maximum: 4294967295 },
    {
      name: "resolution",
      type: "integer",
      enum: [512, 1024, 1536],
      default: 1024,
      label: "detail",
    },
    { name: "bg_removal", type: "string", enum: ["threshold", "birefnet"] },
  ],
});
const catalogWith = (...rows: unknown[]) => ({ workflows: rows });
const glb = () =>
  new Response(new Blob(["glTF"]), {
    status: 200,
    headers: { "content-type": "model/gltf-binary" },
  });

const fetchMock = vi.fn();

beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, "hasPointerCapture", {
    configurable: true,
    value: () => false,
  });
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
    configurable: true,
    value: () => {},
  });
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  mocks.params = new URLSearchParams();
  mocks.promptMemory.mockResolvedValue({ prompt_memory: null });
  mocks.requestHistory.mockResolvedValue({
    keep: true,
    retention: "7_days",
    parts: ["accepted"],
    choices: ["1_day", "7_days", "30_days"],
    allowed_parts: ["accepted", "received"],
    notice_due: false,
  });
  vi.stubGlobal("fetch", fetchMock);
  // jsdom has no object URLs; the viewer needs them once a model comes back.
  vi.stubGlobal(
    "URL",
    Object.assign(URL, {
      createObjectURL: vi.fn(() => "blob:mesh-1"),
      revokeObjectURL: vi.fn(),
    }),
  );
  mocks.session.mockResolvedValue({
    authenticated: true,
    apps: [{ id: "app", name: "Try" }],
  });
  mocks.tryKey.mockResolvedValue({
    id: "key",
    api_key: "test-secret",
    reused: false,
    app_name: "Try",
  });
  localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

/** The URLs the page has fetched, whether it passed a string or a Request. */
function urlsFetched(): string[] {
  return fetchMock.mock.calls.map(([input]) =>
    typeof input === "string" ? input : (input as Request).url,
  );
}

/** Waits until the page has asked for its workflow catalog, so an absent tab is absent for a settled page. */
async function catalogRequested() {
  await waitFor(() =>
    expect(urlsFetched().some((url) => url.includes("/me/api/workflows"))).toBe(
      true,
    ),
  );
}

function serve(workflows: unknown) {
  fetchMock.mockReset().mockImplementation(async (url: string) => {
    if (url.includes("3d/generations")) return glb();
    return url.includes("workflows") ? json(workflows) : json(models);
  });
}

test("the Photo to 3D tab is absent when the catalog has no mesh workflow", async () => {
  serve(catalogWith());
  render(<TryPage />);
  await catalogRequested();
  await waitFor(() =>
    expect(screen.queryByRole("status", { name: "Loading models" })).toBeNull(),
  );
  expect(screen.queryByRole("tab", { name: "Photo to 3D" })).toBeNull();
});

test("the Photo to 3D tab stays hidden while the mesh workflow is unavailable", async () => {
  serve(
    catalogWith(
      meshRow({
        available: false,
        status: "unavailable",
        reason: "The mesh service is off.",
      }),
    ),
  );
  render(<TryPage />);
  await catalogRequested();
  await waitFor(() =>
    expect(screen.queryByRole("status", { name: "Loading models" })).toBeNull(),
  );
  expect(screen.queryByRole("tab", { name: "Photo to 3D" })).toBeNull();
  expect(screen.queryByText("The mesh service is off.")).toBeNull();
});

test("a busy GPU keeps the Photo to 3D tab, with the button disabled until it is free", async () => {
  serve(
    catalogWith(
      meshRow({
        available: false,
        status: "busy",
        reason: "Busy with other requests.",
      }),
    ),
  );
  render(<TryPage />);
  await catalogRequested();
  await userEvent.click(
    await screen.findByRole("tab", { name: "Photo to 3D" }),
  );
  expect(screen.getByRole("button", { name: "Make 3D model" })).toBeDisabled();
  expect(screen.getByText(MESH_BUSY_MESSAGE)).toBeInTheDocument();
});

test("a degraded service hides the Photo to 3D tab, since its requests may fail", async () => {
  serve(catalogWith(meshRow({ available: false, status: "degraded" })));
  render(<TryPage />);
  await catalogRequested();
  await waitFor(() =>
    expect(screen.queryByRole("status", { name: "Loading models" })).toBeNull(),
  );
  expect(screen.queryByRole("tab", { name: "Photo to 3D" })).toBeNull();
});

test("the Photo to 3D tab shows once the mesh workflow is available, and runs a picture through it", async () => {
  serve(catalogWith(meshRow({ available: true })));
  const { container } = render(<TryPage />);
  const tab = await screen.findByRole("tab", { name: "Photo to 3D" });
  await userEvent.click(tab);

  const input = container.querySelector(
    'input[type="file"]',
  ) as HTMLInputElement;
  await userEvent.upload(
    input,
    new File(["png"], "mug.png", { type: "image/png" }),
  );
  await userEvent.click(screen.getByRole("button", { name: "Make 3D model" }));

  await waitFor(() =>
    expect(container.querySelector("model-viewer")).not.toBeNull(),
  );
  const meshCall = fetchMock.mock.calls.find(([url]) =>
    String(url).includes("3d/generations"),
  );
  expect(meshCall?.[0]).toBe("/v1/3d/generations/glb");
  const form = meshCall?.[1].body as FormData;
  expect(form.get("resolution")).toBe("1024");
  expect((form.get("image") as File).name).toBe("mug.png");
  expect(new Headers(meshCall?.[1].headers).get("authorization")).toBe(
    "Bearer test-secret",
  );
});

// A 3D edit stage works on a model file, not a picture. The live listing put such a stage, unavailable, ahead of the
// picture workflows, and the tab was judged by it and hidden.
const editStageRow = (availability: Record<string, unknown>) => ({
  id: "mesh.edit.retopo",
  title: "Retopologize a 3D model",
  capability: "mesh",
  availability,
  outputs: [{ modality: "mesh", media_type: "model/gltf-binary" }],
  inputs: [
    {
      name: "mesh",
      type: "string",
      required: true,
      asset: true,
      media_type: "model/gltf-binary",
      label: "model",
    },
    { name: "target_faces", type: "integer", minimum: 1000 },
  ],
});

test("a 3D edit stage listed first does not decide the Photo to 3D tab", async () => {
  serve(
    catalogWith(
      editStageRow({ available: false, status: "unavailable", reason: "The 3D stages service is off." }),
      meshRow({ available: true }),
    ),
  );
  render(<TryPage />);
  expect(await screen.findByRole("tab", { name: "Photo to 3D" })).toBeInTheDocument();
});

test("a 3D edit stage lists under Workflow, not Photo to 3D", async () => {
  serve(catalogWith(editStageRow({ available: true }), meshRow({ available: true })));
  mocks.params = new URLSearchParams("workflow=mesh.edit.retopo");
  render(<TryPage />);
  await screen.findByRole("tab", { name: "Photo to 3D" });
  expect(await screen.findByRole("tab", { name: "Workflow" })).toHaveAttribute("aria-selected", "true");
});

test("the mesh workflow is not also listed under Workflow", async () => {
  serve(catalogWith(meshRow({ available: true })));
  render(<TryPage />);
  await screen.findByRole("tab", { name: "Photo to 3D" });
  await userEvent.click(screen.getByRole("tab", { name: "Workflow" }));
  expect(screen.queryByRole("option", { name: /TRELLIS\.2/ })).toBeNull();
  expect(screen.queryByText(/TRELLIS\.2/)).toBeNull();
});

test("a link for capability=mesh opens the Photo to 3D tab, with or without a model", async () => {
  serve(catalogWith(meshRow({ available: true })));
  mocks.params = new URLSearchParams("capability=mesh&model=mesh.generate.trellis-2");
  render(<TryPage />);
  expect(await screen.findByRole("tab", { name: "Photo to 3D" })).toHaveAttribute("aria-selected", "true");
  expect(screen.getByRole("button", { name: "Make 3D model" })).toBeVisible();
});

test("a link for capability=mesh says so when the Photo to 3D tab is hidden, and opens no workflow", async () => {
  serve(catalogWith(meshRow({ available: false, status: "unavailable" })));
  mocks.params = new URLSearchParams("capability=mesh");
  render(<TryPage />);
  expect(await screen.findByText("No 3D models are available to you right now. Ask the owner about 3D models.")).toBeVisible();
  expect(screen.queryByRole("tab", { name: "Photo to 3D" })).toBeNull();
  expect(screen.queryByRole("tab", { name: "Workflow", selected: true })).toBeNull();
});

test("a link that names a 3D workflow opens the Photo to 3D tab, not Workflow", async () => {
  serve(catalogWith(meshRow({ available: true })));
  mocks.params = new URLSearchParams("workflow=mesh.generate.trellis-2");
  render(<TryPage />);
  expect(await screen.findByRole("tab", { name: "Photo to 3D" })).toHaveAttribute("aria-selected", "true");
  expect(screen.getByRole("button", { name: "Make 3D model" })).toBeVisible();
});

test("a picture chosen on Home opens Photo to 3D and starts the model with that picture", async () => {
  serve(catalogWith(meshRow({ available: true })));
  mocks.params = new URLSearchParams("capability=mesh&model=mesh.generate.trellis-2");
  storeHomeHandoff({
    capability: "mesh",
    model: "mesh.generate.trellis-2",
    prompt: "",
    files: [new File(["png"], "from-home.png", { type: "image/png" })],
  });
  const { container } = render(<TryPage />);
  await waitFor(() => expect(container.querySelector("model-viewer")).not.toBeNull());
  const meshCall = fetchMock.mock.calls.find(([url]) => String(url).includes("3d/generations"));
  expect(((meshCall?.[1].body as FormData).get("image") as File).name).toBe("from-home.png");
  expect(fetchMock.mock.calls.filter(([url]) => String(url).includes("3d/generations"))).toHaveLength(1);
});

test("a recording chosen on Home opens Transcription with that file and runs it", async () => {
  fetchMock.mockReset().mockImplementation(async (url: string) => {
    if (url.includes("audio/transcriptions")) return json({ text: "hello there" });
    return url.includes("workflows")
      ? json(catalogWith())
      : json({ aliases: [], models: [{ id: "stt-a", name: "Words", capabilities: ["transcribe"], locality: "local" }] });
  });
  mocks.params = new URLSearchParams("capability=transcribe&model=stt-a");
  storeHomeHandoff({
    capability: "transcribe",
    model: "stt-a",
    prompt: "",
    files: [new File(["wav"], "memo.wav", { type: "audio/wav" })],
  });
  render(<TryPage />);
  await waitFor(() => expect(urlsFetched().some((url) => url.includes("audio/transcriptions"))).toBe(true));
  const call = fetchMock.mock.calls.find(([url]) => String(url).includes("audio/transcriptions"));
  expect(((call?.[1].body as FormData).get("file") as File).name).toBe("memo.wav");
});

test("opening Transcription from Home with no file leaves it ready and sends nothing", async () => {
  fetchMock.mockReset().mockImplementation(async (url: string) =>
    url.includes("workflows")
      ? json(catalogWith())
      : json({ aliases: [], models: [{ id: "stt-a", name: "Words", capabilities: ["transcribe"], locality: "local" }] }),
  );
  mocks.params = new URLSearchParams("capability=transcribe&model=stt-a");
  storeHomeHandoff({ capability: "transcribe", model: "stt-a", prompt: "" });
  render(<TryPage />);
  await catalogRequested();
  await screen.findByText("Audio file");
  expect(urlsFetched().some((url) => url.includes("audio/transcriptions"))).toBe(false);
  // The field keeps its name for a screen reader: a group labelled by the visible text holds the controls.
  const field = screen.getByRole("group", { name: "Audio file" });
  expect(within(field).getByRole("button", { name: "Choose a file" })).toBeTruthy();
});

test("an audio file chosen from the Library is the file Transcription sends", async () => {
  const library = stubbedLibrary([PICTURE_ITEM, RECORDING_ITEM], async (url) => {
    if (url.includes("audio/transcriptions")) return Response.json({ text: "hello there" });
    return url.includes("workflows")
      ? Response.json(catalogWith())
      : Response.json({ aliases: [], models: [{ id: "stt-a", name: "Words", capabilities: ["transcribe"], locality: "local" }] });
  });
  fetchMock.mockReset().mockImplementation(library.impl);
  mocks.params = new URLSearchParams("capability=transcribe&model=stt-a");
  render(<TryPage />);
  await userEvent.click(await screen.findByRole("button", { name: "Choose from Library" }));
  expect(screen.queryByRole("button", { name: "A teal fish" })).toBeNull();
  await userEvent.click(await screen.findByRole("button", { name: "A short spoken note" }));
  expect(await screen.findByText("From your Library")).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "Run" }));

  await waitFor(() => expect(urlsFetched().some((url) => url.includes("audio/transcriptions"))).toBe(true));
  const call = fetchMock.mock.calls.find(([url]) => String(url).includes("audio/transcriptions"));
  const sent = (call?.[1].body as FormData).get("file") as File;
  expect(sent.name).toBe("library-audio-clip.wav");
  expect(sent.type).toBe("audio/wav");
  expect(library.listed.every((url) => url.includes("kind=audio"))).toBe(true);
});

test("Photo to 3D offers the Library for its picture, filtered to the picture kind", async () => {
  const library = stubbedLibrary([PICTURE_ITEM, RECORDING_ITEM], async (url) => {
    if (url.includes("3d/generations")) return glb();
    return url.includes("workflows")
      ? Response.json(catalogWith(meshRow({ available: true })))
      : Response.json(models);
  });
  fetchMock.mockReset().mockImplementation(library.impl);
  render(<TryPage />);
  await userEvent.click(await screen.findByRole("tab", { name: "Photo to 3D" }));
  await userEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  await userEvent.click(await screen.findByRole("button", { name: "A teal fish" }));
  expect(await screen.findByText("From your Library")).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "Make 3D model" }));
  await waitFor(() => expect(urlsFetched().some((url) => url.includes("3d/generations"))).toBe(true));
  const meshCall = fetchMock.mock.calls.find(([url]) => String(url).includes("3d/generations"));
  expect(((meshCall?.[1].body as FormData).get("image") as File).name).toBe("library-picture.png");
});
