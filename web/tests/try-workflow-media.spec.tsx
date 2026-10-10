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
import { Profiler, useState } from "react";
import TryPage from "@/app/try/page";
import { WorkflowInputs } from "@/app/try/workflow-inputs";
import { MediaInput } from "@/app/try/workflow-media-input";
import catalog from "./fixtures/workflows-ltx-i2v.json";


describe("media prop changes before passive effects", () => {
  it("resets a failed reference preview on the first value-change commit", () => {
    const commits: Array<string | null> = [];
    let capture = false;
    const onCommit = () => {
      if (capture) commits.push(screen.queryByRole("img", { name: "Chosen picture" })?.getAttribute("src") ?? null);
    };
    const view = (value: string) => (
      <Profiler id="reference" onRender={onCommit}>
        <MediaInput id="reference" label="Picture" kind="image" value={value} onChange={() => {}} />
      </Profiler>
    );
    const { rerender } = render(view("https://example.test/old.png"));
    fireEvent.error(screen.getByRole("img", { name: "Chosen picture" }));
    expect(screen.queryByRole("img", { name: "Chosen picture" })).toBeNull();
    capture = true;
    rerender(view("https://example.test/new.png"));
    expect(commits[0]).toBe("https://example.test/new.png");
  });

  it("never commits stale file URLs when replacing or reselecting files and revokes every URL", () => {
    const create = vi.fn()
      .mockReturnValueOnce("blob:first")
      .mockReturnValueOnce("blob:second")
      .mockReturnValueOnce("blob:third");
    const revoke = vi.fn();
    const originalCreate = Object.getOwnPropertyDescriptor(URL, "createObjectURL");
    const originalRevoke = Object.getOwnPropertyDescriptor(URL, "revokeObjectURL");
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: create });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: revoke });
    const commits: Array<string | null> = [];
    let capture = false;
    const onCommit = () => {
      if (capture) commits.push(screen.queryByRole("img", { name: "Chosen picture" })?.getAttribute("src") ?? null);
    };
    const view = (file: File | null) => (
      <Profiler id="file" onRender={onCommit}>
        <MediaInput id="file" label="Picture" kind="image" file={file} onFileChange={() => {}} />
      </Profiler>
    );
    try {
      const { rerender, unmount } = render(view(PNG));
      expect(screen.getByRole("img", { name: "Chosen picture" })).toHaveAttribute("src", "blob:first");
      capture = true;
      rerender(view(new File(["second"], "second.png", { type: "image/png" })));
      expect(commits.length).toBeGreaterThan(0);
      expect(commits).not.toContain("blob:first");
      expect(screen.getByRole("img", { name: "Chosen picture" })).toHaveAttribute("src", "blob:second");
      expect(revoke).toHaveBeenCalledWith("blob:first");
      rerender(view(null));
      expect(screen.queryByRole("img", { name: "Chosen picture" })).toBeNull();
      expect(revoke).toHaveBeenCalledWith("blob:second");
      commits.length = 0;
      rerender(view(PNG));
      expect(commits).not.toContain("blob:first");
      expect(commits).not.toContain("blob:second");
      expect(screen.getByRole("img", { name: "Chosen picture" })).toHaveAttribute("src", "blob:third");
      unmount();
      expect(revoke).toHaveBeenCalledWith("blob:third");
      expect(revoke).toHaveBeenCalledTimes(3);
    } finally {
      if (originalCreate) Object.defineProperty(URL, "createObjectURL", originalCreate);
      else Reflect.deleteProperty(URL, "createObjectURL");
      if (originalRevoke) Object.defineProperty(URL, "revokeObjectURL", originalRevoke);
      else Reflect.deleteProperty(URL, "revokeObjectURL");
    }
  });

  it("reopens Library in loading state on its first commit", async () => {
    let capture = false;
    const commits: boolean[] = [];
    render(
      <Profiler id="picker" onRender={() => {
        if (capture) {
          const dialog = screen.queryByRole("dialog");
          if (dialog) commits.push(dialog.querySelector("[aria-busy='true']") !== null);
        }
      }}>
        <MediaInput id="picker" label="Picture" kind="image" onChange={() => {}} />
      </Profiler>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
    fireEvent.click(await screen.findByRole("button", { name: "A teal fish" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    fetchMock.mockImplementation(() => new Promise<Response>(() => {}));
    capture = true;
    fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
    expect(commits.length).toBeGreaterThan(0);
    expect(commits[0]).toBe(true);
    expect(screen.queryByRole("button", { name: "A teal fish" })).toBeNull();
  });
});

// The page is driven by the real catalog row the gateway builds for
// "LTX 2.5 Distilled I2V" (an image-to-video workflow): its picture input
// is typed "string", lives in the request body and writes input_reference.
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

const models = {
  aliases: [],
  models: [
    { id: "chat", name: "Chat model", capabilities: ["chat"], locality: "local" },
  ],
};
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
const fetchMock = vi.fn();
const hex = "c".repeat(64);
const libraryAssets = {
  assets: [
    { asset_id: `img_${hex}`, kind: "image", prompt: "A teal fish", available: true },
    { asset_id: `img_${"d".repeat(64)}`, kind: "image", prompt: "Gone", available: false },
  ],
};
const PNG = new File([new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10])], "boat.png", {
  type: "image/png",
});
let videoResponse: () => Response;

function posts() {
  return fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");
}
function postedBody() {
  return JSON.parse(posts().at(-1)![1].body as string) as Record<string, unknown>;
}
function pickerInput() {
  return document.querySelector<HTMLInputElement>("input[data-media-file-input]")!;
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
  account.params = new URLSearchParams("workflow=video.generate.ltx-2.5-i2v");
  account.session.mockResolvedValue({
    authenticated: true,
    apps: [{ id: "try", name: "Try" }],
  });
  account.mintKey.mockResolvedValue({ id: "try-key", api_key: "test-key" });
  videoResponse = () => json({ id: "video_1", object: "video", status: "queued" });
  fetchMock
    .mockReset()
    .mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") return videoResponse();
      if (url.startsWith("/me/api/media?")) return json(libraryAssets);
      if (url.includes("/me/api/receipts/")) return json({}, 404);
      return json(url.includes("workflows") ? catalog : models);
    });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

async function openWorkflow() {
  render(<TryPage />);
  await waitFor(() =>
    expect(screen.getByRole("combobox")).toHaveTextContent(
      "LTX 2.5 Distilled I2V",
    ),
  );
}

describe("an image-to-video workflow on Try", () => {
  it("offers a picture picker and speaks plainly", async () => {
    await openWorkflow();
    const group = screen.getByRole("group", { name: /Starting picture/ });
    expect(within(group).getByRole("button", { name: "Choose a file" })).toBeVisible();
    expect(
      within(group).getByRole("button", { name: "Choose from Library" }),
    ).toBeVisible();
    expect(within(group).getByText(/up to 10 MB/)).toBeVisible();
    // The raw reference is under Advanced, not the main control.
    expect(within(group).getByText("Advanced")).toBeVisible();
    // A member gets no technical mapping and no request JSON at all.
    expect(screen.queryByText("Input details")).toBeNull();
    expect(screen.queryByText("Advanced JSON")).toBeNull();
    const page = document.body.cloneNode(true) as HTMLElement;
    page.querySelectorAll("details").forEach((node) => node.remove());
    expect(page.textContent).not.toMatch(
      /Request body|Writes prompt|Writes negative_prompt|Guided controls|Media input:|\bstring\b|input_reference/,
    );
  });

  it("starts with an empty description and an example placeholder, not typed text", async () => {
    await openWorkflow();
    const prompt = screen.getByRole("textbox", { name: /Describe the video/ });
    expect(prompt).toHaveValue("");
    expect(prompt).toHaveAttribute("placeholder", expect.stringMatching(/camera|boat/i));
    const avoid = screen.getByRole("textbox", { name: /Avoid \(optional\)/ });
    expect(avoid).toHaveValue("");
    expect(avoid).not.toHaveAttribute("placeholder", "value");
    expect(screen.queryByDisplayValue("value")).toBeNull();
    expect(screen.queryByDisplayValue(/teal fish/)).toBeNull();
  });

  it("names the selected tab once, with no matching heading above the form", async () => {
    await openWorkflow();
    expect(screen.getAllByText("Workflow")).toHaveLength(1);
    expect(screen.queryByRole("heading", { name: "Workflow" })).toBeNull();
  });

  it("says what is missing on the field and sends nothing", async () => {
    const user = userEvent.setup();
    await openWorkflow();
    await user.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByText("Choose a picture to start from")).toBeVisible();
    expect(screen.getByText("Describe the video first")).toBeVisible();
    expect(screen.getByRole("button", { name: "Run" })).toBeEnabled();
    expect(posts()).toHaveLength(0);
    expect(account.mintKey).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: /Describe the video/ })).toHaveFocus(),
    );
    // The message clears once the field is filled.
    await user.type(
      screen.getByRole("textbox", { name: /Describe the video/ }),
      "A boat drifts",
    );
    expect(screen.queryByText("Describe the video first")).toBeNull();
  });

  it("chooses a file, previews it and sends it as a data URI in input_reference", async () => {
    const user = userEvent.setup();
    await openWorkflow();
    await user.type(
      screen.getByRole("textbox", { name: /Describe the video/ }),
      "A boat drifts",
    );
    await user.upload(pickerInput(), PNG);
    const preview = await screen.findByRole("img", { name: "Chosen picture" });
    expect(preview).toHaveAttribute("src", expect.stringMatching(/^data:image\/png;base64,/));
    await user.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() => expect(posts()).toHaveLength(1));
    const body = postedBody();
    expect(body.input_reference).toMatch(/^data:image\/png;base64,/);
    expect(body.prompt).toBe("A boat drifts");
    expect(body.model).toBe("video-ltx-2.5-i2v");
    expect(JSON.stringify(body)).not.toContain('"value"');
    expect(body).not.toHaveProperty("size");
    expect(body).not.toHaveProperty("seconds");
  });

  it("refuses a file that is too large and says the limit", async () => {
    const user = userEvent.setup();
    await openWorkflow();
    const big = new File([new Uint8Array(11 * 1024 * 1024)], "huge.png", {
      type: "image/png",
    });
    await user.upload(pickerInput(), big);
    expect(await screen.findByText(/Choose one under 10 MB/)).toBeVisible();
    expect(screen.queryByRole("img", { name: "Chosen picture" })).toBeNull();
  });

  it("refuses a file that is not a picture", async () => {
    await openWorkflow();
    fireEvent.change(pickerInput(), {
      target: {
        files: [new File(["hello"], "notes.txt", { type: "text/plain" })],
      },
    });
    expect(await screen.findByText(/not a picture/)).toBeVisible();
  });

  it("chooses from the Library and sends the asset id", async () => {
    const user = userEvent.setup();
    await openWorkflow();
    await user.type(
      screen.getByRole("textbox", { name: /Describe the video/ }),
      "A boat drifts",
    );
    await user.click(screen.getByRole("button", { name: "Choose from Library" }));
    const dialog = await screen.findByRole("dialog");
    // Only pictures that are still stored are offered.
    const tiles = await within(dialog).findAllByRole("button", { name: "A teal fish" });
    expect(tiles).toHaveLength(1);
    expect(within(dialog).queryByRole("button", { name: "Gone" })).toBeNull();
    await user.click(tiles[0]);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(await screen.findByRole("img", { name: "Chosen picture" })).toHaveAttribute(
      "src",
      `/me/api/media/img_${hex}/download?scope=me`,
    );
    expect(screen.getByText("From your Library")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(postedBody().input_reference).toBe(`img_${hex}`);
  });

  it("takes a pasted Library link under Advanced", async () => {
    const user = userEvent.setup();
    await openWorkflow();
    await user.click(screen.getByText("Advanced"));
    await user.click(
      screen.getByRole("textbox", { name: "Link, Library link or data URI" }),
    );
    await user.paste(`https://ai.vivid.fish/me/api/media/img_${hex}/download?scope=me`);
    expect(
      screen.getByRole("textbox", { name: "Link, Library link or data URI" }),
    ).toHaveValue(`img_${hex}`);
  });

  describe("after a failed run", () => {
    async function failFirstRun() {
      const user = userEvent.setup();
      videoResponse = () =>
        json({ error: { message: "The gateway could not start this video." } }, 400);
      await openWorkflow();
      await user.type(
        screen.getByRole("textbox", { name: /Describe the video/ }),
        "A boat drifts",
      );
      await user.upload(pickerInput(), PNG);
      await user.click(screen.getByRole("button", { name: "Run" }));
      await screen.findByText("The gateway could not start this video.");
      return user;
    }

    it("offers Edit and run again, not a catalog reload called Try again", async () => {
      await failFirstRun();
      expect(screen.getByRole("button", { name: "Edit and run again" })).toBeVisible();
      expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
    });

    it("keeps the form, moves focus to it, and does not repeat the request", async () => {
      const user = await failFirstRun();
      const calls = fetchMock.mock.calls.length;
      await user.click(screen.getByRole("button", { name: "Edit and run again" }));
      expect(fetchMock.mock.calls.length).toBe(calls);
      expect(posts()).toHaveLength(1);
      expect(account.mintKey).toHaveBeenCalledTimes(1);
      const prompt = screen.getByRole("textbox", { name: /Describe the video/ });
      expect(prompt).toHaveFocus();
      expect(prompt).toHaveValue("A boat drifts");
      expect(screen.getByRole("img", { name: "Chosen picture" })).toBeVisible();
      // The failure stays visible until the next run replaces it.
      expect(screen.getByText("The gateway could not start this video.")).toBeVisible();
    });

    it("runs again only when Run is pressed", async () => {
      const user = await failFirstRun();
      videoResponse = () => json({ id: "video_2", object: "video", status: "queued" });
      await user.click(screen.getByRole("button", { name: "Edit and run again" }));
      await user.click(screen.getByRole("button", { name: "Run" }));
      await waitFor(() => expect(posts()).toHaveLength(2));
      expect(postedBody().input_reference).toMatch(/^data:image\/png/);
    });
  });
});

describe("a workflow catalog that failed to load", () => {
  it("keeps its own labelled retry", async () => {
    fetchMock.mockImplementation(async (url: string) =>
      url.includes("workflows") ? json({}, 500) : json(models),
    );
    const user = userEvent.setup();
    render(<TryPage />);
    await user.click(screen.getByRole("tab", { name: "Workflow" }));
    expect(
      await screen.findByRole("button", { name: "Retry workflow catalog" }),
    ).toBeVisible();
  });
});

it("says the Try key is temporary, in plain words", async () => {
  account.params = new URLSearchParams();
  const user = userEvent.setup();
  render(<TryPage />);
  await waitFor(() =>
    expect(screen.getByRole("combobox")).toHaveTextContent("Chat model"),
  );
  await user.click(screen.getByRole("button", { name: "Run" }));
  expect(await screen.findByText(/Try uses a temporary key/)).toBeVisible();
  expect(screen.queryByText(/Using a Try key/)).toBeNull();
});

function deferredReaders() {
  const readers: Array<{
    result: string | null;
    onload: (() => void) | null;
    onerror: (() => void) | null;
  }> = [];
  vi.stubGlobal("FileReader", class {
    result: string | null = null;
    onload: (() => void) | null = null;
    onerror: (() => void) | null = null;
    readAsDataURL() { readers.push(this); }
  });
  return readers;
}

async function finishRead(
  reader: ReturnType<typeof deferredReaders>[number],
  value: string,
) {
  await act(async () => {
    reader.result = value;
    reader.onload?.();
  });
}

describe("media selection races", () => {
  it.each([false, true])("merges two pending picture reads with current JSON (reverse=%s)", async (reverse) => {
    const readers = deferredReaders();
    const changed = vi.fn();
    function Harness() {
      const [value, setValue] = useState('{"prompt":"before"}');
      return <WorkflowInputs
        inputs={[
          { name: "prompt", type: "string" },
          { name: "first", type: "string", asset: true },
          { name: "second", type: "string", asset: true },
        ]}
        json={value}
        onChange={(next) => { setValue(next); changed(next); }}
      />;
    }
    render(<Harness />);
    const pickers = document.querySelectorAll("input[data-media-file-input]");
    fireEvent.change(pickers[0], { target: { files: [PNG] } });
    fireEvent.change(pickers[1], { target: { files: [PNG] } });
    fireEvent.change(screen.getByRole("textbox", { name: "Prompt" }), {
      target: { value: "latest prompt" },
    });
    const order = reverse ? [1, 0] : [0, 1];
    for (const i of order) await finishRead(readers[i], `data:image/png;base64,picture${i}`);
    expect(JSON.parse(changed.mock.calls.at(-1)![0])).toEqual({
      prompt: "latest prompt",
      first: "data:image/png;base64,picture0",
      second: "data:image/png;base64,picture1",
    });
  });

  it("keeps the newer picture when an older read finishes last", async () => {
    const readers = deferredReaders();
    const changed = vi.fn();
    render(<MediaInput id="picture" label="Picture" kind="image" onChange={changed} />);
    fireEvent.change(pickerInput(), { target: { files: [PNG] } });
    fireEvent.change(pickerInput(), { target: { files: [PNG] } });
    await finishRead(readers[1], "data:image/png;base64,new");
    await finishRead(readers[0], "data:image/png;base64,old");
    expect(changed.mock.calls).toEqual([["data:image/png;base64,new"]]);
  });

  it("a rejected file does not cancel a pending valid picture", async () => {
    const readers = deferredReaders();
    const changed = vi.fn();
    render(<MediaInput id="picture" label="Picture" kind="image" onChange={changed} />);
    fireEvent.change(pickerInput(), { target: { files: [PNG] } });
    const notAPicture = new File(["x"], "notes.txt", { type: "text/plain" });
    fireEvent.change(pickerInput(), { target: { files: [notAPicture] } });
    await finishRead(readers[0], "data:image/png;base64,valid");
    expect(changed.mock.calls).toEqual([["data:image/png;base64,valid"]]);
  });

  it.each(["remove", "link", "unmount"])("invalidates a pending file read on %s", async (action) => {
    const readers = deferredReaders();
    const changed = vi.fn();
    const view = render(<MediaInput id="picture" label="Picture" kind="image"
      value="https://example.test/original.png" onChange={changed} />);
    fireEvent.change(pickerInput(), { target: { files: [PNG] } });
    if (action === "remove") {
      fireEvent.click(screen.getByRole("button", { name: "Remove picture for Picture" }));
    } else if (action === "link") {
      fireEvent.change(screen.getByRole("textbox", { name: "Link, Library link or data URI" }), {
        target: { value: "https://example.test/new.png" },
      });
    } else view.unmount();
    changed.mockClear();
    await finishRead(readers[0], "data:image/png;base64,old");
    expect(changed).not.toHaveBeenCalled();
  });

  it.each(["file", "remove", "unmount"])("invalidates a pending Library download on %s", async (action) => {
    let resolveDownload!: (response: Response) => void;
    const pending = new Promise<Response>((resolve) => { resolveDownload = resolve; });
    fetchMock.mockImplementation((url: string) =>
      url.includes("/download") ? pending : Promise.resolve(json(libraryAssets)));
    const changed = vi.fn();
    const view = render(<MediaInput id="picture" label="Picture" kind="image"
      file={PNG} onFileChange={changed} />);
    fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
    fireEvent.click(await screen.findByRole("button", { name: "A teal fish" }));
    if (action === "file") {
      fireEvent.change(pickerInput(), { target: { files: [PNG] } });
    } else if (action === "remove") {
      fireEvent.click(screen.getByRole("button", { name: "Remove picture for Picture" }));
    } else view.unmount();
    changed.mockClear();
    await act(async () => {
      const response = new Response();
      vi.spyOn(response, "blob").mockResolvedValue(new Blob(["old"], { type: "image/png" }));
      resolveDownload(response);
      await pending;
    });
    expect(changed).not.toHaveBeenCalled();
  });

  it("does not carry a pending picture into another workflow", async () => {
    const readers = deferredReaders();
    const original = catalog.workflows[0];
    fetchMock.mockImplementation(async (url: string) => json(
      url.includes("workflows") ? {
        ...catalog,
        workflows: [original, { ...original, id: "another-workflow", title: "Another workflow" }],
      } : models,
    ));
    await openWorkflow();
    fireEvent.change(pickerInput(), { target: { files: [PNG] } });
    const select = screen.getByRole("combobox");
    select.focus();
    fireEvent.keyDown(select, { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: /Another workflow/ }));
    await waitFor(() => expect(select).toHaveTextContent("Another workflow"));
    await finishRead(readers[0], "data:image/png;base64,old");
    expect(screen.queryByRole("img", { name: "Chosen picture" })).toBeNull();
  });
});

it.each([false, true])("sends the multipart File despite upload strings (edited=%s)", async (edited) => {
  const original = catalog.workflows[0];
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") return videoResponse();
    return json(url.includes("workflows") ? {
      ...catalog,
      workflows: [{
        ...original,
        inputs: [
          { name: "picture", type: "image", request_field: "image", required: true },
          // A text field targeting the same part exercises Run's defensive filter.
          ...(edited ? [{ name: "image", type: "string", request_field: "image" }] : []),
        ],
        example_request: {
          path: "/images/edits", content_type: "multipart/form-data",
          form: { model: "edit-model", image: "example.png" },
        },
      }],
    } : models);
  });
  await openWorkflow();
  fireEvent.change(pickerInput(), { target: { files: [PNG] } });
  if (edited) {
    fireEvent.change(document.getElementById("workflow-input-image")!, {
      target: { value: "must not replace the File" },
    });
  }
  fireEvent.click(screen.getByRole("button", { name: "Run" }));
  await waitFor(() => expect(posts()).toHaveLength(1));
  const body = posts()[0][1].body as FormData;
  expect(body.get("image")).toBeInstanceOf(File);
  expect((body.get("image") as File).name).toBe("boat.png");
  expect(body.getAll("image")).toHaveLength(1);
  expect(body.get("model")).toBe("edit-model");
});

describe("readable Try model choices", () => {
  const raw = "/private/models/Example-Model-Q4_K_M.gguf";
  it.each(["catalog", "fallback", "unverified"])("labels a %s model safely while sending the exact id", async (source) => {
    account.params = new URLSearchParams({ capability: "chat", model: raw });
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") return json({ choices: [{ message: { content: "OK" } }] });
      if (url.includes("/openai/v1/models")) return json({ data: [{ id: raw }] });
      if (url.includes("/models/catalog")) {
        if (source === "fallback") return json({}, 500);
        return json(source === "catalog" ? {
          aliases: [],
          models: [
            { id: raw, name: raw, capabilities: ["chat"], locality: "local" },
            // Duplicate labels must not reintroduce raw ids as disambiguators.
            { id: "/private/models/Other-Q4_K_M.gguf", name: "Example Model", capabilities: ["chat"], locality: "local" },
          ],
        } : models);
      }
      return json(catalog);
    });
    render(<TryPage />);
    await waitFor(() => expect(screen.getByRole("combobox")).toHaveTextContent("Example Model"));
    expect(screen.getByRole("combobox")).not.toHaveTextContent("/private/");
    expect(screen.getByRole("combobox")).not.toHaveTextContent(".gguf");
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(postedBody().model).toBe(raw);
    expect(screen.getByRole("combobox")).toHaveTextContent("Example Model");
    if (source !== "unverified") {
      expect(screen.getByRole("combobox")).not.toHaveTextContent("Unverified model");
    }
  });
});

