import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import TryPage from "./page";
import { storeHomeHandoff, takeHomeHandoff } from "../_home/handoff";

const mocks = vi.hoisted(() => ({
  params: new URLSearchParams(),
  session: vi.fn(),
  tryKey: vi.fn(),
  revokeKey: vi.fn(),
  promptMemory: vi.fn(),
  requestHistory: vi.fn(),
}));
vi.mock("next/navigation", () => ({ useSearchParams: () => mocks.params }));
vi.mock("@/lib/account-api", () => ({ accountApi: {
  session: mocks.session,
  tryKey: mocks.tryKey,
  revokeKey: mocks.revokeKey,
  promptMemory: mocks.promptMemory,
  requestHistory: mocks.requestHistory,
} }));

const json = (body: unknown) => new Response(JSON.stringify(body), {
  status: 200, headers: { "content-type": "application/json" },
});
const pictureWorkflow = {
  id: "wf-picture", title: "Picture workflow", capability: "video",
  inputs: [
    { name: "prompt", type: "string", required: true },
    { name: "image", type: "file", required: true, media_type: "image/png" },
  ],
  outputs: [],
  example_request: {
    path: "/v1/workflows/picture", content_type: "multipart/form-data",
    form: { prompt: "the stock example" },
  },
  openapi: { execution: { sync: {
    path: "/v1/workflows/picture",
    fields: { image: { in: "file", field: "image" } },
  } } },
};
const otherWorkflow = {
  id: "wf-other", title: "Other workflow", capability: "video",
  inputs: [{ name: "prompt", type: "string", required: true }], outputs: [],
  example_request: { path: "/v1/workflows/other", body: { prompt: "another example" } },
};
const models = { aliases: [], models: [
  { id: "stt", name: "Words", capability: "transcribe" },
] };
const fetchMock = vi.fn();
const submitted = vi.fn();
let objectUrlDescriptors: Record<string, PropertyDescriptor | undefined>;
const posts = () => fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");

beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, "hasPointerCapture", {
    configurable: true, value: () => false,
  });
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
    configurable: true, value: () => {},
  });
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  objectUrlDescriptors = Object.fromEntries(["createObjectURL", "revokeObjectURL"].map((name) =>
    [name, Object.getOwnPropertyDescriptor(URL, name)]));
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:handoff") });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
  // Keep the real React submit handler, while avoiding jsdom's native-submit limitation.
  vi.spyOn(HTMLFormElement.prototype, "requestSubmit").mockImplementation(function (this: HTMLFormElement) {
    submitted(this);
    this.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });
  mocks.params = new URLSearchParams();
  mocks.session.mockResolvedValue({ authenticated: true, apps: [{ id: "app", name: "Try" }] });
  mocks.tryKey.mockResolvedValue({ id: "key", api_key: "test-secret", reused: false, app_name: "Try" });
  mocks.promptMemory.mockResolvedValue({ prompt_memory: null });
  mocks.requestHistory.mockResolvedValue({
    keep: true, retention: "7_days", parts: ["accepted"],
    choices: ["1_day", "7_days", "30_days"],
    allowed_parts: ["accepted", "received"], notice_due: false,
  });
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url === "/me/api/models/catalog") return json(models);
    if (url === "/me/api/workflows") return json({ workflows: [pictureWorkflow, otherWorkflow] });
    if (init?.method === "POST") return json({ text: "Finished" });
    throw new Error("Unexpected request: " + url);
  });
  localStorage.clear();
  sessionStorage.clear();
  takeHomeHandoff(null);
});

afterEach(() => {
  cleanup();
  for (const [name, descriptor] of Object.entries(objectUrlDescriptors)) {
    if (descriptor) Object.defineProperty(URL, name, descriptor);
    else Reflect.deleteProperty(URL, name);
  }
  takeHomeHandoff(null);
  sessionStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

test("submits Home's words and its actual picture, not the workflow example", async () => {
  mocks.params = new URLSearchParams("capability=video&model=wf-picture");
  const picture = new File(["p"], "fox.png", { type: "image/png" });
  storeHomeHandoff({
    capability: "video", model: "wf-picture", prompt: "a red fox",
    files: [picture], file_inputs: ["image"],
  });
  render(<TryPage />);
  await waitFor(() => expect(posts()).toHaveLength(1));
  const [url, init] = posts()[0];
  expect(url).toBe("/v1/workflows/picture");
  expect(init.body).toBeInstanceOf(FormData);
  const body = init.body as FormData;
  expect(body.get("prompt")).toBe("a red fox");
  expect((body.get("image") as File).name).toBe("fox.png");
  expect(submitted).toHaveBeenCalledTimes(1);
});

test("a hand-off waiting for W1's picture never auto-submits W2", async () => {
  mocks.params = new URLSearchParams("capability=video&model=wf-picture");
  storeHomeHandoff({ capability: "video", model: "wf-picture", prompt: "a red fox" });
  render(<TryPage />);
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Picture workflow"));
  expect(submitted).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("combobox", { name: "Model" }));
  await userEvent.click(screen.getByRole("option", { name: "Other workflow" }));
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Other workflow"));
  await act(async () => { await Promise.resolve(); });
  expect(submitted).not.toHaveBeenCalled();
  expect(mocks.tryKey).not.toHaveBeenCalled();
  expect(posts()).toHaveLength(0);
});

test("names a picture dropped from transcribe's audio slot and waits without submitting", async () => {
  mocks.params = new URLSearchParams("capability=transcribe&model=stt");
  storeHomeHandoff({
    capability: "transcribe", model: "stt", prompt: "",
    files: [new File(["p"], "fox.png", { type: "image/png" })], file_inputs: ["audio"],
  });
  render(<TryPage />);
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Words"));
  expect(await screen.findByText("Could not attach fox.png. Choose a matching file.")).toBeVisible();
  await act(async () => { await Promise.resolve(); });
  expect(submitted).not.toHaveBeenCalled();
  expect(mocks.tryKey).not.toHaveBeenCalled();
  expect(posts()).toHaveLength(0);
  expect(screen.queryByText("Choose an audio file or record one first.")).toBeNull();
});

test("a matching transcribe hand-off still submits its named audio file exactly once", async () => {
  mocks.params = new URLSearchParams("capability=transcribe&model=stt");
  storeHomeHandoff({
    capability: "transcribe", model: "stt", prompt: "",
    files: [new File(["w"], "voice.wav", { type: "audio/wav" })], file_inputs: ["audio"],
  });
  render(<TryPage />);
  await waitFor(() => expect(posts()).toHaveLength(1));
  const [url, init] = posts()[0];
  expect(url).toBe("/openai/v1/audio/transcriptions");
  expect((init.body as FormData).get("model")).toBe("stt");
  const file = (init.body as FormData).get("file") as File;
  expect(file.name).toBe("voice.wav");
  expect(file.type).toBe("audio/wav");
  expect(submitted).toHaveBeenCalledTimes(1);
  expect(screen.queryByText(/Could not attach/)).toBeNull();
});
