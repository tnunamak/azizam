import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import TryPage from "@/app/try/page";
import { ViewerContext, type Viewer } from "@/components/shell/session";
import { unsavedWorkNow } from "@/lib/unsaved-work";
import catalog from "./fixtures/workflows-image-edit.json";

// Two picture-edit workflows with the same input names ("prompt", "source_image", "mask"). Leaving one
// of them with unsent words or an attached picture is unsent work, and each keeps its own pictures.
const first = catalog.workflows[0];
const second = { ...first, id: "image.edit.second", short_id: "second-edit", title: "Second edit" };

const HISTORY_ON = {
  keep: true,
  retention: "7_days",
  parts: ["accepted"],
  choices: ["1_day", "7_days", "30_days"],
  allowed_parts: ["accepted", "received"],
  notice_due: false,
};

const account = vi.hoisted(() => ({
  session: vi.fn(),
  tryKey: vi.fn(),
  revokeKey: vi.fn(),
  promptMemory: vi.fn(),
  requestHistory: vi.fn(),
  params: new URLSearchParams(),
}));
vi.mock("next/navigation", () => ({ useSearchParams: () => account.params }));
vi.mock("@/lib/account-api", () => ({ accountApi: account }));

const member: Viewer = {
  authenticated: true,
  name: "Robin",
  email: "robin@example.test",
  role: "member",
  admin: false,
};
const PNG_BYTES = new Uint8Array([137, 80, 78, 71]);
const PNG = new File([PNG_BYTES], "garden.png", { type: "image/png" });
const SKY = new File([PNG_BYTES], "sky.png", { type: "image/png" });

const json = (body: unknown) =>
  new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
// The picture a successful run answers with: one pixel, as the picture-edit route returns it.
const PIXEL_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==";
const bytes = (text: string, type: string) =>
  new Response(new Blob([text], { type }), { headers: { "content-type": type } });

beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, "hasPointerCapture", { configurable: true, value: () => false });
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: () => {} });
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  account.params = new URLSearchParams({ workflow: first.id });
  account.session.mockResolvedValue({ authenticated: true, apps: [{ id: "try", name: "Try" }] });
  account.tryKey.mockResolvedValue({ id: "try-key", api_key: "test-key", reused: false, app_name: "Try", expires_at: 0 });
  account.promptMemory.mockResolvedValue({
    prompt_memory: {
      remember: true,
      retention: "never",
      effective_remember: true,
      effective_retention: "never",
      household: { default_remember: true, never_keep: false, retention_cap: "never" },
    },
  });
  account.requestHistory.mockResolvedValue({ ...HISTORY_ON });
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: () => "blob:result" });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: () => {} });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      // A run answers with its picture, as the picture-edit route does.
      if (init?.method === "POST") return bytes(atob(PIXEL_PNG), "image/png");
      return json(url.includes("workflows") ? { workflows: [first, second] } : { aliases: [], models: [] });
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

async function openTry() {
  render(
    <ViewerContext.Provider value={member}>
      <TryPage />
    </ViewerContext.Provider>,
  );
  await waitFor(() => expect(combobox()).toHaveTextContent(first.title));
}

const combobox = () => screen.getByRole("combobox", { name: "Model" });
const description = () => screen.getByRole("textbox", { name: /Describe the picture|^Description/ });
const picturePlace = () => screen.getByRole("group", { name: /Picture to change/ });

async function switchTo(user: ReturnType<typeof userEvent.setup>, title: string) {
  await user.click(combobox());
  await user.click(await screen.findByRole("option", { name: title }));
  await waitFor(() => expect(combobox()).toHaveTextContent(title));
}

async function attach(user: ReturnType<typeof userEvent.setup>, picture: File) {
  const input = picturePlace().querySelector<HTMLInputElement>("input[data-media-file-input]")!;
  await user.upload(input, picture);
  expect(await within(picturePlace()).findByText(picture.name)).toBeVisible();
}

/** The unsaved work the leave guard would ask about, as kind and count. */
function unsentWork() {
  return unsavedWorkNow().map(({ kind, count }) => ({ kind, count }));
}

describe("unsent work in a workflow the person has left", () => {
  it("counts an edited workflow as unsent once the person moves to an untouched one", async () => {
    const user = userEvent.setup();
    await openTry();
    fireEvent.change(description(), { target: { value: "Make the sky purple" } });
    await switchTo(user, second.title);
    expect(unsentWork()).toEqual([{ kind: "try-draft", count: 1 }]);
  });

  it("counts a picture attached to a workflow, on screen or left behind", async () => {
    const user = userEvent.setup();
    await openTry();
    await attach(user, PNG);
    expect(unsentWork()).toEqual([{ kind: "try-draft", count: 1 }]);
    await switchTo(user, second.title);
    expect(unsentWork()).toEqual([{ kind: "try-draft", count: 1 }]);
  });

  it("counts nothing when the workflows the person left were never changed", async () => {
    const user = userEvent.setup();
    await openTry();
    await switchTo(user, second.title);
    expect(unsentWork()).toEqual([]);
  });
});

describe("returning to a workflow", () => {
  it("restores its words and its attached picture, and still counts them as unsent", async () => {
    const user = userEvent.setup();
    await openTry();
    fireEvent.change(description(), { target: { value: "Make the sky purple" } });
    await attach(user, PNG);
    await switchTo(user, second.title);
    expect(unsentWork()).toEqual([{ kind: "try-draft", count: 1 }]);
    await switchTo(user, first.title);
    expect(description()).toHaveValue("Make the sky purple");
    expect(within(picturePlace()).getByText("garden.png")).toBeVisible();
    expect(unsentWork()).toEqual([{ kind: "try-draft", count: 1 }]);
  });
});

describe("workflows with the same input names", () => {
  it("never share a picture between them", async () => {
    const user = userEvent.setup();
    await openTry();
    await attach(user, PNG);
    await switchTo(user, second.title);
    expect(within(picturePlace()).queryByText("garden.png")).toBeNull();
    await attach(user, SKY);
    await switchTo(user, first.title);
    expect(within(picturePlace()).getByText("garden.png")).toBeVisible();
    expect(within(picturePlace()).queryByText("sky.png")).toBeNull();
    await switchTo(user, second.title);
    expect(within(picturePlace()).getByText("sky.png")).toBeVisible();
    expect(within(picturePlace()).queryByText("garden.png")).toBeNull();
  });
});

/** Types a request and runs it. The run succeeds, so the words and the picture it sent become the base. */
async function runWithPicture(user: ReturnType<typeof userEvent.setup>) {
  fireEvent.change(description(), { target: { value: "Make the sky purple" } });
  await user.click(screen.getByRole("button", { name: /^Run/ }));
  expect(await screen.findByText("Workflow result")).toBeVisible();
}

describe("the picture a workflow last ran with", () => {
  it("is not unsent after a successful run, on screen or once the person leaves", async () => {
    const user = userEvent.setup();
    await openTry();
    await attach(user, PNG);
    await runWithPicture(user);
    expect(unsentWork()).toEqual([]);
    await switchTo(user, second.title);
    expect(unsentWork()).toEqual([]);
  });

  it("is unsent again once that picture is replaced after the run", async () => {
    const user = userEvent.setup();
    await openTry();
    await attach(user, PNG);
    await runWithPicture(user);
    await attach(user, SKY);
    expect(unsentWork()).toEqual([{ kind: "try-draft", count: 1 }]);
  });

  it("is unsent again once that picture is removed after the run", async () => {
    const user = userEvent.setup();
    await openTry();
    await attach(user, PNG);
    await runWithPicture(user);
    await user.click(within(picturePlace()).getByRole("button", { name: /Remove picture/ }));
    expect(within(picturePlace()).queryByText("garden.png")).toBeNull();
    expect(unsentWork()).toEqual([{ kind: "try-draft", count: 1 }]);
  });

  it("comes back with the picture it ran with, and is not unsent, when the person returns to it", async () => {
    const user = userEvent.setup();
    await openTry();
    await attach(user, PNG);
    await runWithPicture(user);
    await switchTo(user, second.title);
    await switchTo(user, first.title);
    expect(within(picturePlace()).getByText("garden.png")).toBeVisible();
    expect(unsentWork()).toEqual([]);
  });

  it("is still unsent when a picture is attached and the person leaves without running it", async () => {
    const user = userEvent.setup();
    await openTry();
    await attach(user, PNG);
    await switchTo(user, second.title);
    expect(unsentWork()).toEqual([{ kind: "try-draft", count: 1 }]);
  });
});
