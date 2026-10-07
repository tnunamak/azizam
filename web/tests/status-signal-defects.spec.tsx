import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError, api, type StatusPayload } from "@/lib/api";
import { productApi } from "@/lib/product-api";
import * as session from "@/components/shell/session";
import EnginesPage from "@/app/engines/page";
import { MemberStatus } from "@/app/status/status-view";
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
it("counts every provider failure and signal, deduplicating identical source/reason pairs", async () => {
  const provider = { enabled: true, reachable: false, keyState: "set" as const, keySuffix: null, spendText: "", tone: "bad" as const };
  state.providers = [
    { ...provider, id: "first", name: "First provider", keyState: "missing" },
    { ...provider, id: "second", name: "Second provider" },
  ];
  mount([
    { source: "api", tone: "warn", reasons: ["Request failures.", "Request failures."] },
    { source: "disk", tone: "warn", reasons: ["Disk full.", "Other disk full."] },
    { source: "disk", tone: "warn", reasons: ["Disk full."] },
    { source: "thermal", tone: "warn", reasons: ["Temperature high."] },
    { source: "custom-check", tone: "bad", reasons: ["Check failed."] },
  ]);
  expect(await screen.findByText("8 problems need you")).toBeVisible();
  expect(screen.getAllByText("Request failures.")).toHaveLength(1);
  expect(screen.getAllByText("Disk full.")).toHaveLength(1);
  expect(screen.getByText("First provider needs owner action: provider key is missing.")).toBeVisible();
  expect(screen.getByText("Second provider needs owner action: provider is unreachable.")).toBeVisible();
  expect(screen.getByText("Check failed.")).toBeVisible();
  for (const reason of ["Disk full.", "Other disk full."]) {
    const row = screen.getByText(reason).parentElement!.parentElement!;
    expect(within(row).getByRole("button", { name: "Free disk space" })).toBeVisible();
  }
});


function mountHealthErrorPage(page: React.ReactNode) {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0, gcTime: 0 } } })}>{page}</QueryClientProvider>);
}

function statusResponseError(status: number) {
  // Model an HTTP response without depending on the ApiError constructor signature.
  return Object.assign(Object.create(ApiError.prototype) as ApiError, {
    status,
    message: "Status response failed",
  });
}

it("reports an engine list load failure with unknown engine states and working Retry", async () => {
  vi.spyOn(session, "useViewer").mockReturnValue({ admin: true } as ReturnType<typeof session.useViewer>);
  const registry = vi.spyOn(api.services, "registry").mockRejectedValue(new Error("Registry request failed"));
  vi.spyOn(api, "status").mockResolvedValue({});
  mountHealthErrorPage(<EnginesPage />);
  expect(await screen.findByText("Engine list could not be loaded")).toBeVisible();
  expect(screen.getByText(/Each engine's state is unknown\./)).toBeVisible();
  expect(screen.queryByText("Engines unavailable")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(registry).toHaveBeenCalledTimes(2));
});

it.each([403, 500, 503])("reports member status HTTP %s errors by status, blaming no layer", async (status) => {
  vi.spyOn(productApi, "status").mockRejectedValue(statusResponseError(status));
  mountHealthErrorPage(<MemberStatus />);
  expect(await screen.findByText(`Your model status check failed (HTTP ${status}). The gateway owner can see details. Try again.`)).toBeVisible();
  expect(screen.queryByText(/server side/)).toBeNull();
  expect(screen.queryByText(/Check your connection/)).toBeNull();
});

it.each([new TypeError("Failed to fetch"), new Error("Unexpected failure")])("keeps unclassified member status errors neutral: %s", async (error) => {
  vi.spyOn(productApi, "status").mockRejectedValue(error);
  mountHealthErrorPage(<MemberStatus />);
  expect(await screen.findByText("Your model status could not be loaded. Try again.")).toBeVisible();
  expect(screen.queryByText(/Check your connection/)).toBeNull();
  expect(screen.queryByText(/check failed \(HTTP/)).toBeNull();
});

it("keeps the member status sign-in action for HTTP 401", async () => {
  vi.spyOn(productApi, "status").mockRejectedValue(statusResponseError(401));
  mountHealthErrorPage(<MemberStatus />);
  expect(await screen.findByText("Sign in to see your status")).toBeVisible();
  expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/sign-in/?return_to=%2Fui%2Fstatus%2F");
  expect(screen.queryByText(/check failed \(HTTP/)).toBeNull();
});

async function openEndpointEditor(baseUrl: string) {
  vi.spyOn(session, "useViewer").mockReturnValue({ admin: true } as ReturnType<typeof session.useViewer>);
  const registry = {
    schema_version: 1,
    services: {
      endpoint: {
        id: "endpoint", name: "Endpoint regression engine", enabled: true,
        editable: true, source: "provisioned", role: "primary", priority: 1,
        capabilities: [], config: { base_url: baseUrl, note: "Original note" },
      },
    },
  } as unknown as Awaited<ReturnType<typeof api.services.registry>>;
  vi.spyOn(api.services, "registry").mockResolvedValue(registry);
  vi.spyOn(api, "status").mockResolvedValue({});
  const edit = vi.spyOn(api.services, "edit").mockResolvedValue(registry);
  mountHealthErrorPage(<EnginesPage />);
  fireEvent.click(await screen.findByText("Show infrastructure"));
  fireEvent.click(screen.getByText("Endpoint regression engine").closest("button")!);
  await screen.findByRole("button", { name: "Save engine" });
  return edit;
}

it.each([
  "https://engine.example/v1",
  "ftp://engine.example/v1",
  "http://[invalid",
  "",
])("saves an unrelated engine edit without validating or rewriting unchanged endpoint %s", async (endpoint) => {
  const edit = await openEndpointEditor(endpoint);
  fireEvent.change(screen.getByRole("textbox", { name: "Note" }), { target: { value: "Updated note" } });
  fireEvent.click(screen.getByRole("button", { name: "Save engine" }));
  await waitFor(() => expect(edit).toHaveBeenCalledTimes(1));
  expect(edit).toHaveBeenCalledWith("endpoint", expect.objectContaining({
    base_url: endpoint, note: "Updated note",
  }));
  expect(edit.mock.calls[0][1]).not.toHaveProperty("host");
  expect(edit.mock.calls[0][1]).not.toHaveProperty("port");
  expect(edit.mock.calls[0][1]).not.toHaveProperty("base_path");
});

it.each(["https://secure.example/v1/", "https://secure.example:8443/v1/"])(
  "still rejects a NEW https endpoint %s, because health checks are http-only",
  async (endpoint) => {
    const edit = await openEndpointEditor("http://old.example/v1");
    fireEvent.change(screen.getByRole("textbox", { name: "Endpoint" }), { target: { value: endpoint } });
    fireEvent.click(screen.getByRole("button", { name: "Save engine" }));
    expect(await screen.findByText("Endpoint must use http://. HTTPS endpoints are not supported by this registry editor yet.")).toBeVisible();
    expect(edit).not.toHaveBeenCalled();
  },
);

it("keeps changed HTTP engine endpoints supported and removes a stale HTTPS base URL", async () => {
  const edit = await openEndpointEditor("https://old.example/v1");
  fireEvent.change(screen.getByRole("textbox", { name: "Endpoint" }), { target: { value: "http://plain.example/v2/" } });
  fireEvent.click(screen.getByRole("button", { name: "Save engine" }));
  await waitFor(() => expect(edit).toHaveBeenCalledTimes(1));
  expect(edit).toHaveBeenCalledWith("endpoint", expect.objectContaining({
    host: "plain.example", port: 80, base_path: "/v2",
  }));
  expect(edit.mock.calls[0][1]).not.toHaveProperty("base_url");
});

it.each(["ftp://other.example/v1", "ws://other.example/v1"])("rejects a changed unsupported engine endpoint %s with the existing message", async (endpoint) => {
  const edit = await openEndpointEditor("https://old.example/v1");
  fireEvent.change(screen.getByRole("textbox", { name: "Endpoint" }), { target: { value: endpoint } });
  fireEvent.click(screen.getByRole("button", { name: "Save engine" }));
  expect(await screen.findByText("Endpoint must use http://. HTTPS endpoints are not supported by this registry editor yet.")).toBeVisible();
  expect(edit).not.toHaveBeenCalled();
});
