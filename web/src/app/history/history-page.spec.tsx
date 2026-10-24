import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import HistoryPage from "./page";

const mocks = vi.hoisted(() => ({
  viewer: { authenticated: true, serviceAdmin: false },
  modelName: (id: string) => id,
  requestHistory: vi.fn(),
  requestHistoryPage: vi.fn(),
  requestHistoryRecord: vi.fn(),
  receiptRetention: vi.fn(),
  libraryItemForRequest: vi.fn(),
  deleteRequestHistoryRecord: vi.fn(),
  deleteAllRequestHistory: vi.fn(),
  toast: { success: vi.fn(), error: vi.fn() },
}));

vi.mock("@/components/shell/session", () => ({
  useViewer: () => mocks.viewer,
}));
vi.mock("@/lib/toast", () => ({ toast: mocks.toast }));
vi.mock("@/lib/use-model-name", () => ({
  useModelName: () => mocks.modelName,
}));
vi.mock("@/lib/account-api", () => ({
  accountApi: {
    requestHistory: mocks.requestHistory,
    requestHistoryPage: mocks.requestHistoryPage,
    requestHistoryRecord: mocks.requestHistoryRecord,
    receiptRetention: mocks.receiptRetention,
    libraryItemForRequest: mocks.libraryItemForRequest,
    deleteRequestHistoryRecord: mocks.deleteRequestHistoryRecord,
    deleteAllRequestHistory: mocks.deleteAllRequestHistory,
  },
}));

const NOW = Math.floor(Date.now() / 1000);

const item = (id: string, model: string, extra: Record<string, unknown> = {}) => ({
  record_id: id,
  request_id: `req_${id}`,
  kind: "request_history",
  created_at: NOW - 60,
  kept_until: NOW + 86_400,
  origin_kind: "app_key",
  app_name: "Try",
  kept_parts: ["accepted"],
  summary: { route: "/chat/completions", model, status: 200, latency_ms: 40 },
  ...extra,
});

const choice = (keep: boolean, extra: Record<string, unknown> = {}) => ({
  keep,
  retention: "7_days",
  parts: ["accepted"],
  choices: ["1_day", "7_days", "30_days"],
  allowed_parts: ["accepted", "received"],
  household_off: false,
  notice_due: false,
  ...extra,
});

const recordBody = (overrides: Record<string, unknown> = {}) => ({
  record_id: "a",
  kind: "request_history",
  created_at: NOW - 120,
  kept_until: NOW + 86_400,
  origin_kind: "app_key",
  request_id: "req_a",
  parts: {
    accepted: {
      headers: { "content-type": "application/json" },
      body: { model: "chat-a", messages: [{ role: "user", content: "Hello from history" }] },
    },
    received: { status: 200, headers: {}, body: { choices: [{ message: { content: "A reply" } }] } },
  },
  omissions: [],
  summary: { route: "/openai/v1/chat/completions", model: "chat-a", status: 200, latency_ms: 40 },
  ...overrides,
});

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <HistoryPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mocks.viewer = { authenticated: true, serviceAdmin: false };
  mocks.modelName = (id: string) => id;
  mocks.requestHistory.mockResolvedValue(choice(true));
  mocks.requestHistoryPage.mockResolvedValue({ requests: [], next_cursor: null });
  mocks.requestHistoryRecord.mockResolvedValue(recordBody());
  mocks.receiptRetention.mockResolvedValue({
    retention: [{ kind: "request_history", decision: "kept", reason: "kept", kept_until_ms: (NOW + 86_400) * 1000, parts: ["accepted", "received"], limited_by: "member_choice" }],
  });
  mocks.libraryItemForRequest.mockResolvedValue(null);
  mocks.deleteRequestHistoryRecord.mockResolvedValue(undefined);
  mocks.deleteAllRequestHistory.mockResolvedValue({ deleted: 2 });
  mocks.toast.success.mockClear();
  mocks.toast.error.mockClear();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("History list", () => {
  it("lists the person's own requests newest first and pages with the server cursor", async () => {
    mocks.requestHistoryPage
      .mockResolvedValueOnce({
        requests: [item("a", "chat-a"), item("b", "chat-b")],
        next_cursor: "cursor-1",
      })
      .mockResolvedValueOnce({ requests: [item("c", "chat-c")], next_cursor: null });
    renderPage();

    const first = await screen.findByRole("button", { name: /chat-a/ });
    const rows = within(first.closest("ul") as HTMLElement).getAllByRole("button");
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining("chat-a"),
      expect.stringContaining("chat-b"),
    ]);
    expect(mocks.requestHistoryPage).toHaveBeenLastCalledWith(null);
    expect(screen.getAllByText("Try")).toHaveLength(2);
    expect(screen.getAllByText("OK")).toHaveLength(2);

    const more = screen.getByRole("button", { name: "Load more" });
    await userEvent.click(more);
    expect(mocks.requestHistoryPage).toHaveBeenLastCalledWith("cursor-1");
    expect(await screen.findByRole("button", { name: /chat-c/ })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

  it("shows the model's name, and under it what was asked, and no route", async () => {
    mocks.modelName = (id: string) => (id === "chat-a" ? "Chat A" : id);
    mocks.requestHistoryPage.mockResolvedValue({
      requests: [item("a", "chat-a", { summary: { route: "/openai/v1/chat/completions", model: "chat-a", status: 200, preview: "What is the capital of France?" } })],
      next_cursor: null,
    });
    renderPage();
    const row = await screen.findByRole("button", { name: /Chat A/ });
    expect(within(row).getByText("Chat A")).toBeVisible();
    expect(within(row).getByText("What is the capital of France?")).toBeVisible();
    expect(screen.queryByText("/openai/v1/chat/completions")).not.toBeInTheDocument();
    expect(screen.queryByText("/chat/completions")).not.toBeInTheDocument();
  });

  it("shows the kind in place of what was asked when the row has no preview", async () => {
    mocks.requestHistoryPage.mockResolvedValue({
      requests: [item("a", "whisper-1", { summary: { route: "/v1/audio/transcriptions", model: "whisper-1", status: 200 } })],
      next_cursor: null,
    });
    renderPage();
    const row = await screen.findByRole("button", { name: /whisper-1/ });
    expect(within(row).getByText("Transcription")).toBeVisible();
  });

  it("explains that history is off, and links to the setting that turns it on", async () => {
    mocks.requestHistory.mockResolvedValue(choice(false));
    renderPage();
    expect(await screen.findByText("History is off")).toBeVisible();
    expect(screen.getByRole("link", { name: "Open Your settings" })).toHaveAttribute("href", "/account#history");
  });

  it("says the owner has turned history off for everyone, and offers no setting to turn it on", async () => {
    mocks.requestHistory.mockResolvedValue(choice(true, { household_off: true }));
    mocks.requestHistoryPage.mockResolvedValue({ requests: [], next_cursor: null });
    renderPage();
    expect(await screen.findByText("History is off on this gateway")).toBeVisible();
    expect(screen.getByText(/The owner has turned request history off for everyone/)).toBeVisible();
    expect(screen.queryByRole("link", { name: "Open Your settings" })).not.toBeInTheDocument();
  });

  it("says, above the rows, that the owner has turned history off, whatever the member chose", async () => {
    mocks.requestHistory.mockResolvedValue(choice(true, { household_off: true }));
    mocks.requestHistoryPage.mockResolvedValue({ requests: [item("a", "chat-a")], next_cursor: null });
    renderPage();
    expect(await screen.findByText("History is off on this gateway. Nothing new is kept.")).toBeVisible();
  });

  it("explains an empty history that is on, and links to Try", async () => {
    renderPage();
    expect(await screen.findByText("No requests yet")).toBeVisible();
    expect(screen.getByText(/kept here for 7 days/)).toBeVisible();
    expect(screen.getByRole("link", { name: "Open Try" })).toHaveAttribute("href", "/try");
  });

  it("says a key session has no history of its own", async () => {
    mocks.viewer = { authenticated: true, serviceAdmin: true };
    renderPage();
    expect(await screen.findByText("Sign in as a person")).toBeVisible();
    expect(mocks.requestHistoryPage).not.toHaveBeenCalled();
  });
});

describe("opening one request", () => {
  it("names the model in the title, gives the time in words, and shows the conversation as bubbles", async () => {
    mocks.modelName = (id: string) => (id === "chat-a" ? "Chat A" : id);
    mocks.requestHistoryPage.mockResolvedValue({ requests: [item("a", "chat-a")], next_cursor: null });
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /Chat A/ }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Chat A" })).toBeVisible();
    expect(within(dialog).getByText("2 minutes ago")).toBeVisible();
    expect(within(dialog).getByText("Hello from history")).toBeVisible();
    expect(within(dialog).getByText("A reply")).toBeVisible();
    expect(await within(dialog).findByText(/^Kept the request and its reply until /)).toBeVisible();
    expect(mocks.requestHistoryRecord).toHaveBeenCalledWith("a");
    expect(mocks.receiptRetention).toHaveBeenCalledWith("req_a");
  });

  it("keeps the raw record folded away under Technical details, with the app, route and headers", async () => {
    mocks.requestHistoryPage.mockResolvedValue({ requests: [item("a", "chat-a")], next_cursor: null });
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /chat-a/ }));
    const dialog = await screen.findByRole("dialog");

    const technical = within(dialog).getByText("Technical details").closest("details") as HTMLElement;
    expect(technical).not.toHaveAttribute("open");
    expect(technical).toHaveTextContent("App: Try");
    expect(technical).toHaveTextContent("Route: /openai/v1/chat/completions");
    expect(technical).toHaveTextContent('"content-type": "application/json"');
    expect(within(dialog).queryByText(/What was sent/)).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("heading", { name: "What came back" })).not.toBeInTheDocument();
  });

  it("says the reply was not kept, and that the receipt is not there, when the request has no receipt", async () => {
    mocks.requestHistoryPage.mockResolvedValue({ requests: [item("a", "chat-a")], next_cursor: null });
    mocks.requestHistoryRecord.mockResolvedValue(
      recordBody({
        request_id: null,
        parts: { accepted: { headers: {}, body: { model: "chat-a", messages: [{ role: "user", content: "Hi" }] } } },
      }),
    );
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /chat-a/ }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("The reply wasn’t kept.")).toBeVisible();
    expect(within(dialog).getByText("The receipt for this request isn’t available.")).toBeVisible();
    expect(mocks.receiptRetention).not.toHaveBeenCalled();
  });

  it("says that details were removed, once, and shows no removed marker in the readable part", async () => {
    mocks.requestHistoryPage.mockResolvedValue({ requests: [item("a", "chat-a")], next_cursor: null });
    mocks.requestHistoryRecord.mockResolvedValue(
      recordBody({
        parts: {
          accepted: {
            headers: {},
            body: { model: "chat-a", api_key: "[removed]", messages: [{ role: "user", content: "Use key [removed] to ask" }] },
          },
        },
      }),
    );
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /chat-a/ }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Some details weren’t kept (keys and links are never saved).")).toBeVisible();
    expect(within(dialog).getByText("Use key to ask")).toBeVisible();
    const conversation = within(dialog).getByRole("list", { name: "Conversation" });
    expect(conversation.textContent).not.toMatch(/\[removed\]/);
  });

  it("links the picture a request made to its Library item", async () => {
    mocks.requestHistoryPage.mockResolvedValue({
      requests: [item("a", "img-a", { summary: { route: "/v1/images/generations", model: "img-a", status: 200, preview: "a fox" } })],
      next_cursor: null,
    });
    mocks.requestHistoryRecord.mockResolvedValue(
      recordBody({
        request_id: "req_a",
        parts: { accepted: { headers: {}, body: { model: "img-a", prompt: "a fox", size: "1024x1024" } } },
        summary: { route: "/v1/images/generations", model: "img-a", status: 200 },
      }),
    );
    mocks.libraryItemForRequest.mockResolvedValue("asset-7");
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /img-a/ }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("a fox")).toBeVisible();
    expect(await within(dialog).findByRole("link", { name: "Open in Library" })).toHaveAttribute("href", "/library?asset=asset-7");
  });
});

describe("deleting", () => {
  it("deletes one request after a confirm, then refreshes the list", async () => {
    mocks.requestHistoryPage
      .mockResolvedValueOnce({ requests: [item("a", "chat-a"), item("b", "chat-b")], next_cursor: null })
      .mockResolvedValueOnce({ requests: [item("b", "chat-b")], next_cursor: null });
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /chat-a/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Delete this request" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Delete this request?")).toBeVisible();
    expect(within(dialog).getByText(/Receipts and Library items stay/)).toBeVisible();
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete request" }));

    await waitFor(() => expect(mocks.deleteRequestHistoryRecord).toHaveBeenCalledWith("a"));
    await waitFor(() => expect(screen.queryByRole("button", { name: /chat-a/ })).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: /chat-b/ })).toBeVisible();
    expect(mocks.toast.success).toHaveBeenCalledWith("Deleted this request.");
  });

  it("does not delete when the confirm is cancelled", async () => {
    mocks.requestHistoryPage.mockResolvedValue({ requests: [item("a", "chat-a")], next_cursor: null });
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /chat-a/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Delete this request" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(mocks.deleteRequestHistoryRecord).not.toHaveBeenCalled();
  });

  it("deletes all history after a confirm that says what goes, and shows the empty state", async () => {
    mocks.requestHistoryPage
      .mockResolvedValueOnce({ requests: [item("a", "chat-a")], next_cursor: null })
      .mockResolvedValueOnce({ requests: [], next_cursor: null });
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Delete all history" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Delete all request history?")).toBeVisible();
    expect(within(dialog).getByText(/Azizam deletes every request and reply it kept for you/)).toBeVisible();
    expect(within(dialog).getByText(/Pictures, audio and video in Library stay/)).toBeVisible();
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete all" }));

    await waitFor(() => expect(mocks.deleteAllRequestHistory).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("No requests yet")).toBeVisible();
    expect(mocks.toast.success).toHaveBeenCalledWith("Deleted all request history.");
  });
});
