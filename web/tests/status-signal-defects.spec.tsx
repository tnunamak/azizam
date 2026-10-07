import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api, type StatusPayload } from "@/lib/api";
import type { ProviderStatusView } from "@/lib/provider-status";
import { StatusClient } from "@/app/status/status-client";

vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams() }));
const state = vi.hoisted(() => ({ providers: [] as ProviderStatusView[] }));
vi.mock("@/lib/provider-status", () => ({
  useOptionalProviderStatus: () => ({ state: "ready", providers: state.providers, refetch: vi.fn() }),
}));
vi.mock("next/link", () => ({ default: ({ children, href }: React.ComponentProps<"a">) => <a href={href}>{children}</a> }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); state.providers = []; });
function mount(signals: NonNullable<StatusPayload["current_health"]>["signals"]) {
  vi.spyOn(api.services, "registry").mockResolvedValue({ schema_version: 1, services: {} });
  vi.spyOn(api, "status").mockResolvedValue({ current_health: {
    state: "warn", tone: "warn", label: "Needs attention", detail: "", observation_available: true, services: [], signals,
  } });
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}><StatusClient /></QueryClientProvider>);
}
it("keeps the disk remedy on the disk signal rather than an API signal", async () => {
  mount([
    { source: "api", tone: "warn", reasons: ["Gateway request failures are elevated."] },
    { source: "disk", tone: "warn", reasons: ["Disk is almost full."] },
  ]);
  const reason = await screen.findByText("Gateway request failures are elevated.");
  const row = reason.parentElement!.parentElement!;
  expect(within(row).getByText("Gateway needs attention")).toBeVisible();
  expect(within(row).queryByRole("button", { name: "Free disk space" })).toBeNull();
});
it("does not attach a disk remedy to a temperature warning", async () => {
  mount([
    { source: "thermal", tone: "warn", reasons: ["Temperature is high."] },
    { source: "disk", tone: "warn", reasons: ["Disk is almost full."] },
  ]);
  const reason = await screen.findByText("Temperature is high.");
  expect(within(reason.parentElement!.parentElement!).queryByRole("button", { name: "Free disk space" })).toBeNull();
});
