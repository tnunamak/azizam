import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api, type ServiceRegistry, type StatusPayload } from "@/lib/api";
import { StatusClient } from "./status-client";

// Moved from e2e/status-engines-status.spec.ts: the seeded, empty and error
// pictures of the engines-and-providers layer, with the same response shapes.
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));
const providerState = vi.hoisted(() => ({
  state: "hidden" as string,
  providers: [] as Array<Record<string, unknown>>,
  refetch: vi.fn(),
}));
vi.mock("@/lib/use-capability-views", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/use-capability-views")>()),
  useActivitySample: () => ({
    data: [],
    isPending: false,
    isError: false,
    refetch: vi.fn(),
  }),
}));
vi.mock("@/lib/provider-status", () => ({
  useOptionalProviderStatus: () => providerState,
}));
vi.mock("@/app/_home/home-api", () => ({
  homeApi: {
    home: async () => ({
      authenticated: true,
      capabilities: ["chat", "image"].map((capability) => ({
        capability,
        capability_title: capability === "chat" ? "Chat" : "Images",
        icon_key: capability,
        status: "ready",
        available: true,
        availability: {
          evidence: { source: "status", observed_at_s: Date.now() / 1000 },
        },
      })),
      aliases: [],
      receipts: [],
      apps: [],
    }),
  },
}));
vi.mock("next/link", () => ({
  default: ({ children, href }: React.ComponentProps<"a">) => (
    <a href={href}>{children}</a>
  ),
}));
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  providerState.state = "hidden";
  providerState.providers = [];
  providerState.refetch.mockClear();
});

const service = (
  id: string,
  name: string,
  capabilities: string[],
  enabled: boolean,
  config: Record<string, unknown>,
) => ({
  id,
  name,
  kind: "generic",
  source: "ui" as const,
  enabled,
  editable: true,
  capabilities,
  role: capabilities[0] === "chat" ? "primary chat" : "image",
  config,
});
const registry = {
  schema_version: 1,
  services: {
    chat: service("chat", "Local chat engine", ["chat"], true, {
      base_url: "http://127.0.0.1:18081/v1",
    }),
    image: service("image", "Local image engine", ["image", "video"], false, {
      base_url: "http://127.0.0.1:18082",
    }),
    imageOnly: service("imageOnly", "Image-only engine", ["image"], true, {
      base_url: "http://127.0.0.1:18083",
    }),
  },
} as unknown as ServiceRegistry;
const emptyRegistry = { schema_version: 1, services: {} } as ServiceRegistry;

const row = (
  id: string,
  name: string,
  title: string,
  labels: string[],
  enabled: boolean,
) => ({
  service_id: id,
  name,
  engine_label: name,
  capability_title: title,
  capability_labels: labels,
  icon_key: id === "chat" ? "chat" : "image",
  configured: true,
  enabled,
  role: id === "chat" ? "primary chat" : "image",
  routable: enabled ? true : null,
  observation: enabled ? "up" : "disabled",
  tone: enabled ? "ok" : "neutral",
  last_error: null,
});
const seeded = {
  current_health: {
    state: "warn",
    tone: "warn",
    label: "One engine needs attention",
    detail: "The chat engine is reachable; one disabled engine is hidden by default.",
    observation_available: true,
    signals: [
      {
        source: "disk",
        tone: "warn",
        reasons: ["Media disk (/media) is above the warning threshold."],
      },
    ],
    services: [
      row("chat", "Local chat engine", "Chat", ["Chat"], true),
      row("image", "Local image engine", "Images", ["Images", "Video"], false),
      row("imageOnly", "Image-only engine", "Images", ["Images"], true),
    ],
  },
  backends: [{ service_id: "chat", url: "http://127.0.0.1:18081/v1", latency_ms: 42 }],
  disk: {
    partitions: [
      {
        source: "/dev/nvme0n1p2",
        target: "/media",
        display_name: "Disk (/media)",
        size_bytes: 1000,
        used_bytes: 930,
        avail_bytes: 70,
        pct_used: 93,
      },
    ],
    hotspots: [],
  },
} as unknown as StatusPayload;
const emptyStatus = {
  current_health: {
    state: "empty",
    tone: "neutral",
    label: "No engines configured",
    detail: "Add an engine to start routing.",
    observation_available: false,
    services: [],
  },
  backends: [],
} as unknown as StatusPayload;

const openRouter = {
  id: "openrouter",
  name: "OpenRouter",
  enabled: true,
  reachable: true,
  keyState: "set",
  spendText: "$1.25 of $10.00",
};

function mount(status: StatusPayload | Error, serviceRegistry = registry) {
  vi.spyOn(api.services, "registry").mockResolvedValue(serviceRegistry);
  const spy = vi.spyOn(api, "status");
  if (status instanceof Error) spy.mockRejectedValue(status);
  else spy.mockResolvedValue(status);
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}
    >
      <StatusClient />
    </QueryClientProvider>,
  );
  return spy;
}
const FOLD = "Engines: 2 ready, 1 turned off. Remote providers: 1 set up. Show all";

it("folds healthy engines under a problem and lists the turned-off engine as Off, never Ready", async () => {
  providerState.state = "ready";
  providerState.providers = [openRouter];
  const user = userEvent.setup();
  mount(seeded);
  expect(
    await screen.findByText(/Media disk .*above the warning threshold/),
  ).toBeVisible();
  await user.click(await screen.findByRole("button", { name: FOLD }));
  expect(screen.getByRole("row", { name: /Image-only engine/ })).toBeVisible();
  const disabled = screen.getByRole("row", { name: /Local image engine/ });
  expect(disabled).toHaveTextContent("Off");
  expect(disabled).not.toHaveTextContent("Ready");
  expect(screen.getByRole("rowheader", { name: /^OpenRouter\b/ })).toBeVisible();
});

it("opens provider spend evidence, restores focus, and links the technical details from the actions menu", async () => {
  providerState.state = "ready";
  providerState.providers = [openRouter];
  const user = userEvent.setup();
  mount(seeded);
  await user.click(await screen.findByRole("button", { name: FOLD }));
  const trigger = screen.getByRole("button", { name: "OpenRouter details" });
  await user.click(trigger);
  const sheet = screen.getByRole("dialog", { name: "OpenRouter" });
  expect(within(sheet).getByText("USD 1.25 of USD 10.00")).toBeVisible();
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(trigger).toHaveFocus();
  await user.click(screen.getByRole("button", { name: "Status actions" }));
  const details = await screen.findByRole("menuitem", { name: /Technical details/ });
  expect(details).toHaveAttribute("href", "/status/raw");
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("menuitem", { name: /Technical details/ })).toBeNull();
});

it("says No engines yet and offers Add an engine when nothing is configured", async () => {
  providerState.state = "ready";
  mount(emptyStatus, emptyRegistry);
  await waitFor(() =>
    expect(screen.getAllByRole("status")[0]).toHaveTextContent("No engines yet"),
  );
  expect(screen.getByRole("heading", { name: "No engines yet", level: 2 })).toBeVisible();
  expect(screen.getByRole("link", { name: "Add an engine" })).toHaveAttribute("href", "/engines");
});

it("blames the failed check, claims no engine is ready, and recovers on Refresh", async () => {
  const user = userEvent.setup();
  const spy = mount(Object.assign(new Error("down"), { status: 503 }));
  await waitFor(() =>
    expect(screen.getAllByRole("status")[0]).toHaveTextContent("Couldn’t check status"),
  );
  expect(
    await screen.findByRole("heading", { name: "Engine health couldn’t load" }),
  ).toBeVisible();
  expect(screen.queryByText(/\bready\b/i)).toBeNull();
  spy.mockResolvedValue(seeded);
  await user.click(screen.getByRole("button", { name: "Refresh" }));
  await screen.findByRole("button", { name: /Engines: 2 ready, 1 turned off/ });
  expect(screen.queryByRole("heading", { name: "Engine health couldn’t load" })).toBeNull();
});

it("keeps a provider failure and its Retry visible while the table is folded", async () => {
  providerState.state = "error";
  const user = userEvent.setup();
  mount(seeded);
  expect(
    await screen.findByRole("heading", { name: "Engines and providers" }),
  ).toBeVisible();
  expect(screen.getByText("Provider status did not load.")).toBeVisible();
  expect(
    screen
      .getAllByRole("alert")
      .filter((a) => /Provider status couldn’t load/.test(a.textContent ?? "")),
  ).toHaveLength(1);
  await user.click(screen.getByRole("button", { name: "Retry providers" }));
  expect(providerState.refetch).toHaveBeenCalledTimes(1);
});

it("removes stale green engine observations after a failed refresh", async () => {
  const user = userEvent.setup();
  const spy = mount(seeded);
  await user.click(await screen.findByRole("button", { name: /Engines: 2 ready, 1 turned off/ }));
  const rows = () =>
    screen
      .getAllByRole("row")
      .filter((r) => /engine/i.test(r.textContent ?? "") && !r.querySelector("th[scope=col]"));
  expect(rows().filter((r) => /Ready/.test(r.textContent ?? ""))).toHaveLength(2);
  spy.mockRejectedValue(Object.assign(new Error("Refresh failed"), { status: 503 }));
  await user.click(screen.getByRole("button", { name: "Refresh" }));
  await screen.findByRole("heading", { name: "Engine health couldn’t load" });
  expect(rows()).toHaveLength(3);
  expect(rows().filter((r) => /Ready/.test(r.textContent ?? ""))).toHaveLength(0);
  expect(screen.queryByText(/engines? ready/i)).toBeNull();
});
