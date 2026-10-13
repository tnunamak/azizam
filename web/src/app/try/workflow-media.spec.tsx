// A workflow run answers with the rendered file's bytes (the gateway's generated
// OpenAPI says the 200 is binary), not with JSON. This renders the Try page with
// that real response shape: a Blob and its content type. It moved here from a
// browser case that stubbed an invented JSON body (assets[].signed_url).
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import TryPage from "./page";

const mocks = vi.hoisted(() => ({
  params: new URLSearchParams(),
  session: vi.fn(),
  tryKey: vi.fn(),
  revokeKey: vi.fn(),
}));
vi.mock("next/navigation", () => ({ useSearchParams: () => mocks.params }));
vi.mock("@/lib/account-api", () => ({
  accountApi: { session: mocks.session, tryKey: mocks.tryKey, revokeKey: mocks.revokeKey },
}));

const json = (body: unknown) =>
  ({ ok: true, status: 200, headers: new Headers({ "content-type": "application/json" }), json: async () => body }) as Response;
const bytes = (type: string) =>
  ({ ok: true, status: 200, headers: new Headers({ "content-type": type }), blob: async () => new Blob(["x"], { type }) }) as Response;
const workflow = (kind: "audio" | "video", mediaType: string) => ({
  id: `${kind}.generate.fixture`,
  short_id: "fixture",
  title: "Fixture",
  description: `${kind} workflow that returns bytes.`,
  engine_binding: "comfyui",
  native_route: "/gateway/v1/workflows/fixture/run",
  inputs: [{ name: "prompt", type: "string", required: true, accepted: true }],
  example_request: {
    method: "POST",
    path: "/gateway/v1/workflows/fixture/run",
    content_type: "application/json",
    body: { model: "fixture", prompt: "Say hello." },
    form: {},
    files: {},
  },
  outputs: [{ kind, media_type: mediaType }],
});

const fetchMock = vi.fn();
beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, "hasPointerCapture", { configurable: true, value: () => false });
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: () => {} });
  URL.createObjectURL = vi.fn(() => "blob:fixture-output");
  URL.revokeObjectURL = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  mocks.session.mockResolvedValue({ authenticated: true, apps: [{ id: "app", name: "Try" }] });
  mocks.tryKey.mockResolvedValue({ id: "key", api_key: "test-secret", reused: false, app_name: "Try" });
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

test.each([
  ["audio", "audio/mpeg", "audio"],
  ["video", "video/mp4", "video"],
] as const)("a workflow's %s bytes that cannot load say so, with Retry and a download link", async (kind, type, noun) => {
  mocks.params = new URLSearchParams(`workflow=${kind}.generate.fixture`);
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") return bytes(type);
    return json(
      url.includes("workflows")
        ? { workflows: [workflow(kind, type)] }
        : { aliases: [], models: [], counts: { aliases: 0, models: 0, capabilities: 0 } },
    );
  });
  render(<TryPage />);
  const prompt = await screen.findByRole("textbox", { name: /^(prompt|Describe the \w+)/i });
  fireEvent.change(prompt, { target: { value: "A teal fish." } });
  await waitFor(() => expect(screen.getByRole("button", { name: "Run" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Run" }));
  const player = await waitFor(() => {
    const element = document.querySelector(kind);
    expect(element).not.toBeNull();
    return element!;
  });
  // The signed host is unreachable: the player's own load failure drives the copy.
  expect(player.getAttribute("src")).toBe("blob:fixture-output");
  fireEvent.error(player);
  expect(await screen.findByText(`The ${noun} couldn’t be loaded.`)).toBeTruthy();
  expect(screen.getAllByRole("button", { name: "Retry" })).toHaveLength(1);
  expect(screen.getByRole("link", { name: `Download ${noun}` }).getAttribute("href")).toBe("blob:fixture-output");
});
