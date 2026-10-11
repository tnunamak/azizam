import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError, api, type StatusPayload } from "@/lib/api";
import { productApi } from "@/lib/product-api";
import * as session from "@/components/shell/session";
import EnginesPage from "@/app/engines/page";
import * as healthPresentation from "@/lib/health-presentation";
import { Button } from "@/components/ui/button";
import { MemberStatus } from "@/app/status/status-view";
import type { ProviderStatusView } from "@/lib/provider-status";
import { StatusClient } from "@/app/status/status-client";

vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams() }));
const state = vi.hoisted(() => ({ providers: [] as ProviderStatusView[] }));
vi.mock("@/lib/provider-status", () => ({
  useOptionalProviderStatus: () => ({ state: "ready", providers: state.providers, refetch: vi.fn() }),
}));
vi.mock("next/link", () => ({ default: ({ children, href, ...props }: React.ComponentProps<"a">) => <a href={href} {...props}>{children}</a> }));
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
  const row = reason.closest("article")!;
  // The card's title names its layer; the reason is the gateway's sentence.
  expect(within(row).getByRole("heading", { name: "The gateway needs attention" })).toBeVisible();
  expect(within(row).queryByRole("button", { name: "Check disk" })).toBeNull();
  const disk = screen.getByText("Disk is almost full.").closest("article")!;
  expect(within(disk).getByRole("heading", { name: "The gateway’s computer reported a disk problem" })).toBeVisible();
  expect(within(disk).getByRole("button", { name: "Check disk" })).toBeVisible();
});
it("does not attach a disk remedy to a temperature warning", async () => {
  mount([
    { source: "thermal", tone: "warn", reasons: ["Temperature is high."] },
    { source: "disk", tone: "warn", reasons: ["Disk is almost full."] },
  ]);
  const reason = await screen.findByText("Temperature is high.");
  expect(within(reason.closest("article")!).queryByRole("button", { name: "Check disk" })).toBeNull();
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
  // One card per provider (a missing key is reported before reachability)
  // and one per layer: gateway, computer, computer disk.
  expect(await screen.findByText("5 problems need you")).toBeVisible();
  expect(screen.getAllByText("Request failures.")).toHaveLength(1);
  expect(screen.getAllByText("Disk full.")).toHaveLength(1);
  const first = screen.getByRole("heading", { name: "First provider needs a key." }).closest("article")!;
  expect(within(first).getByRole("link", { name: "Add provider key" })).toBeVisible();
  const second = screen.getByRole("heading", { name: "Second provider is down" }).closest("article")!;
  expect(within(second).getByText("The last connection check failed.")).toBeVisible();
  expect(screen.getByText("Check failed.")).toBeVisible();
  for (const reason of ["Disk full.", "Other disk full."]) {
    const row = screen.getByText(reason).closest("article")!;
    expect(within(row).getByRole("button", { name: "Check disk" })).toBeVisible();
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
  expect(await screen.findByText("Couldn’t check your status")).toBeVisible();
  expect(screen.getByText("The gateway returned an error. Try again.")).toBeVisible();
  // The status code is kept for whoever needs it, out of the main sentence.
  expect(screen.getByText("Technical details")).toBeInTheDocument();
  expect(screen.getByText(new RegExp(`HTTP ${status}`))).toBeInTheDocument();
  expect(screen.queryByText(/server side/)).toBeNull();
  expect(screen.queryByText(/Check your connection/)).toBeNull();
});

it.each([new TypeError("Failed to fetch"), new Error("Unexpected failure")])("keeps unclassified member status errors neutral: %s", async (error) => {
  vi.spyOn(productApi, "status").mockRejectedValue(error);
  mountHealthErrorPage(<MemberStatus />);
  expect(await screen.findByText("Couldn’t check your status")).toBeVisible();
  expect(screen.getByText("We couldn’t tell where this stopped. Try again.")).toBeVisible();
  expect(screen.queryByText(/could not be reached/)).toBeNull();
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
  const table = screen.getByRole("table", { name: "Engines" });
  const row = within(table).getByText("Endpoint regression engine").closest("tr")!;
  expect(within(row).getAllByRole("button", { name: /Endpoint regression engine/ }).length).toBeGreaterThan(0);
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
    expect(await screen.findByText("Endpoint must use http://. HTTPS endpoints are not supported by this editor yet.")).toBeVisible();
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
  expect(await screen.findByText("Endpoint must use http://. HTTPS endpoints are not supported by this editor yet.")).toBeVisible();
  expect(edit).not.toHaveBeenCalled();
});

function mockEngineRows() {
  vi.spyOn(session, "useViewer").mockReturnValue({ admin: true } as ReturnType<typeof session.useViewer>);
  // Isolate row presentation from the unchanged health classification helper.
  vi.spyOn(healthPresentation, "engineHealthPresentation").mockImplementation(
    (enabled, observationAvailable, health) => ({
      label: !enabled ? "Disabled" : !observationAvailable || !health ? "Unknown" : health.service_id === "degraded" ? "Degraded" : "Ready",
      tone: !enabled || !observationAvailable || !health ? "neutral" : health.service_id === "degraded" ? "warn" : "ok",
    }) as ReturnType<typeof healthPresentation.engineHealthPresentation>,
  );
  const services = Object.fromEntries([
    ["ready", "Chat", ["chat"], true],
    ["degraded", "Images", ["image"], true],
    ["off", "Video", ["video"], false],
    ["unknown", "Voice", ["speech"], true],
    ["infrastructure", "Infrastructure", [], true],
  ].map(([id, name, capabilities, enabled]) => [String(id), {
    id, name, capabilities, enabled, kind: "openai-compatible", editable: true,
    source: id === "infrastructure" ? "provisioned" : "ui",
    role: "primary", priority: 1, config: { host: "localhost", port: 18123 },
  }]));
  vi.spyOn(api.services, "registry").mockResolvedValue({
    schema_version: 1, services,
  } as unknown as Awaited<ReturnType<typeof api.services.registry>>);
  const checkedAt = Math.floor(Date.now() / 1000) - 120;
  vi.spyOn(api, "status").mockResolvedValue({
    generated_at: checkedAt + 120,
    backends: [{ service_id: "ready", observed_at_s: checkedAt }],
    current_health: {
      observation_available: true, signals: [],
      services: ["ready", "degraded", "off", "infrastructure"].map((id) => ({
        service_id: id, in_use: true,
      })),
    },
  } as unknown as StatusPayload);
}

it("shows Ready, Degraded, Off and Unknown with only recorded engine check times", async () => {
  mockEngineRows();
  mountHealthErrorPage(<EnginesPage />);
  await screen.findByRole("switch", { name: "Show not in use" });
  fireEvent.click(screen.getByRole("switch", { name: "Show not in use" }));
  await waitFor(() => {
    for (const [id, word] of [["ready", "Ready"], ["degraded", "Degraded"], ["off", "Off"], ["unknown", "Unknown"]]) {
      const row = document.querySelector(`[data-engine-id="${id}"]`);
      expect(row).not.toBeNull();
      expect(within(row as HTMLElement).getByText(word)).toBeVisible();
    }
  });
  const ready = document.querySelector('[data-engine-id="ready"]') as HTMLElement;
  expect(within(ready).getByText(/Last checked/)).toBeVisible();
  for (const id of ["degraded", "off", "unknown"]) {
    const row = document.querySelector(`[data-engine-id="${id}"]`) as HTMLElement;
    expect(within(row).queryByText(/Last checked/)).toBeNull();
  }
});

it("keeps capability and infrastructure engines in one compact table", async () => {
  mockEngineRows();
  mountHealthErrorPage(<EnginesPage />);
  fireEvent.click(await screen.findByRole("button", { name: "Show infrastructure" }));
  fireEvent.click(screen.getByRole("switch", { name: "Show not in use" }));
  const table = screen.getByRole("table", { name: "Engines" });
  expect(screen.getAllByRole("table")).toHaveLength(1);
  await waitFor(() => expect(table.querySelectorAll("[data-engine-id]")).toHaveLength(5));
  for (const row of table.querySelectorAll("[data-engine-id]")) {
    // The row carries the height tokens: a table cell height does not set a row's height.
    expect(row).toHaveClass("h-(--row-2)", "max-sm:h-(--row-phone-2)");
  }
  fireEvent.click(screen.getByRole("button", { name: "Show infrastructure" }));
  expect(table.querySelector('[data-engine-id="infrastructure"]')).toBeNull();
  expect(screen.getAllByRole("table")).toHaveLength(1);
});

it("uses a secondary outline Inspect action on the primary engine problem", async () => {
  mockEngineRows();
  mountHealthErrorPage(<StatusClient />);
  const inspect = await screen.findByRole("link", { name: /Inspect/ });
  expect(inspect).toHaveAttribute("href", "/engines/?service=degraded");
  render(<Button variant="outline" size="md">Outline reference</Button>);
  expect(inspect.className).toBe(screen.getByRole("button", { name: "Outline reference" }).className);
});
