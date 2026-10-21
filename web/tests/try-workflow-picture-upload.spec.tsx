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
  tryKey: vi.fn(),
  revokeKey: vi.fn(),
  promptMemory: vi.fn(),
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
let activeCatalog: unknown = catalog;
// Prompt memory as the member reads it: on by default.
const promptMemory = (remember: boolean) => ({
  prompt_memory: {
    remember,
    retention: "never",
    effective_remember: remember,
    effective_retention: "never",
    household: {
      default_remember: true,
      never_keep: false,
      retention_cap: "never",
    },
  },
});
const PNG = new File([new Uint8Array([137, 80, 78, 71])], "garden.png", {
  type: "image/png",
});

function posts() {
  return fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");
}

beforeEach(() => {
  activeCatalog = catalog;
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
  account.tryKey.mockResolvedValue({ id: "try-key", api_key: "test-key", reused: false, app_name: "Try", expires_at: 0 });
  account.promptMemory.mockResolvedValue(promptMemory(true));
  fetchMock.mockReset().mockImplementation(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") return json({ data: [] });
    if (url.includes("/me/api/receipts/")) return json({}, 404);
    return json(url.includes("workflows") ? activeCatalog : { aliases: [], models: [] });
  });
  vi.stubGlobal("fetch", fetchMock);
  // The "Don't keep this prompt" switch measures itself.
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
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

async function openWorkflow(viewer: Viewer = member, title = "Flux2 Klein 4B Edit") {
  render(
    <ViewerContext.Provider value={viewer}>
      <TryPage />
    </ViewerContext.Provider>,
  );
  await waitFor(() =>
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent(title),
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

  it("asks the gateway not to keep the prompt when the member turned memory off", async () => {
    account.promptMemory.mockResolvedValue(promptMemory(false));
    await openWorkflow();
    await screen.findByText("Prompts aren’t kept");
    fireEvent.change(screen.getByRole("textbox", { name: /Describe the picture/ }), {
      target: { value: "Make the sky purple" },
    });
    const source = screen.getByRole("group", { name: /Picture to change/ });
    const input = source.querySelector<HTMLInputElement>("input[data-media-file-input]")!;
    await userEvent.upload(input, PNG);
    await userEvent.click(screen.getByRole("button", { name: /^Run/ }));
    await waitFor(() => expect(posts()).toHaveLength(1));
    const form = posts()[0][1].body as FormData;
    expect((form.get("image") as File).name).toBe("garden.png");
    expect(JSON.parse(form.get("extensions") as string)).toMatchObject({
      "com.azizam.prompts": { mode: "forget" },
    });
  });
});

describe("a workflow with repeated image[] parts", () => {
  beforeEach(() => {
    const base = catalog.workflows[0];
    activeCatalog = {
      workflows: [{
        ...base,
        id: "image.edit.multiref",
        title: "Two reference pictures",
        inputs: [
          base.inputs[0],
          { ...base.inputs[1], name: "source_image", label: "a main picture", request_field: "image[]" },
          { ...base.inputs[1], name: "reference_image_2", label: "a second picture", request_field: "image[]" },
        ],
        openapi: { execution: { sync: {
          ...base.openapi.execution.sync,
          fields: {
            prompt: { in: "form", field: "prompt" },
            source_image: { in: "file", field: "image[]" },
            reference_image_2: { in: "file", field: "image[]" },
          },
        } } },
      }],
    };
    account.params = new URLSearchParams("workflow=image.edit.multiref");
  });

  async function open() {
    await openWorkflow(owner, "Two reference pictures");
    fireEvent.change(screen.getByRole("textbox", { name: /Describe the picture/ }), {
      target: { value: "Combine both pictures" },
    });
  }

  async function upload(group: RegExp, picture = PNG) {
    const input = screen.getByRole("group", { name: group })
      .querySelector<HTMLInputElement>("input[data-media-file-input]")!;
    await userEvent.upload(input, picture);
  }

  it("sends a Library picture and an upload as two image[] parts in manifest order", async () => {
    const normalFetch = fetchMock.getMockImplementation()!;
    const assetId = `img_${"a".repeat(64)}`;
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.startsWith("/me/api/media?")) return json({ assets: [{
        asset_id: assetId, kind: "image", media_type: "image/png",
        prompt: "Library main picture", available: true,
      }] });
      if (url.includes(`/media/${assetId}/download`))
        return json({ download_url: "https://media.example.test/main.png" });
      if (url === "https://media.example.test/main.png")
        return new Response(new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]), {
          headers: { "content-type": "image/png" },
        });
      return normalFetch(url, init);
    });
    await open();
    // Choose in reverse order: the wire order must follow the manifest.
    await upload(/Second picture/);
    const main = screen.getByRole("group", { name: /Main picture/ });
    await userEvent.click(within(main).getByRole("button", { name: "Choose from Library" }));
    await userEvent.click(await screen.findByRole("button", { name: "Library main picture" }));
    expect(await within(main).findByText("From your Library")).toBeVisible();
    expect(within(screen.getByRole("group", { name: /Second picture/ })).getByText("garden.png")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: /^Run/ }));
    await waitFor(() => expect(posts()).toHaveLength(1));
    const [url, init] = posts()[0];
    expect(String(url)).toContain("/v1/images/edits");
    const form = init.body as FormData;
    expect(form.getAll("image[]").map((file) => (file as File).name))
      .toEqual(["library-picture.png", "garden.png"]);
    expect(form.getAll("image[]").map((file) => (file as File).size)).toEqual([8, 4]);
    expect(form.get("prompt")).toBe("Combine both pictures");
  });

  it("asks to Choose every picture when the second picture is missing and sends nothing", async () => {
    await open();
    await upload(/Main picture/);
    await userEvent.click(screen.getByRole("button", { name: /^Run/ }));
    expect(await screen.findByText("Choose every picture this workflow needs first.")).toBeVisible();
    expect(within(screen.getByRole("group", { name: /Second picture/ })).getByRole("alert"))
      .toHaveTextContent("Choose a picture first");
    expect(posts()).toHaveLength(0);
  });

  it("shows both image[] parts in curl, JavaScript and Python examples", async () => {
    await open();
    await upload(/Main picture/);
    await upload(/Second picture/, new File(["second"], "reference.png", { type: "image/png" }));
    await userEvent.click(screen.getByRole("button", { name: /^Run/ }));
    await waitFor(() => expect(posts()).toHaveLength(1));
    fireEvent.click(screen.getByText("Technical details"));
    fireEvent.click(screen.getByText("Copy request as"));
    for (const language of ["curl", "JavaScript", "Python"]) {
      await userEvent.click(screen.getByRole("tab", { name: language }));
      const code = screen.getByRole("tabpanel", { name: language }).querySelector("code")!.textContent!;
      expect(code.match(/image\[\]/g)).toHaveLength(2);
      expect(code.indexOf("garden.png")).toBeLessThan(code.indexOf("reference.png"));
      if (language === "JavaScript") expect(code.match(/form.append\("image\[\]"/g)).toHaveLength(2);
      if (language === "Python") {
        expect(code).toContain("files = [");
        expect(code.match(/\("image\[\]", \(/g)).toHaveLength(2);
      }
    }
  });
});
