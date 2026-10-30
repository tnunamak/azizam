import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import TryPage from "./page";
import { MESH_BUSY_MESSAGE } from "./mesh-mode";

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
    if (url.includes("meshes")) return glb();
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
    String(url).includes("meshes"),
  );
  expect(meshCall?.[0]).toBe("/v1/meshes/generate");
  const form = meshCall?.[1].body as FormData;
  expect(form.get("resolution")).toBe("1024");
  expect((form.get("image") as File).name).toBe("mug.png");
  expect(new Headers(meshCall?.[1].headers).get("authorization")).toBe(
    "Bearer test-secret",
  );
});

test("the mesh workflow is not also listed under Workflow", async () => {
  serve(catalogWith(meshRow({ available: true })));
  render(<TryPage />);
  await screen.findByRole("tab", { name: "Photo to 3D" });
  await userEvent.click(screen.getByRole("tab", { name: "Workflow" }));
  expect(screen.queryByRole("option", { name: /TRELLIS\.2/ })).toBeNull();
  expect(screen.queryByText(/TRELLIS\.2/)).toBeNull();
});
