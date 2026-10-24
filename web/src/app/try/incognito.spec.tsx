import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import TryPage from "./page";
import { ViewerContext } from "@/components/shell/session";

// Keep request history is one switch on every Try tab. On (the default), each request is kept as the person's
// settings say; off, each request carries the forget control. The request history choice and the prompt setting
// say what would be kept when it is off.
const mocks = vi.hoisted(() => ({
  params: new URLSearchParams(),
  session: vi.fn(),
  tryKey: vi.fn(),
  revokeKey: vi.fn(),
  promptMemory: vi.fn(),
  requestHistory: vi.fn(),
}));

vi.mock("next/navigation", () => ({ useSearchParams: () => mocks.params }));
vi.mock("@/lib/account-api", () => ({
  accountApi: {
    session: mocks.session,
    tryKey: mocks.tryKey,
    revokeKey: mocks.revokeKey,
    promptMemory: mocks.promptMemory,
    requestHistory: mocks.requestHistory,
  },
}));

const member = { authenticated: true, name: "River", email: null, role: "member", admin: false };

const promptsOn = {
  prompt_memory: {
    remember: true,
    retention: "7_days",
    effective_remember: true,
    effective_retention: "7_days",
    household: { default_remember: true, never_keep: false, retention_cap: "1_year" },
  },
};
const promptsOff = {
  prompt_memory: {
    ...promptsOn.prompt_memory,
    remember: false,
    effective_remember: false,
  },
};
const historyOn = {
  keep: true,
  retention: "7_days",
  parts: ["accepted"],
  choices: ["1_day", "7_days", "30_days"],
  allowed_parts: ["accepted", "received"],
  notice_due: false,
};

const fetchMock = vi.fn();

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

/** A catalog with one model for the tab, under the capability key Try reads. */
const catalogFor = (tab: string) => {
  if (tab === "speech") {
    return {
      aliases: [],
      models: [{
        id: "speech-a",
        name: "Voice model",
        capability: "speech",
        speech_voices: { voices: [{ id: "voice-a", name: "Voice A" }], default_voice: "voice-a", allow_other: false },
      }],
    };
  }
  return {
    aliases: [],
    models: [{ id: `${tab}-a`, name: `${tab} model`, capability: tab }],
  };
};

const workflow = {
  workflows: [{
    id: "wf-a",
    title: "Working workflow",
    inputs: [],
    outputs: [],
    example_request: { path: "/v1/workflow-test", body: {} },
  }],
};

/** Serve the catalogs for one tab, and the given reply to its request. Every other request answers empty. */
function serve(tab: string, reply: unknown = { choices: [{ message: { content: "Reply" }, finish_reason: "stop" }] }) {
  mocks.params = new URLSearchParams(`capability=${tab}`);
  fetchMock.mockImplementation(async (url: string) => {
    if (url === "/me/api/models/catalog") return response(tab === "workflow" ? { aliases: [], models: [] } : catalogFor(tab));
    if (url === "/me/api/workflows") return response(workflow);
    if (url.includes("workflows")) return response(workflow);
    return response(reply);
  });
}

function renderTry() {
  return render(
    <ViewerContext.Provider value={member}>
      <TryPage />
    </ViewerContext.Provider>,
  );
}

/** The request body the page sent with a POST, parsed. Form bodies keep the JSON `extensions` field. */
async function sentBody(): Promise<Record<string, unknown>> {
  await waitFor(() =>
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(true),
  );
  const [, init] = fetchMock.mock.calls.findLast(([, options]) => options?.method === "POST")!;
  const body = init.body;
  if (body instanceof FormData) {
    const raw = body.get("extensions");
    return { extensions: typeof raw === "string" ? JSON.parse(raw) : undefined };
  }
  return JSON.parse(String(body));
}

function forgetIn(body: Record<string, unknown>) {
  const extensions = body.extensions as Record<string, unknown> | undefined;
  return extensions?.["com.azizam.prompts"];
}

async function pressRun() {
  // A person who has not written a prompt takes the tab's example first.
  const example = screen.queryByRole("button", { name: "Try an example" });
  const promptBox = screen.queryByRole("textbox", { name: /^(Prompt|Question)$/ }) as HTMLTextAreaElement | null;
  if (example && promptBox?.value === "") fireEvent.click(example);
  const run = await screen.findByRole("button", { name: "Run" });
  await waitFor(() => expect(run).toBeEnabled());
  fireEvent.click(run);
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  Object.defineProperty(HTMLElement.prototype, "hasPointerCapture", { configurable: true, value: () => false });
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: () => {} });
  fetchMock.mockReset();
  mocks.params = new URLSearchParams();
  mocks.session.mockResolvedValue({ authenticated: true, apps: [{ id: "app", name: "Try" }] });
  mocks.tryKey.mockResolvedValue({ id: "key", api_key: "test-secret", reused: false, app_name: "Try" });
  mocks.promptMemory.mockResolvedValue(promptsOn);
  mocks.requestHistory.mockResolvedValue(historyOn);
  sessionStorage.clear();
  localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  sessionStorage.clear();
});

describe("Keep request history in Try", () => {
  it("is on by default, and says what Azizam keeps for this person", async () => {
    serve("chat");
    renderTry();
    const keep = await screen.findByRole("switch", { name: "Keep request history" });
    expect(keep).toBeChecked();
    expect(await screen.findByText("Saved in your History for 7 days.")).toBeVisible();
  });

  it("when off, says it isn’t saved in History, and the request carries the forget control", async () => {
    serve("chat");
    renderTry();
    await userEvent.click(await screen.findByRole("switch", { name: "Keep request history" }));
    expect(await screen.findByText("Not saved in your History.")).toBeVisible();
    await pressRun();
    expect(forgetIn(await sentBody())).toEqual({ mode: "forget" });
  });

  it("a request with keep request history on carries no forget control", async () => {
    serve("chat");
    renderTry();
    await screen.findByRole("switch", { name: "Keep request history" });
    await pressRun();
    expect(forgetIn(await sentBody())).toBeUndefined();
  });

  it.each(["chat", "image", "speech", "transcribe", "decide", "embed", "workflow"])(
    "every %s request carries the forget control once keep request history is off",
    async (tab) => {
      serve(tab);
      renderTry();
      await userEvent.click(await screen.findByRole("switch", { name: "Keep request history" }));
      if (tab === "transcribe") {
        const input = await screen.findByLabelText("Audio file");
        await userEvent.upload(input, new File(["sound"], "clip.m4a", { type: "audio/mp4" }));
      }
      await pressRun();
      expect(forgetIn(await sentBody())).toEqual({ mode: "forget" });
    },
  );

  it("when prompts are off, a picture request still goes with forget and offers no switch", async () => {
    mocks.promptMemory.mockResolvedValue(promptsOff);
    serve("image");
    renderTry();
    expect(await screen.findByText("Prompts aren’t kept")).toBeVisible();
    expect(screen.queryByRole("switch", { name: "Keep request history" })).not.toBeInTheDocument();
    await pressRun();
    expect(forgetIn(await sentBody())).toEqual({ mode: "forget" });
  });

  it("says nothing is kept, with no switch to change, when History and prompts are both off", async () => {
    mocks.promptMemory.mockResolvedValue(promptsOff);
    mocks.requestHistory.mockResolvedValue({ ...historyOn, keep: false });
    serve("chat");
    renderTry();
    expect(await screen.findByText("Nothing you send is kept")).toBeVisible();
    expect(screen.queryByRole("switch", { name: "Keep request history" })).not.toBeInTheDocument();
  });

  it("stays off for this browser tab only, and is back on in a new session", async () => {
    serve("chat");
    const first = renderTry();
    await userEvent.click(await screen.findByRole("switch", { name: "Keep request history" }));
    expect(sessionStorage.getItem("azizam.try.forget")).toBe("1");
    first.unmount();

    renderTry();
    expect(await screen.findByRole("switch", { name: "Keep request history" })).not.toBeChecked();
  });
});
