import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { adminFetch } from "@/lib/api";
import { SubscriptionsSection } from "./subscriptions-section";
vi.mock("@/lib/api", async (original) => ({ ...await original<typeof import("@/lib/api")>(), adminFetch: vi.fn() }));
afterEach(() => { cleanup(); window.history.replaceState({}, "", "/providers/"); vi.clearAllMocks(); });
function show(enabled = true) {
  vi.mocked(adminFetch).mockResolvedValue({ subscriptions: [
    { kind: "fixture", display_name: "Fixture", status: "disconnected" },
  ] });
  render(<QueryClientProvider client={new QueryClient()}><SubscriptionsSection enabled={enabled} /></QueryClientProvider>);
}
it("opens a saved detail link for a disconnected plan and returns to the list", async () => {
  window.history.replaceState({}, "", "/providers/?subscription=fixture");
  show();
  expect(await screen.findByRole("heading", { name: "Fixture subscription" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("link", { name: "Back to Providers" }));
  expect(window.location.search).toBe("");
  expect(await screen.findByRole("link", { name: "View Fixture subscription" })).toHaveAttribute("href", "/providers/?subscription=fixture");
});
it("shows an unknown link rather than selecting the first plan", async () => {
  window.history.replaceState({}, "", "/providers/?subscription=missing");
  show();
  expect(await screen.findByText("Plan not found")).toBeInTheDocument();
});
it("does not fetch connection data for a non-owner", () => { show(false); expect(adminFetch).not.toHaveBeenCalled(); });
it("separates allowance from recorded counts and explains refresh", async () => {
  window.history.replaceState({}, "", "/providers/?subscription=fixture");
  show();
  expect(await screen.findByRole("group", { name: "Provider allowance" })).toBeInTheDocument();
  expect(screen.getByRole("group", { name: "Through Azizam" })).toHaveTextContent("unavailable");
  expect(screen.getByRole("button", { name: "Refresh displayed usage" })).toBeInTheDocument();
});

it("ages the displayed allowance after reset without a reload", async () => {
  vi.useFakeTimers();
  const now = Date.now();
  const { SubscriptionDetails } = await import("./subscriptions-section");
  const { act } = await import("@testing-library/react");
  render(<SubscriptionDetails sub={{ kind: "fixture", status: "connected", usage_limits: {
    captured_at: now / 1000, windows: [{ id: "account", scope: "Account", source: "runner", used_percent: 42,
      window_minutes: 300, resets_at: now / 1000 + 1 }] } }} subscriptions={[]} />);
  expect(screen.getByText(/42% used/)).toBeInTheDocument();
  act(() => { vi.advanceTimersByTime(30_001); });
  expect(screen.getByText(/out of date/)).toBeInTheDocument();
  expect(screen.queryByText(/42% used/)).not.toBeInTheDocument();
  vi.useRealTimers();
});
