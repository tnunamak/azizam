import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RequestHistoryNotice } from "./request-history-notice";

const mocks = vi.hoisted(() => ({
  viewer: { authenticated: true, serviceAdmin: false },
  requestHistory: vi.fn(),
  saveRequestHistory: vi.fn(),
  dismissRequestHistoryNotice: vi.fn(),
  toast: { success: vi.fn(), error: vi.fn() },
}));

vi.mock("@/components/shell/session", () => ({
  useViewer: () => mocks.viewer,
}));
vi.mock("@/lib/toast", () => ({ toast: mocks.toast }));
vi.mock("@/lib/account-api", () => ({
  accountApi: {
    requestHistory: mocks.requestHistory,
    saveRequestHistory: mocks.saveRequestHistory,
    dismissRequestHistoryNotice: mocks.dismissRequestHistoryNotice,
  },
}));

const choice = (overrides: Record<string, unknown> = {}) => ({
  keep: false,
  retention: "7_days",
  parts: ["accepted"],
  choices: ["1_day", "7_days", "30_days"],
  allowed_parts: ["accepted", "received"],
  notice_due: true,
  ...overrides,
});

function renderNotice() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <RequestHistoryNotice />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mocks.viewer = { authenticated: true, serviceAdmin: false };
  mocks.requestHistory.mockResolvedValue(choice());
  mocks.saveRequestHistory.mockImplementation(async (value: Record<string, unknown>) =>
    choice({ ...value, notice_due: false }),
  );
  mocks.dismissRequestHistoryNotice.mockResolvedValue({ notice_due: false });
  mocks.toast.error.mockClear();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("the one-time request history notice on Home", () => {
  it("tells a member who predates request history that it is available and off for them", async () => {
    renderNotice();
    expect(await screen.findByText("Request history is available")).toBeVisible();
    expect(
      screen.getByText(
        "Azizam can now keep your request history so you can look back at what you asked. It’s off for you until you turn it on.",
      ),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Turn on" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Not now" })).toBeEnabled();
  });

  it("is not shown when the notice is not due", async () => {
    mocks.requestHistory.mockResolvedValue(choice({ notice_due: false, keep: true }));
    renderNotice();
    await waitFor(() => expect(mocks.requestHistory).toHaveBeenCalled());
    expect(screen.queryByText("Request history is available")).not.toBeInTheDocument();
  });

  it("goes away on Not now, and dismissing does not change the choice", async () => {
    renderNotice();
    await userEvent.click(await screen.findByRole("button", { name: "Not now" }));
    await waitFor(() => expect(screen.queryByText("Request history is available")).not.toBeInTheDocument());
    expect(mocks.dismissRequestHistoryNotice).toHaveBeenCalledTimes(1);
    expect(mocks.saveRequestHistory).not.toHaveBeenCalled();
  });

  it("turns history on with the member's own period and parts, and goes away", async () => {
    renderNotice();
    await userEvent.click(await screen.findByRole("button", { name: "Turn on" }));
    await waitFor(() =>
      expect(mocks.saveRequestHistory).toHaveBeenCalledWith({ keep: true, retention: "7_days", parts: ["accepted"] }),
    );
    await waitFor(() => expect(screen.queryByText("Request history is available")).not.toBeInTheDocument());
    expect(mocks.dismissRequestHistoryNotice).not.toHaveBeenCalled();
  });

  it("keeps the notice and says so when turning history on fails", async () => {
    mocks.saveRequestHistory.mockRejectedValue(new Error("busy"));
    renderNotice();
    await userEvent.click(await screen.findByRole("button", { name: "Turn on" }));
    await waitFor(() =>
      expect(mocks.toast.error).toHaveBeenCalledWith("Request history wasn’t turned on. Try again."),
    );
    expect(screen.getByText("Request history is available")).toBeVisible();
  });

  it("is never asked of a key session, which has no person to tell", async () => {
    mocks.viewer = { authenticated: true, serviceAdmin: true };
    renderNotice();
    expect(mocks.requestHistory).not.toHaveBeenCalled();
    expect(screen.queryByText("Request history is available")).not.toBeInTheDocument();
  });
});
