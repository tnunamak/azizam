import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import HistoryPage from "./page";

const mocks = vi.hoisted(() => ({
  viewer: { authenticated: true, serviceAdmin: false },
  requestHistory: vi.fn(),
  requestHistoryPage: vi.fn(),
  requestHistoryRecord: vi.fn(),
  receiptRetention: vi.fn(),
  deleteRequestHistoryRecord: vi.fn(),
  deleteAllRequestHistory: vi.fn(),
  toast: { success: vi.fn(), error: vi.fn() },
}));

vi.mock("@/components/shell/session", () => ({
  useViewer: () => mocks.viewer,
}));
vi.mock("@/lib/toast", () => ({ toast: mocks.toast }));
vi.mock("@/lib/account-api", () => ({
  accountApi: {
    requestHistory: mocks.requestHistory,
    requestHistoryPage: mocks.requestHistoryPage,
    requestHistoryRecord: mocks.requestHistoryRecord,
    receiptRetention: mocks.receiptRetention,
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

const choice = (keep: boolean) => ({
  keep,
  retention: "7_days",
  parts: ["accepted"],
  choices: ["1_day", "7_days", "30_days"],
  allowed_parts: ["accepted", "received"],
  notice_due: false,
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
  mocks.requestHistory.mockResolvedValue(choice(true));
  mocks.requestHistoryPage.mockResolvedValue({ requests: [], next_cursor: null });
  mocks.requestHistoryRecord.mockResolvedValue({
    record_id: "a",
    kind: "request_history",
    created_at: NOW - 60,
    kept_until: NOW + 86_400,
    origin_kind: "app_key",
    request_id: "req_a",
    parts: {
      accepted: { headers: { "content-type": "application/json" }, body: { model: "chat-a", messages: [{ content: "Hello from history" }] } },
      received: { status: 200, headers: {}, body: { choices: [{ message: { content: "A reply" } }] } },
    },
    omissions: [],
    summary: { route: "/chat/completions", model: "chat-a", status: 200, latency_ms: 40 },
  });
  mocks.receiptRetention.mockResolvedValue({
    retention: [{ kind: "request_history", decision: "kept", reason: "kept", kept_until_ms: (NOW + 86_400) * 1000, parts: ["accepted", "received"], limited_by: "member_choice" }],
  });
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

  it("explains that history is off, and links to the setting that turns it on", async () => {
    mocks.requestHistory.mockResolvedValue(choice(false));
    renderPage();
    expect(await screen.findByText("History is off")).toBeVisible();
    expect(screen.getByRole("link", { name: "Open Your settings" })).toHaveAttribute("href", "/account#history");
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
  it("shows what was sent and what came back, with the receipt's sentence about keeping it", async () => {
    mocks.requestHistoryPage.mockResolvedValue({ requests: [item("a", "chat-a")], next_cursor: null });
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /chat-a/ }));

    expect(await screen.findByRole("heading", { name: "What was sent" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "What came back" })).toBeVisible();
    expect(screen.getByText(/Hello from history/)).toBeVisible();
    expect(screen.getByText(/A reply/)).toBeVisible();
    expect(await screen.findByText(/^Kept the request and its reply until /)).toBeVisible();
    expect(mocks.requestHistoryRecord).toHaveBeenCalledWith("a");
    expect(mocks.receiptRetention).toHaveBeenCalledWith("req_a");
  });

  it("shows only what was sent when the reply was not kept", async () => {
    mocks.requestHistoryPage.mockResolvedValue({ requests: [item("a", "chat-a")], next_cursor: null });
    mocks.requestHistoryRecord.mockResolvedValue({
      record_id: "a",
      kind: "request_history",
      created_at: NOW,
      kept_until: NOW + 60,
      origin_kind: "app_key",
      request_id: null,
      parts: { accepted: { headers: {}, body: { model: "chat-a" } } },
      omissions: [],
      summary: { route: "/chat/completions", model: "chat-a", status: 200, latency_ms: 1 },
    });
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /chat-a/ }));
    expect(await screen.findByRole("heading", { name: "What was sent" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "What came back" })).not.toBeInTheDocument();
    expect(screen.getByText("The receipt for this request isn’t available.")).toBeVisible();
    expect(mocks.receiptRetention).not.toHaveBeenCalled();
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
