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
import { ViewerContext } from "@/components/shell/session";
const mocks = vi.hoisted(() => ({
  params: new URLSearchParams(),
  session: vi.fn(),
  tryKey: vi.fn(),
  revokeKey: vi.fn(),
  promptMemory: vi.fn().mockResolvedValue({ prompt_memory: null }),
}));
vi.mock("next/navigation", () => ({ useSearchParams: () => mocks.params }));
vi.mock("@/lib/account-api", () => ({
  accountApi: {
    session: mocks.session,
    tryKey: mocks.tryKey,
    revokeKey: mocks.revokeKey,
    promptMemory: mocks.promptMemory,
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
  mocks.promptMemory.mockResolvedValue({ prompt_memory: null });
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
test("names the page with exactly one level-one heading", async () => {
  render(<TryPage />);
  expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  expect(screen.getByRole("heading", { level: 1, name: "Try" })).toBeVisible();
});

test("Try names the signed-in account's plan, ignores a plan in the URL, and refreshes access", async () => {
  mocks.params = new URLSearchParams("plan=plan-other");
  let planName = "Family";
  fetchMock.mockImplementation(async (url: string) => response(
    url.includes("workflows") ? workflows : {
      ...models, access_plan: { id: "plan-account", name: planName },
    },
  ));
  render(
    <ViewerContext.Provider value={{ authenticated: true, name: "River", email: null, role: "owner", admin: true }}>
      <TryPage />
    </ViewerContext.Provider>,
  );
  expect(await screen.findByText("Your access plan: Family.")).toBeVisible();
  expect(screen.getByText("Signed in as River.")).toBeVisible();
  expect(screen.getByRole("link", { name: "Check another plan" })).toHaveAttribute("href", "/people?tab=plans");
  expect(screen.getByText(/Checking another plan does not change your access here/)).toBeVisible();
  planName = "Everything and categories";
  fireEvent.click(screen.getByRole("button", { name: "Refresh access" }));
  expect(await screen.findByText("Your access plan: Everything and categories.")).toBeVisible();
  expect(mocks.tryKey).not.toHaveBeenCalled();
});

test.each([null, undefined])("Try distinguishes no assigned plan from an unconfirmed plan (%s)", async (plan) => {
  fetchMock.mockImplementation(async (url: string) => response(
    url.includes("workflows") ? workflows : { ...models, ...(plan === null ? { access_plan: null } : {}) },
  ));
  render(
    <ViewerContext.Provider value={{ authenticated: true, name: "River", email: null, role: "member", admin: false }}>
      <TryPage />
    </ViewerContext.Provider>,
  );
  expect(await screen.findByText(plan === null
    ? "No access plan is assigned to your account. Ask the owner."
    : "Your access plan could not be confirmed.")).toBeVisible();
  expect(screen.queryByRole("link", { name: "Check another plan" })).toBeNull();
});

test.each([false, true])(
  "recording retry does not mutate credentials (key present: %s)",
  async (withKey) => {
    mocks.params = new URLSearchParams("capability=transcribe");
    fetchMock.mockImplementation(async (url: string) =>
      response(
        url.includes("workflows")
          ? workflows
          : {
              aliases: [],
              models: [{ id: "stt-a", name: "Words", capabilities: ["transcribe"] }],
            },
      ),
    );
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
      await waitFor(() =>
        expect(screen.getByRole("button", { name: "Run" })).toBeEnabled(),
      );
      fireEvent.click(screen.getByRole("button", { name: "Run" }));
      await screen.findByText("Key details");
    }
    const minted = mocks.tryKey.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    const alert = await screen.findByText(/Recording is not available/);
    fireEvent.click(alert.closest('[role="alert"]')!.querySelector("button")!);
    await waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(2));
    expect(mocks.tryKey).toHaveBeenCalledTimes(minted);
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
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Shared name"),
  );
  await userEvent.click(screen.getByRole("combobox", { name: "Model" }));
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
  await screen.findByText("We couldn’t tell why the model list didn’t load.");
  // Run waits for a list. Retry prepares the key and reads the fallback.
  expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/openai/v1/models", expect.anything(),
  ));
  await waitFor(() =>
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("NEW Chat"),
  );
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
  await screen.findByText("We couldn’t tell why the model list didn’t load.");
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/openai/v1/models", expect.anything(),
  ));
  await screen.findByText(/No chat models are available/);
  expect(screen.getByRole("combobox", { name: "Model" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
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

test("a missing catalog leaves no invented model selected", async () => {
  fetchMock.mockImplementation(async (url: string) =>
    response(url.includes("workflows") ? workflows : {}, !url.includes("/me/api/models/catalog")),
  );
  render(<TryPage />);
  await screen.findByText("We couldn’t tell why the model list didn’t load.");
  expect(screen.getByRole("combobox", { name: "Model" })).toBeDisabled();
  expect(screen.queryByText(/Unverified model/)).toBeNull();
});

test("speech refresh uses the replacement model and its discovered voice", async () => {
  mocks.params = new URLSearchParams("capability=speech");
  let catalogReads = 0;
  fetchMock.mockImplementation(async (url: string) => {
    if (url.includes("workflows")) return response({ workflows: [] });
    if (url === "/me/api/models/catalog") {
      const next = ++catalogReads > 1;
      return response({ aliases: [], models: [{
        id: next ? "new-speech-b" : "new-speech-a",
        name: next ? "Second voice model" : "First voice model",
        capability: "speech",
        speech_voices: {
          voices: [{ id: next ? "novel-b" : "novel-a", name: next ? "Voice B" : "Voice A" }],
          default_voice: next ? "novel-b" : "novel-a",
          allow_other: false,
        },
      }] });
    }
    return response({});
  });
  render(<TryPage />);
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("First voice model"));
  expect(screen.getByRole<HTMLSelectElement>("combobox", { name: "Voice" }).value).toBe("novel-a");
  fireEvent.click(screen.getByRole("button", { name: "Run" }));
  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => url === "/openai/v1/audio/speech")).toBe(true));
  const call = fetchMock.mock.calls.find(([url]) => url === "/openai/v1/audio/speech");
  expect(JSON.parse((call?.[1] as RequestInit).body as string)).toMatchObject({ model: "new-speech-b", voice: "novel-b" });
});
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
      expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Chat model"),
    );
    await userEvent.click(screen.getByRole("tab", { name: "Workflow" }));
    expect(
      await screen.findByText("We couldn’t tell why the workflow list didn’t load."),
    ).toBeVisible();
    fetchMock.mockImplementation(async () => response(workflows));
    fireEvent.click(
      screen.getByRole("button", { name: "Retry" }),
    );
    await waitFor(() =>
      expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent(
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
      expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent(
        "Working workflow",
      ),
    );
    await userEvent.click(screen.getByRole("tab", { name: "Chat" }));
    expect(
      await screen.findByText("We couldn’t tell why the model list didn’t load."),
    ).toBeVisible();
    fetchMock.mockImplementation(async () => response(models));
    fireEvent.click(
      screen.getByRole("button", { name: "Retry" }),
    );
    await waitFor(() =>
      expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Chat model"),
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
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Working workflow"),
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
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Working workflow"),
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
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Working workflow");
  await userEvent.click(screen.getByRole("tab", { name: "Chat" }));
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Chat model");
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
  expect(
    await screen.findByText("No workflows are available. Ask the owner."),
  ).toBeVisible();
  expect(screen.getByRole("combobox", { name: "Model" })).toBeDisabled();
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent(
    "No options available",
  );
  expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
});

test("an empty decision catalog has no inferred model and cannot run", async () => {
  mocks.params = new URLSearchParams("capability=decide");
  fetchMock.mockImplementation(async (url: string) =>
    response(url.includes("workflows") ? { workflows: [] } : { aliases: [], models: [] }),
  );
  render(<TryPage />);
  expect(await screen.findByText(/No decision models are available/)).toBeVisible();
  expect(screen.getByRole("tab", { name: "Choose an option" })).toHaveAttribute("aria-selected", "true");
  expect(screen.getByRole("combobox", { name: "Model" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
  expect(screen.queryByRole("link", { name: "View receipt" })).toBeNull();
  expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
});

// Run is disabled until the model list has loaded, and a click on a disabled
// button does nothing. Wait for it to be enabled, which is the event that
// the list arrived, before pressing it.

function chatReply(body: unknown, requestId?: string) {
  return {
    ...response(body),
    headers: new Headers({
      "content-type": "application/json",
      ...(requestId ? { "x-request-id": requestId } : {}),
    }),
  } as Response;
}
function serveChat(reply: () => Response | Promise<Response>) {
  fetchMock.mockImplementation(async (url: string) => {
    if (url === "/me/api/models/catalog") return response(models);
    if (url === "/me/api/workflows") return response(workflows);
    if (url.startsWith("/me/api/receipts/")) return response({});
    return reply();
  });
}
test.each([
  [new SyntaxError("invalid json"), "The gateway sent a reply we could not read. Try again."],
  [new TypeError("Failed to fetch"), "The answer did not come back. Edit and run again when you are back online."],
])("a rejected chat body is not an empty answer (%s)", async (error, message) => {
  serveChat(() => ({ ...chatReply(null), json: async () => { throw error; } }) as Response);
  render(<TryPage />);
  await pressRun();
  expect(await screen.findByText(message)).toBeVisible();
  expect(screen.queryByText("The gateway returned no reply.")).toBeNull();
});
test.each([
  ["length", "This answer stopped early because it reached its length limit."],
  ["content_filter", "This answer was cut short by a content filter."],
  ["stop", null],
  ["tool_calls", null],
  [undefined, null],
])("chat finish reason %s is shown truthfully", async (finish_reason, note) => {
  serveChat(() => chatReply({ choices: [{ message: { content: "Partial answer" }, finish_reason }] }));
  render(<TryPage />);
  await pressRun();
  expect(await screen.findByText("Partial answer")).toBeVisible();
  if (note) expect(screen.getByText(note)).toBeVisible();
  else expect(screen.queryByText(/This answer (stopped early|was cut short)/)).toBeNull();
});
test("valid JSON without choices still shows no reply", async () => {
  serveChat(() => chatReply({}));
  render(<TryPage />);
  await pressRun();
  expect(await screen.findByText("The gateway returned no reply.")).toBeVisible();
});
test("Copy answer copies the body and labels request snippets", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
  serveChat(() => chatReply({ choices: [{ message: { content: "Copy these words" } }] }));
  render(<TryPage />);
  await pressRun();
  fireEvent.click(await screen.findByRole("button", { name: "Copy answer" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith("Copy these words"));
  expect(await screen.findByRole("button", { name: "Copied" })).toBeVisible();
  expect(screen.getByText("Copy request as")).toBeVisible();
});
test("pending receipts do not hide answers or attach to a newer run", async () => {
  let finishOld!: (value: Response) => void;
  let runs = 0;
  fetchMock.mockImplementation(async (url: string) => {
    if (url === "/me/api/models/catalog") return response(models);
    if (url === "/me/api/workflows") return response(workflows);
    if (url === "/me/api/receipts/old") return new Promise<Response>((resolve) => { finishOld = resolve; });
    if (url === "/me/api/receipts/new") return response({});
    const id = ++runs === 1 ? "old" : "new";
    return chatReply({ choices: [{ message: { content: `Answer ${id}` } }] }, id);
  });
  render(<TryPage />);
  await pressRun();
  expect(await screen.findByText("Answer old")).toBeVisible();
  expect(screen.getByText("Checking receipt...")).toBeVisible();
  await pressRun();
  expect(await screen.findByText("Answer new")).toBeVisible();
  await waitFor(() => expect(screen.getByRole("link", { name: /receipt/i })).toHaveAttribute("href", expect.stringContaining("new")));
  await act(async () => finishOld(response({})));
  expect(screen.getByRole("link", { name: /receipt/i })).toHaveAttribute("href", expect.stringContaining("new"));
});
test("receipt lookup times out after ten seconds and can be retried", async () => {
  let receiptReads = 0;
  serveChat(() => chatReply({ choices: [{ message: { content: "Ready answer" } }] }, "slow"));
  fetchMock.mockImplementation(async (url: string) => {
    if (url === "/me/api/models/catalog") return response(models);
    if (url === "/me/api/workflows") return response(workflows);
    if (url.startsWith("/me/api/receipts/")) {
      receiptReads += 1;
      return receiptReads === 1 ? new Promise<Response>(() => {}) : response({});
    }
    return chatReply({ choices: [{ message: { content: "Ready answer" } }] }, "slow");
  });
  render(<TryPage />);
  await waitFor(() => expect(screen.getByRole("button", { name: "Run" })).toBeEnabled());
  vi.useFakeTimers();
  try {
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Run" })));
    expect(screen.getByText("Ready answer")).toBeVisible();
    await act(async () => vi.advanceTimersByTimeAsync(10_000));
    expect(screen.getByRole("button", { name: "Check again" })).toBeEnabled();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Check again" })));
    expect(screen.getByRole("link", { name: /receipt/i })).toBeVisible();
  } finally {
    vi.useRealTimers();
  }
});
test("recorded mp4 uploads its real type and filename", async () => {
  mocks.params = new URLSearchParams("capability=transcribe");
  const stt = { aliases: [], models: [{ id: "stt", name: "Words", capability: "transcribe" }] };
  fetchMock.mockImplementation(async (url: string) => {
    if (url === "/me/api/models/catalog") return response(stt);
    if (url === "/me/api/workflows") return response(workflows);
    return chatReply({ text: "Recorded words" });
  });
  const original = Object.getOwnPropertyDescriptor(navigator, "mediaDevices");
  const constructed = vi.fn();
  class FakeRecorder {
    static isTypeSupported = (type: string) => type === "audio/mp4";
    mimeType = "audio/mp4";
    state = "inactive";
    ondataavailable?: (event: { data: Blob }) => void;
    onstop?: () => void;
    constructor(_stream: MediaStream, options?: MediaRecorderOptions) { constructed(options); }
    start() { this.state = "recording"; }
    stop() {
      this.state = "inactive";
      this.ondataavailable?.({ data: new Blob(["sound"], { type: this.mimeType }) });
      this.onstop?.();
    }
  }
  vi.stubGlobal("MediaRecorder", FakeRecorder);
  Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia: async () => ({ getTracks: () => [{ stop: vi.fn() }] }) } });
  const view = render(<TryPage />);
  try {
    fireEvent.click(await screen.findByRole("button", { name: "Record" }));
    fireEvent.click(await screen.findByRole("button", { name: /Stop/ }));
    await pressRun();
    expect(await screen.findByText("Recorded words")).toBeVisible();
    const call = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    const uploaded = (call?.[1].body as FormData).get("file") as File;
    expect(uploaded.type).toBe("audio/mp4");
    expect(uploaded.name).toMatch(/\.(m4a|mp4)$/);
    expect(constructed).toHaveBeenCalledWith({ mimeType: "audio/mp4" });
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    fireEvent.click(screen.getByRole("button", { name: "Copy answer" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("Recorded words"));
  } finally {
    view.unmount();
    if (original) Object.defineProperty(navigator, "mediaDevices", original);
    else Reflect.deleteProperty(navigator, "mediaDevices");
  }
});
test("a workflow removed during key refresh never posts its stale request", async () => {
  mocks.params = new URLSearchParams("capability=workflow");
  let reads = 0;
  fetchMock.mockImplementation(async (url: string) => {
    if (url === "/me/api/workflows") {
      const item = { id: "wf-a", title: "Working workflow", inputs: [], outputs: [],
        example_request: { path: "/v1/workflow-test", body: {} } };
      return response({ workflows: ++reads === 1 ? [item] : [{ ...item, id: "wf-b", title: "Replacement workflow" }] });
    }
    if (url === "/me/api/models/catalog") return response(models);
    return chatReply({});
  });
  render(<TryPage />);
  await pressRun();
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Replacement workflow"));
  await waitFor(() => expect(screen.getByRole("button", { name: "Run" })).toBeEnabled());
  expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
});
async function pressRun() {
  const run = await screen.findByRole("button", { name: "Run" });
  await waitFor(() => expect(run).toBeEnabled());
  fireEvent.click(run);
}
function bearerOfLastRun() {
  const call = fetchMock.mock.calls.findLast(
    ([, init]) => init?.method === "POST",
  );
  return new Headers(call?.[1]?.headers).get("authorization");
}
test("a later visit mints a fresh Try key because the secret stays in page memory", async () => {
  const first = render(<TryPage />);
  await pressRun();
  await screen.findByText("Key details");
  expect(mocks.tryKey).toHaveBeenLastCalledWith(null);
  await waitFor(() => expect(bearerOfLastRun()).toBe("Bearer test-secret"));
  first.unmount();
  expect(localStorage.getItem("azizam.try.key")).toBeNull();

  fetchMock.mockClear();
  render(<TryPage />);
  await pressRun();
  await screen.findByText("Key details");
  expect(mocks.tryKey).toHaveBeenLastCalledWith(null);
  await waitFor(() => expect(bearerOfLastRun()).toBe("Bearer test-secret"));
});
test("an old stored secret is removed unused and Remove this key ends the page key", async () => {
  localStorage.setItem(
    "azizam.try.key",
    JSON.stringify({ id: "old", secret: "old-secret", appName: "Try" }),
  );
  render(<TryPage />);
  expect(localStorage.getItem("azizam.try.key")).toBeNull();
  await pressRun();
  await screen.findByText("Key details");
  expect(mocks.tryKey).toHaveBeenLastCalledWith(null);
  await waitFor(() => expect(bearerOfLastRun()).toBe("Bearer test-secret"));
  fireEvent.click(screen.getByText("Key details"));
  fireEvent.click(screen.getByRole("button", { name: "Remove this key" }));
  await waitFor(() => expect(mocks.revokeKey).toHaveBeenCalledWith("key"));
  expect(localStorage.getItem("azizam.try.key")).toBeNull();
});

// WP-5: a link names a task or a kind of work; Try opens that, or says why it
// cannot, and never quietly opens Chat (PF-001).
const kindWorkflows = {
  workflows: [
    { id: "wf-song", title: "Song maker", capability: "music", aliases: ["song"], inputs: [], outputs: [{ kind: "audio" }] },
    { id: "wf-boom", title: "Boom maker", capability: "sfx", inputs: [], outputs: [{ kind: "audio" }] },
    { id: "wf-pic", title: "Picture tool", capability: "image", inputs: [], outputs: [{ kind: "image" }] },
  ],
};
function serveKinds() {
  fetchMock.mockImplementation(async (url: string) =>
    response(url.includes("workflows") ? kindWorkflows : models),
  );
}
test.each([
  ["music", "Song maker", "Boom maker", "Showing music workflows."],
  ["sfx", "Boom maker", "Song maker", "Showing sound effects workflows."],
  ["audio", "Song maker", "Picture tool", "Choose an audio workflow to make music or sound effects."],
])("a %s link opens only those workflows", async (kind, shown, hidden, line) => {
  mocks.params = new URLSearchParams({ capability: kind, model: "song" });
  serveKinds();
  render(<TryPage />);
  await screen.findByText(line);
  expect(screen.getByRole("tab", { name: "Workflow" })).toHaveAttribute("aria-selected", "true");
  const picker = screen.getByRole("combobox", { name: "Model" });
  expect(picker).toHaveTextContent(shown);
  await userEvent.click(picker);
  const names = screen.getAllByRole("option").map((option) => option.textContent);
  expect(names.join("|")).not.toContain(hidden);
  expect(names.join("|")).not.toContain("song ·");
  await userEvent.keyboard("{Escape}");
  // The filter is one tap from all workflows.
  await userEvent.click(screen.getByRole("button", { name: "Show all workflows" }));
  expect(screen.queryByText(line)).toBeNull();
  await userEvent.click(screen.getByRole("combobox", { name: "Model" }));
  expect(screen.getAllByRole("option")).toHaveLength(3);
});
test("a video link with no video workflow says so and does not open Chat", async () => {
  mocks.params = new URLSearchParams("capability=video");
  serveKinds();
  render(<TryPage />);
  expect(
    await screen.findByText("No video is available to you right now. Ask the owner about video."),
  ).toBeVisible();
  expect(screen.getByRole("tab", { name: "Chat" })).toHaveAttribute("aria-selected", "false");
  expect(screen.getByRole("tab", { name: "Workflow" })).toHaveAttribute("aria-selected", "true");
  expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
});
test("a link to work Try cannot run says so", async () => {
  mocks.params = new URLSearchParams("capability=rerank");
  render(<TryPage />);
  expect(await screen.findByText("Try can’t run reranking. Choose another task.")).toBeVisible();
});

// PF-002: Run waits for the model list and needs a model on it.
test("Run stays off while the models load, after they fail and when none fit", async () => {
  let finish!: (value: Response) => void;
  fetchMock.mockImplementation((url: string) =>
    url.includes("workflows")
      ? Promise.resolve(response(workflows))
      : new Promise<Response>((resolve) => { finish = resolve; }),
  );
  mocks.params = new URLSearchParams("capability=image");
  render(<TryPage />);
  expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
  // The list has chat models only: nothing makes a picture.
  finish(response(models));
  expect(await screen.findByText("No picture models are available. Ask the owner.")).toBeVisible();
  expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
  expect(screen.queryByText(/from your link/)).toBeNull();
});
test("a model in the link is offered with its doubt stated", async () => {
  mocks.params = new URLSearchParams("capability=chat&model=not-listed");
  render(<TryPage />);
  await waitFor(() =>
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent(
      "from your link, not in your list",
    ),
  );
  expect(screen.getByRole("button", { name: "Run" })).toBeEnabled();
});

// PF-003: a phone picks a task from a labelled select with every task in it.
test("the phone task picker lists every task and switches the form", async () => {
  render(<TryPage />);
  const picker = screen.getByRole("combobox", { name: "Task" });
  expect(
    [...(picker as HTMLSelectElement).options].map((option) => option.text),
  ).toEqual([
    "Chat",
    "Make a picture",
    "Read text aloud",
    "Transcribe a recording",
    "Choose an option",
    "Turn text into numbers",
    "Workflow",
  ]);
  fireEvent.change(picker, { target: { value: "decide" } });
  expect(screen.getByRole("tab", { name: "Choose an option" })).toHaveAttribute("aria-selected", "true");
  // PF-005: the decision form asks in plain words.
  expect(screen.getByText("What is happening?")).toBeVisible();
  expect(screen.queryByText("State")).toBeNull();
});

// PF-004: the empty result says where the result goes, not "Run chat" again.
test("the empty result names where the result appears", async () => {
  render(<TryPage />);
  expect(await screen.findByText("Your result appears here")).toBeVisible();
  expect(screen.queryByText("Run chat")).toBeNull();
});
test("a member's sound effects link reads as a plural", async () => {
  mocks.params = new URLSearchParams("capability=sfx");
  fetchMock.mockImplementation(async (url: string) =>
    response(url.includes("workflows") ? { workflows: [] } : models),
  );
  render(<TryPage />);
  expect(
    await screen.findByText(
      "No sound effects are available to you right now. Ask the owner about sound effects.",
    ),
  ).toBeVisible();
});

test.each(["switch-task", "unmount"] as const)(
  "releases permission granted after %s without starting a recorder",
  async (ending) => {
    mocks.params = new URLSearchParams("capability=transcribe");
    fetchMock.mockImplementation(async (url: string) =>
      response(url.includes("workflows") ? { workflows: [] } : {
        aliases: [],
        models: [
          ...models.models,
          { id: "fixture-stt", name: "Fixture transcription", capabilities: ["transcribe"] },
        ],
      }),
    );
    let finish!: (value: MediaStream) => void;
    const getUserMedia = vi.fn(() => new Promise<MediaStream>((resolve) => {
      finish = resolve;
    }));
    const stop = vi.fn();
    const original = Object.getOwnPropertyDescriptor(navigator, "mediaDevices");
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia },
    });
    const recorderConstructor = vi.fn();
    vi.stubGlobal("MediaRecorder", recorderConstructor);
    const view = render(<TryPage />);
    try {
      fireEvent.click(await screen.findByRole("button", { name: "Record" }));
      expect(getUserMedia).toHaveBeenCalledTimes(1);
      if (ending === "unmount") view.unmount();
      else await userEvent.setup().click(screen.getByRole("tab", { name: "Chat" }));
      await act(async () => {
        finish({ getTracks: () => [{ stop }] } as unknown as MediaStream);
      });
      expect(stop).toHaveBeenCalledTimes(1);
      expect(recorderConstructor).not.toHaveBeenCalled();
      expect(mocks.tryKey).not.toHaveBeenCalled();
    } finally {
      view.unmount();
      if (original) Object.defineProperty(navigator, "mediaDevices", original);
      else Reflect.deleteProperty(navigator, "mediaDevices");
    }
  },
);

test("serializes pending microphone acquisition and releases the acquired stream", async () => {
  mocks.params = new URLSearchParams("capability=transcribe");
  fetchMock.mockImplementation(async (url: string) =>
    response(url.includes("workflows") ? { workflows: [] } : {
      aliases: [],
      models: [{ id: "fixture-stt", name: "Fixture transcription", capabilities: ["transcribe"] }],
    }),
  );

  const original = Object.getOwnPropertyDescriptor(navigator, "mediaDevices");
  const finishers: Array<(stream: MediaStream) => void> = [];
  const getUserMedia = vi.fn(() => new Promise<MediaStream>((resolve) => {
    finishers.push(resolve);
  }));
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia },
  });
  const constructed = vi.fn();
  class FakeRecorder {
    state = "inactive";
    ondataavailable: ((event: BlobEvent) => void) | null = null;
    onstop: (() => void) | null = null;
    constructor(_stream: MediaStream) { constructed(); }
    start() { this.state = "recording"; }
    stop() {
      if (this.state !== "recording") return;
      this.state = "inactive";
      this.onstop?.();
    }
  }
  vi.stubGlobal("MediaRecorder", FakeRecorder);
  const view = render(<TryPage />);
  try {
    const record = await screen.findByRole("button", { name: "Record" });
    fireEvent.click(record);
    fireEvent.click(record);
    // Resolve every request even on the buggy implementation so cleanup is tested.
    const stops = finishers.map(() => vi.fn());
    await act(async () => {
      finishers.forEach((finish, index) => finish({
        getTracks: () => [{ stop: stops[index] }],
      } as unknown as MediaStream));
    });
    view.unmount();
    expect(getUserMedia).toHaveBeenCalledTimes(1);
    expect(constructed).toHaveBeenCalledTimes(1);
    expect(stops).toHaveLength(1);
    expect(stops[0]).toHaveBeenCalledTimes(1);
    expect(mocks.tryKey).not.toHaveBeenCalled();
    expect(mocks.revokeKey).not.toHaveBeenCalled();
  } finally {
    view.unmount();
    if (original) Object.defineProperty(navigator, "mediaDevices", original);
    else Reflect.deleteProperty(navigator, "mediaDevices");
  }
});


function tryJson(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function serveTryRun(body: unknown, status = 200) {
  fetchMock.mockImplementation(async (url: string) => {
    if (url === "/me/api/models/catalog") return response(models);
    if (url === "/me/api/workflows") return response(workflows);
    return tryJson(body, status);
  });
}

test("a model blocked by the plan shows the returned plan reason", async () => {
  const reason = "Your Starter plan does not include this model. Choose a model on your plan.";
  serveTryRun({ error: { message: reason, details: { layer: "account" } } }, 403);
  render(<TryPage />);
  await pressRun();
  expect(await screen.findByRole("heading", { name: "Your account stopped this request" })).toBeVisible();
  expect(screen.getByText(reason)).toBeVisible();
  expect(screen.queryByRole("heading", { name: "Chat result" })).toBeNull();
  expect(screen.getByRole("button", { name: "Run" })).toBeEnabled();
});

test("a cancelled workflow job is terminal and cannot be polled again", async () => {
  mocks.params = new URLSearchParams("capability=workflow");
  fetchMock.mockImplementation(async (url: string) => {
    if (url === "/me/api/models/catalog") return response(models);
    if (url === "/me/api/workflows") return response({ workflows: [{
      id: "wf-cancel", title: "Cancellable workflow", inputs: [], outputs: [],
      example_request: { path: "/v1/workflows/cancel", body: {} },
    }] });
    return tryJson({ object: "media.job", id: "job-cancel", status: "cancelled" });
  });
  render(<TryPage />);
  await pressRun();
  expect(await screen.findByText("Couldn’t finish")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Check again" })).toBeNull();
  expect(screen.queryByText("The gateway is still making this. Check again to load it.")).toBeNull();
  expect(screen.getByRole("button", { name: "Run" })).toBeEnabled();
});

test("a long completed chat keeps every character including the tail", async () => {
  const answer = "BEGIN\n" + "Long answer detail ".repeat(4000) + "\nEND-OF-ANSWER";
  serveTryRun({ choices: [{ message: { content: answer }, finish_reason: "stop" }] });
  const view = render(<TryPage />);
  await pressRun();
  await screen.findByRole("heading", { name: "Chat result" });
  const output = view.container.querySelector("section[aria-live] pre");
  expect(output?.textContent).toBe(answer);
  expect(screen.queryByRole("heading", { name: "Reply cut off" })).toBeNull();
});

function serveImageRun(reply: (url: string) => Response) {
  mocks.params = new URLSearchParams("capability=image");
  fetchMock.mockImplementation(async (url: string) => {
    if (url === "/me/api/models/catalog") return response({
      aliases: [], models: [{ id: "picture-a", name: "Picture model", capability: "image" }],
    });
    if (url === "/me/api/workflows") return response(workflows);
    return reply(url);
  });
}

test.each([
  ["url", { url: "/pictures/result.webp" }, "/pictures/result.webp"],
  ["base64", { b64_json: "cGljdHVyZQ==" }, "data:image/png;base64,cGljdHVyZQ=="],
])("image %s result shows bounded preview, download, model and run time", async (_kind, item, url) => {
  serveImageRun((path) => path.endsWith("/content")
    ? tryJson({ data: [item] })
    : tryJson({ object: "media.job", id: "picture-job", status: "completed" }));
  render(<TryPage />);
  await pressRun();
  const preview = await screen.findByRole("img", { name: "Generated result" });
  expect(preview).toHaveAttribute("src", url);
  expect(preview).toHaveClass("max-h-96", "object-contain");
  expect(preview.parentElement?.parentElement).toHaveClass("max-w-2xl");
  const download = screen.getByRole("link", { name: "Download picture" });
  expect(download).toHaveAttribute("href", url);
  expect(download).toHaveAttribute("download", "generated-picture");
  expect(screen.getByText("Run time").nextElementSibling?.textContent).toMatch(/^\d+ ms$/);
  expect(screen.getByText("Model", { selector: "dt" }).nextElementSibling).toHaveTextContent("Picture model");
  fireEvent.error(preview);
  expect(screen.getByText("The picture couldn’t be loaded.")).toBeVisible();
  expect(download).toBeVisible();
});

test.each(["http", "job"])("image %s failure uses plain recovery copy", async (kind) => {
  serveImageRun(() => kind === "http"
    ? tryJson({ error: { message: "upstream_trace_internal_failure" } }, 500)
    : tryJson({ object: "media.job", id: "picture-job", status: "failed" }));
  render(<TryPage />);
  await pressRun();
  expect(await screen.findByText(kind === "http"
    ? "We couldn’t make this picture. Try again, or choose another model."
    : "The gateway could not make this picture.")).toBeVisible();
  expect(screen.queryByRole("link", { name: "Download picture" })).toBeNull();
  expect(screen.queryByRole("heading", { name: "Image result" })).toBeNull();
  expect(screen.getByRole("button", { name: "Run" })).toBeEnabled();
});

test("image blocked by a plan keeps its explanation and offers another run", async () => {
  const reason = "Your Starter plan does not include this model. Choose a model on your plan.";
  serveImageRun(() => tryJson({ error: { message: reason, details: { layer: "account" } } }, 403));
  render(<TryPage />);
  await pressRun();
  expect(await screen.findByText(reason)).toBeVisible();
  expect(screen.getByRole("heading", { name: "Your account stopped this request" })).toBeVisible();
  expect(screen.queryByRole("link", { name: "Download picture" })).toBeNull();
  expect(screen.getByRole("button", { name: "Run" })).toBeEnabled();
});

test("chat length limit retains the partial reply and marks it as cut", async () => {
  serveTryRun({ choices: [{ message: { content: "Visible partial reply" }, finish_reason: "length" }] });
  render(<TryPage />);
  await pressRun();
  expect(await screen.findByText("Visible partial reply")).toBeVisible();
  expect(screen.getByText("This answer stopped early because it reached its length limit.")).toBeVisible();
  expect(screen.queryByText("The gateway returned no reply.")).toBeNull();
});

test.each(["", "   \n\t"])("empty chat prompt %j is refused with a clear message before a request", async (prompt) => {
  serveTryRun({ choices: [{ message: { content: "Should not run" } }] });
  render(<TryPage />);
  await waitFor(() => expect(screen.getByRole("button", { name: "Run" })).toBeEnabled());
  fireEvent.change(screen.getByRole("textbox", { name: "Prompt" }), { target: { value: prompt } });
  await pressRun();
  expect(await screen.findByText("Enter a prompt before running chat.")).toBeVisible();
  expect(mocks.tryKey).not.toHaveBeenCalled();
  expect(fetchMock.mock.calls.filter(([url]) => url === "/openai/v1/chat/completions")).toHaveLength(0);
});

test("chat network drop offers an explicit retry without automatically repeating the request", async () => {
  let attempts = 0;
  serveChat(() => {
    if (++attempts === 1) throw new TypeError("Failed to fetch");
    return chatReply({ choices: [{ message: { content: "Recovered reply" }, finish_reason: "stop" }] });
  });
  render(<TryPage />);
  const prompt = screen.getByRole("textbox", { name: "Prompt" });
  fireEvent.change(prompt, { target: { value: "Keep my question" } });
  await pressRun();
  expect(await screen.findByRole("heading", { name: "The connection dropped" })).toBeVisible();
  expect(screen.getByText("The answer did not come back. Edit and run again when you are back online.")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Edit and run again" }));
  expect(prompt).toHaveFocus();
  expect(prompt).toHaveValue("Keep my question");
  expect(attempts).toBe(1);
  await pressRun();
  expect(await screen.findByText("Recovered reply")).toBeVisible();
  expect(attempts).toBe(2);
  expect(screen.queryByRole("heading", { name: "The connection dropped" })).toBeNull();
});
