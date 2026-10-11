import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import TryPage from "./page";
const mocks = vi.hoisted(() => ({
  params: new URLSearchParams(),
  session: vi.fn(),
  mintKey: vi.fn(),
  revokeKey: vi.fn(),
}));
vi.mock("next/navigation", () => ({ useSearchParams: () => mocks.params }));
vi.mock("@/lib/account-api", () => ({
  accountApi: {
    session: mocks.session,
    mintKey: mocks.mintKey,
    revokeKey: mocks.revokeKey,
  },
}));
const response = (body: unknown, ok = true) =>
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
const workflows = {
  workflows: [
    { id: "wf-a", title: "Working workflow", inputs: [], outputs: [] },
  ],
};
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
  mocks.params = new URLSearchParams();
  vi.stubGlobal("fetch", fetchMock);
  fetchMock
    .mockReset()
    .mockImplementation(async (url: string) =>
      response(url.includes("workflows") ? workflows : models),
    );
  mocks.session.mockResolvedValue({
    authenticated: true,
    apps: [{ id: "app", name: "Try" }],
  });
  mocks.mintKey.mockResolvedValue({ id: "key", api_key: "test-secret" });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});
test.each([false, true])(
  "recording retry does not mutate credentials (key present: %s)",
  async (withKey) => {
    mocks.params = new URLSearchParams("capability=transcribe");
    const getUserMedia = vi
      .fn()
      .mockRejectedValue(new Error("Permission denied"));
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia },
    });
    render(<TryPage />);
    await screen.findByRole("button", { name: "Record" });
    if (withKey) {
      fireEvent.click(screen.getByRole("button", { name: "Run" }));
      await screen.findByText(/Try uses a temporary key/);
    }
    const minted = mocks.mintKey.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    const alert = await screen.findByText(/Recording is not available/);
    fireEvent.click(alert.closest('[role="alert"]')!.querySelector("button")!);
    await waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(2));
    expect(mocks.mintKey).toHaveBeenCalledTimes(minted);
    expect(mocks.revokeKey).not.toHaveBeenCalled();
  },
);
test("equal labels retain each selectable model ID", async () => {
  fetchMock.mockImplementation(async (url: string) =>
    response(
      url.includes("workflows")
        ? workflows
        : {
            aliases: [],
            models: ["provider/a", "provider/b"].map((id) => ({
              id,
              name: "Shared name",
              capabilities: ["chat"],
              provider: "Provider",
              locality: "remote",
            })),
          },
    ),
  );
  render(<TryPage />);
  await waitFor(() =>
    expect(screen.getByRole("combobox")).toHaveTextContent("Shared name"),
  );
  await userEvent.click(screen.getByRole("combobox"));
  expect(
    screen
      .getAllByRole("option")
      .filter((option) => option.textContent?.includes("Shared name")),
  ).toHaveLength(2);
  expect(screen.getByRole("option", { name: /Provider B/ })).toBeVisible();
});

test("fallback model list keeps capabilities and leaves unknown rows unclassified", async () => {
  fetchMock.mockImplementation(async (url: string) => {
    if (url.includes("workflows")) return response({ workflows: [] });
    if (url === "/me/api/models/catalog") return response({}, false);
    if (url === "/openai/v1/models") return response({ data: [
      { id: "new-chat", capability: "chat" },
      { id: "new-picture", capability: "image" },
      {
        id: "new-speech",
        capability: "speech",
        speech_voices: {
          voices: [{ id: "new-voice", name: "New voice" }],
          default_voice: "new-voice",
          allow_other: false,
        },
      },
      { id: "new-unknown" },
    ] });
    return response({ choices: [{ message: { content: "ok" } }] });
  });
  render(<TryPage />);
  await screen.findByText("Model catalog could not be loaded.");
  fireEvent.click(screen.getByRole("button", { name: "Run" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/openai/v1/models", expect.anything(),
  ));
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("NEW Chat");
  expect(screen.getByRole("combobox", { name: "Model" })).not.toHaveTextContent("NEW Picture");
  expect(screen.getByText(/1 model has no supported Try capability/)).toBeVisible();
  await userEvent.click(screen.getByRole("tab", { name: "Make a picture" }));
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("NEW Picture");
  expect(screen.getByRole("combobox", { name: "Model" })).not.toHaveTextContent("NEW Unknown");
  await userEvent.click(screen.getByRole("tab", { name: "Read text aloud" }));
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("NEW Speech");
  expect(screen.getByRole<HTMLSelectElement>("combobox", { name: "Voice" }).value).toBe("new-voice");
});

test("fallback does not send an unknown model as chat", async () => {
  fetchMock.mockImplementation(async (url: string) => {
    if (url.includes("workflows")) return response({ workflows: [] });
    if (url === "/me/api/models/catalog") return response({}, false);
    if (url === "/openai/v1/models") return response({ data: [{ id: "new-unknown" }] });
    return response({});
  });
  render(<TryPage />);
  await screen.findByText("Model catalog could not be loaded.");
  fireEvent.click(screen.getByRole("button", { name: "Run" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/openai/v1/models", expect.anything(),
  ));
  expect(screen.getByRole("combobox", { name: "Model" })).toBeDisabled();
  expect(fetchMock.mock.calls.some(([url]) => url === "/openai/v1/chat/completions")).toBe(false);
});

test.each(["unknown", "speech"])(
  "first Run does not send a selected chat model reclassified as %s",
  async (capability) => {
    let catalogReads = 0;
    fetchMock.mockImplementation(async (url: string) => {
      if (url.includes("workflows")) return response({ workflows: [] });
      if (url === "/me/api/models/catalog") {
        catalogReads += 1;
        return catalogReads === 1
          ? response({ aliases: [], models: [
              { id: "first-chat", name: "First chat", capability: "chat" },
              { id: "second-chat", name: "Second chat", capability: "chat" },
            ] })
          : response({}, false);
      }
      if (url === "/openai/v1/models")
        return response({ data: [
          capability === "speech"
            ? { id: "second-chat", capability: "speech" }
            : { id: "second-chat" },
        ] });
      return response({});
    });
    render(<TryPage />);
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("First chat"));
    await userEvent.click(screen.getByRole("combobox", { name: "Model" }));
    await userEvent.click(screen.getByRole("option", { name: "Second chat" }));
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Second chat");
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toBeDisabled());
    expect(fetchMock.mock.calls.some(([url]) => url === "/openai/v1/chat/completions")).toBe(false);
  },
);

test.each(["http", "transport"])(
  "workflow %s failure offers retry while model options survive",
  async (kind) => {
    fetchMock.mockImplementation(async (url: string) => {
      if (!url.includes("workflows")) return response(models);
      if (kind === "transport") throw new Error("Network failed");
      return response({}, false);
    });
    render(<TryPage />);
    await waitFor(() =>
      expect(screen.getByRole("combobox")).toHaveTextContent("Chat model"),
    );
    await userEvent.click(screen.getByRole("tab", { name: "Workflow" }));
    expect(
      await screen.findByText("Workflow catalog could not be loaded."),
    ).toBeVisible();
    fetchMock.mockImplementation(async () => response(workflows));
    fireEvent.click(
      screen.getByRole("button", { name: "Retry workflow catalog" }),
    );
    await waitFor(() =>
      expect(screen.getByRole("combobox")).toHaveTextContent(
        "Working workflow",
      ),
    );
    expect(
      fetchMock.mock.calls.filter(([url]) => url === "/me/api/models/catalog"),
    ).toHaveLength(1);
  },
);
test.each(["http", "transport"])(
  "healthy workflows survive model %s failure",
  async (kind) => {
    mocks.params = new URLSearchParams("capability=workflow");
    fetchMock.mockImplementation(async (url: string) => {
      if (url.includes("workflows")) return response(workflows);
      if (kind === "transport") throw new Error("Network failed");
      return response({}, false);
    });
    render(<TryPage />);
    await waitFor(() =>
      expect(screen.getByRole("combobox")).toHaveTextContent(
        "Working workflow",
      ),
    );
    await userEvent.click(screen.getByRole("tab", { name: "Chat" }));
    expect(
      await screen.findByText("Model catalog could not be loaded."),
    ).toBeVisible();
    fetchMock.mockImplementation(async () => response(models));
    fireEvent.click(
      screen.getByRole("button", { name: "Retry model catalog" }),
    );
    await waitFor(() =>
      expect(screen.getByRole("combobox")).toHaveTextContent("Chat model"),
    );
    expect(
      fetchMock.mock.calls.filter(([url]) => url === "/me/api/workflows"),
    ).toHaveLength(1);
  },
);
test("catalogs start concurrently and workflows render before models settle", async () => {
  let finishModels!: (value: Response) => void;
  fetchMock.mockImplementation((url: string) =>
    url.includes("workflows")
      ? Promise.resolve(response(workflows))
      : new Promise<Response>((resolve) => {
          finishModels = resolve;
        }),
  );
  mocks.params = new URLSearchParams("capability=workflow");
  render(<TryPage />);
  await waitFor(() =>
    expect(screen.getByRole("combobox")).toHaveTextContent("Working workflow"),
  );
  expect(fetchMock).toHaveBeenCalledTimes(2);
  await act(async () => finishModels(response(models)));
});
test("navigation aborts old catalogs and ignores late successful responses", async () => {
  let finishOldModels!: (value: Response) => void;
  let finishOldWorkflows!: (value: Response) => void;
  fetchMock
    .mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          finishOldModels = resolve;
        }),
    )
    .mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          finishOldWorkflows = resolve;
        }),
    );
  const view = render(<TryPage />);
  const signals = fetchMock.mock.calls
    .slice(0, 2)
    .map(([, options]) => options.signal as AbortSignal);
  mocks.params = new URLSearchParams("workflow=wf-a");
  view.rerender(<TryPage />);
  await waitFor(() =>
    expect(screen.getByRole("combobox")).toHaveTextContent("Working workflow"),
  );
  expect(signals.every((signal) => signal.aborted)).toBe(true);
  await act(async () => {
    finishOldModels(
      response({
        aliases: [],
        models: [{ id: "stale", name: "Stale model", capabilities: ["chat"] }],
      }),
    );
    finishOldWorkflows(
      response({
        workflows: [{ id: "stale-workflow", title: "Stale workflow" }],
      }),
    );
  });
  expect(screen.getByRole("combobox")).toHaveTextContent("Working workflow");
  await userEvent.click(screen.getByRole("tab", { name: "Chat" }));
  expect(screen.getByRole("combobox")).toHaveTextContent("Chat model");
  view.unmount();
  expect(
    fetchMock.mock.calls
      .slice(2, 4)
      .every(([, options]) => options.signal.aborted),
  ).toBe(true);
});
test("empty workflow catalog disables the selector and Run", async () => {
  mocks.params = new URLSearchParams("capability=workflow");
  fetchMock.mockImplementation(async (url: string) =>
    response(url.includes("workflows") ? { workflows: [] } : models),
  );
  render(<TryPage />);
  expect(await screen.findByText("No workflows are listed")).toBeVisible();
  expect(screen.getByRole("combobox")).toBeDisabled();
  expect(screen.getByRole("combobox")).toHaveTextContent(
    "No options available",
  );
  expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
});
