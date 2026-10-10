import {
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
import catalog from "./fixtures/workflows-image-edit.json";

// The real catalog row the gateway builds for "Flux2 Klein 4B Edit": its two
// pictures are typed "string" with `asset: true`, and the recommended route
// (multipart /v1/images/edits) sends them as the file parts "image" and
// "mask". Each picture gets one picker; nothing shows a made-up path.
const account = vi.hoisted(() => ({
  session: vi.fn(),
  mintKey: vi.fn(),
  revokeKey: vi.fn(),
  params: new URLSearchParams(),
}));
vi.mock("next/navigation", () => ({
  useSearchParams: () => account.params,
}));
vi.mock("@/lib/account-api", () => ({ accountApi: account }));

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
const fetchMock = vi.fn();
const PNG = new File([new Uint8Array([137, 80, 78, 71])], "garden.png", {
  type: "image/png",
});

function posts() {
  return fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");
}

beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, "hasPointerCapture", {
    configurable: true,
    value: () => false,
  });
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
    configurable: true,
    value: () => {},
  });
  account.params = new URLSearchParams("workflow=image.edit.flux2-klein-4b");
  account.session.mockResolvedValue({
    authenticated: true,
    apps: [{ id: "try", name: "Try" }],
  });
  account.mintKey.mockResolvedValue({ id: "try-key", api_key: "test-key" });
  fetchMock.mockReset().mockImplementation(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") return json({ data: [] });
    if (url.includes("/me/api/receipts/")) return json({}, 404);
    return json(url.includes("workflows") ? catalog : { aliases: [], models: [] });
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

const member: Viewer = {
  authenticated: true,
  name: "Robin",
  email: "robin@example.test",
  role: "member",
  admin: false,
};
const owner: Viewer = { ...member, name: "Sam", role: "owner", admin: true };

async function openWorkflow(viewer: Viewer = member) {
  render(
    <ViewerContext.Provider value={viewer}>
      <TryPage />
    </ViewerContext.Provider>,
  );
  await waitFor(() =>
    expect(screen.getByRole("combobox")).toHaveTextContent("Flux2 Klein 4B Edit"),
  );
}

describe("a picture-edit workflow on Try", () => {
  it("gives each picture exactly one picker, in plain words", async () => {
    await openWorkflow();
    const source = screen.getByRole("group", { name: /Picture to change/ });
    const mask = screen.getByRole("group", { name: /Mask/ });
    expect(within(source).getByText("The picture you want to change.")).toBeVisible();
    expect(within(source).getAllByRole("button", { name: "Choose a file" })).toHaveLength(1);
    expect(within(mask).getAllByRole("button", { name: "Choose a file" })).toHaveLength(1);
    // Two pictures, two pickers: no second upload control below the form.
    expect(screen.getAllByRole("button", { name: "Choose a file" })).toHaveLength(2);
    expect(document.querySelectorAll('input[type="file"]')).toHaveLength(2);
    // No text box for a picture, no raw field name, no made-up path.
    expect(screen.queryByRole("textbox", { name: /source_image|Picture to change|Mask/i })).toBeNull();
    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/\.bin\b|\.\/[\w-]+\.\w+|source_image|Managed by the workflow/);
  });

  it("keeps the request JSON and input details for the owner, behind disclosures", async () => {
    await openWorkflow();
    expect(screen.queryByText("Advanced JSON")).toBeNull();
    expect(screen.queryByText("Input details")).toBeNull();
    cleanup();
    await openWorkflow(owner);
    expect(screen.getByText("Advanced JSON").closest("details")).not.toHaveAttribute("open");
    expect(screen.getByText("Input details").closest("details")).not.toHaveAttribute("open");
  });

  it("asks for the missing picture on its field and sends nothing", async () => {
    await openWorkflow();
    fireEvent.change(screen.getByRole("textbox", { name: /Describe the picture/ }), {
      target: { value: "Make the sky purple" },
    });
    await userEvent.click(screen.getByRole("button", { name: /^Run/ }));
    const source = screen.getByRole("group", { name: /Picture to change/ });
    expect(await within(source).findByRole("alert")).toHaveTextContent("Choose a picture first");
    expect(within(screen.getByRole("group", { name: /Mask/ })).queryByRole("alert")).toBeNull();
    expect(posts()).toHaveLength(0);
  });

  it("sends the chosen picture as the route's file part", async () => {
    await openWorkflow();
    fireEvent.change(screen.getByRole("textbox", { name: /Describe the picture/ }), {
      target: { value: "Make the sky purple" },
    });
    const source = screen.getByRole("group", { name: /Picture to change/ });
    const input = source.querySelector<HTMLInputElement>("input[data-media-file-input]")!;
    await userEvent.upload(input, PNG);
    expect(await within(source).findByText("garden.png")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: /^Run/ }));
    await waitFor(() => expect(posts()).toHaveLength(1));
    const [url, init] = posts()[0];
    expect(String(url)).toContain("/v1/images/edits");
    const form = init.body as FormData;
    expect(form.get("image")).toBeInstanceOf(File);
    expect((form.get("image") as File).name).toBe("garden.png");
    expect(form.get("prompt")).toBe("Make the sky purple");
    expect(form.has("source_image")).toBe(false);
    expect(form.has("mask")).toBe(false);
  });
});
