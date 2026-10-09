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
import catalog from "./fixtures/workflows-ltx-i2v.json";

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
    // None of that is on the page outside the closed Technical details.
    const technical = screen.getByText("Input details").closest("details")!;
    const page = document.body.cloneNode(true) as HTMLElement;
    page.querySelectorAll("details").forEach((node) => node.remove());
    expect(page.textContent).not.toMatch(
      /Request body|Writes prompt|Writes negative_prompt|Guided controls|Media input:|\bstring\b/,
    );
    expect(technical.textContent).toMatch(/input_reference/);
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
