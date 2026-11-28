import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RequestHistorySection } from "./request-history-section";

const mocks = vi.hoisted(() => ({
  requestHistory: vi.fn(),
  saveRequestHistory: vi.fn(),
  toast: { success: vi.fn(), error: vi.fn() },
}));

vi.mock("@/lib/toast", () => ({ toast: mocks.toast }));
vi.mock("@/lib/account-api", () => ({
  accountApi: {
    requestHistory: mocks.requestHistory,
    saveRequestHistory: mocks.saveRequestHistory,
  },
}));

const choice = (overrides: Record<string, unknown> = {}) => ({
  keep: true,
  retention: "7_days",
  parts: ["accepted"],
  choices: ["1_day", "7_days", "30_days"],
  allowed_parts: ["accepted", "received"],
  notice_due: false,
  ...overrides,
});

function renderSection() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <RequestHistorySection />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  // Radix measures the switch and the select with APIs jsdom does not have.
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  Object.defineProperty(HTMLElement.prototype, "hasPointerCapture", { configurable: true, value: () => false });
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: () => {} });
  mocks.requestHistory.mockResolvedValue(choice());
  mocks.saveRequestHistory.mockImplementation(async (value: Record<string, unknown>) => choice(value));
  mocks.toast.error.mockClear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("Request history in Your settings", () => {
  it("shows history on, the period, and a way to keep replies, with a link to History", async () => {
    renderSection();
    expect(await screen.findByRole("heading", { name: "Request history" })).toBeVisible();
    expect(screen.getByRole("switch", { name: "Keep request history" })).toBeChecked();
    expect(screen.getByRole("switch", { name: "Also keep replies" })).not.toBeChecked();
    expect(screen.getByRole("combobox", { name: "Forget requests after" })).toHaveTextContent("7 days");
    expect(screen.getByRole("link", { name: "Open History" })).toHaveAttribute("href", "/history");
  });

  it("asks before turning history off, names the deletion, then saves the choice off", async () => {
    renderSection();
    await userEvent.click(await screen.findByRole("switch", { name: "Keep request history" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Stop keeping request history?")).toBeVisible();
    expect(within(dialog).getByText(/deletes the requests and replies it kept for you now/)).toBeVisible();
    expect(mocks.saveRequestHistory).not.toHaveBeenCalled();

    await userEvent.click(within(dialog).getByRole("button", { name: "Turn off" }));
    await waitFor(() =>
      expect(mocks.saveRequestHistory).toHaveBeenCalledWith({ keep: false, retention: "7_days", parts: ["accepted"] }),
    );
  });

  it("turns history back on at once, with no confirm", async () => {
    mocks.requestHistory.mockResolvedValue(choice({ keep: false }));
    renderSection();
    await userEvent.click(await screen.findByRole("switch", { name: "Keep request history" }));
    await waitFor(() =>
      expect(mocks.saveRequestHistory).toHaveBeenCalledWith({ keep: true, retention: "7_days", parts: ["accepted"] }),
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("asks before shortening the period, and saves a longer one at once", async () => {
    renderSection();
    await userEvent.click(await screen.findByRole("combobox", { name: "Forget requests after" }));
    await userEvent.click(await screen.findByRole("option", { name: "1 day" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Forget requests after 1 day?")).toBeVisible();
    expect(mocks.saveRequestHistory).not.toHaveBeenCalled();
    await userEvent.click(within(dialog).getByRole("button", { name: "Forget older requests" }));
    await waitFor(() =>
      expect(mocks.saveRequestHistory).toHaveBeenCalledWith({ keep: true, retention: "1_day", parts: ["accepted"] }),
    );

    cleanup();
    mocks.saveRequestHistory.mockClear();
    renderSection();
    await userEvent.click(await screen.findByRole("combobox", { name: "Forget requests after" }));
    await userEvent.click(await screen.findByRole("option", { name: "30 days" }));
    await waitFor(() =>
      expect(mocks.saveRequestHistory).toHaveBeenCalledWith({ keep: true, retention: "30_days", parts: ["accepted"] }),
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("saves both parts when replies are kept, and only what was sent when they are not", async () => {
    renderSection();
    await userEvent.click(await screen.findByRole("switch", { name: "Also keep replies" }));
    await waitFor(() =>
      expect(mocks.saveRequestHistory).toHaveBeenCalledWith({
        keep: true,
        retention: "7_days",
        parts: ["accepted", "received"],
      }),
    );
  });

  it("cannot change the period or replies while history is off, and says how to turn it on", async () => {
    mocks.requestHistory.mockResolvedValue(choice({ keep: false, parts: ["accepted"] }));
    renderSection();
    expect(await screen.findByRole("combobox", { name: "Forget requests after" })).toBeDisabled();
    expect(screen.getByRole("switch", { name: "Also keep replies" })).toBeDisabled();
    expect(screen.getAllByText("Turn on request history to change this.")).toHaveLength(2);
  });

  it("says history is off on this gateway, and cannot be turned on, when the owner has turned it off for everyone", async () => {
    mocks.requestHistory.mockResolvedValue(choice({ keep: true, household_off: true }));
    renderSection();
    expect(await screen.findByText("History is off on this gateway. Nothing new is kept.")).toBeVisible();
    expect(screen.queryByText("Azizam keeps what your apps send so you can look back at it.")).not.toBeInTheDocument();

    const keep = screen.getByRole("switch", { name: "Keep request history" });
    expect(keep).not.toBeChecked();
    expect(keep).toBeDisabled();
    expect(screen.getByRole("switch", { name: "Also keep replies" })).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "Forget requests after" })).toBeDisabled();
    expect(screen.getAllByText("The owner has turned request history off for everyone.")).toHaveLength(2);

    await userEvent.click(keep);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(mocks.saveRequestHistory).not.toHaveBeenCalled();
  });

  it("says when the settings cannot be read", async () => {
    mocks.requestHistory.mockRejectedValue(new Error("nope"));
    renderSection();
    expect(await screen.findByText("Couldn’t load your request history")).toBeVisible();
  });
});
