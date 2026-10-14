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
  tryKey: vi.fn(),
  revokeKey: vi.fn(),
}));
vi.mock("next/navigation", () => ({ useSearchParams: () => mocks.params }));
vi.mock("@/lib/account-api", () => ({
  accountApi: {
    session: mocks.session,
    tryKey: mocks.tryKey,
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

