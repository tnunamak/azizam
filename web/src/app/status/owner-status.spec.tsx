import { afterEach, expect, it, vi } from "vitest";
import { UptimeBar } from "@/components/product/uptime-bar";
import {
  cleanup,
  render,
  screen,
  within,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  ApiError,
  api,
  type CurrentHealthService,
  type ServiceRegistry,
  type StatusPayload,
} from "@/lib/api";
import { StatusClient } from "./status-client";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));
const providerState = vi.hoisted(() => ({ state: "hidden", providers: [] as Array<Record<string, unknown>> }));
const activityState = vi.hoisted(() => ({ data: [] as Array<Record<string, unknown>>, isPending: false, isError: false, refetch: vi.fn() }));
vi.mock("@/lib/use-capability-views", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/use-capability-views")>(),
  useActivitySample: () => activityState,
}));
vi.mock("@/lib/provider-status", () => ({
  useOptionalProviderStatus: () => ({
    ...providerState,
    refetch: vi.fn(),
  }),
}));
// The capability model reads the household Home snapshot.
const homeCapabilities = vi.hoisted(() => ({
  rows: [] as Array<Record<string, unknown>>,
}));
vi.mock("@/app/_home/home-api", () => ({
  homeApi: {
    home: async () => ({
      authenticated: true,
      capabilities: homeCapabilities.rows,
      aliases: [],
      receipts: [],
      apps: [],
    }),
  },
}));
function freshChat() {
  const row = (capability: string, title: string) => ({
    capability,
    capability_title: title,
    icon_key: capability,
    status: "ready",
    available: true,
    availability: {
      evidence: { source: "status", observed_at_s: Date.now() / 1000 },
    },
  });
  return [row("chat", "Chat"), row("image", "Images")];
}
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
  activityState.data = [];
  activityState.isPending = false;
  activityState.isError = false;
  activityState.refetch.mockClear();
  homeCapabilities.rows = freshChat();
});
homeCapabilities.rows = freshChat();

const registry: ServiceRegistry = {
  schema_version: 1,
  services: {
    chat: {
      id: "chat",
      name: "Local chat",
      kind: "generic",
      source: "ui",
      enabled: true,
      editable: true,
      capabilities: ["chat"],
      role: "primary",
      config: {
        base_url: "http://localhost:18081/v1",
        models: [{ model_group: "fast", upstream_model: "test" }],
      },
    },
    image: {
      id: "image",
      name: "Image engine",
      kind: "generic",
      source: "ui",
      enabled: false,
      editable: true,
      capabilities: ["image"],
      role: "optional",
      config: {},
    },
    realtime: {
      id: "realtime",
      name: "Speech To Speech",
      display_name: "Realtime voice",
      kind: "generic",
      source: "ui",
      enabled: true,
      editable: true,
      capabilities: ["speech", "transcriptions"],
      config: {},
    },
    remote: {
      id: "remote",
      name: "Remote provider",
      kind: "provider",
      source: "ui",
      enabled: true,
      editable: true,
      capabilities: ["chat"],
      config: {},
    },
  },
};
const chatRegistry: ServiceRegistry = {
  ...registry,
  services: { chat: registry.services.chat, image: registry.services.image },
};
function mount(
  status?: StatusPayload,
  serviceRegistry = chatRegistry,
  statusError = false,
) {
  vi.spyOn(api.services, "registry").mockResolvedValue(serviceRegistry);
  vi.spyOn(api, "status").mockResolvedValue(
    status ?? {
      current_health: {
        state: "ok",
        tone: "ok",
        label: "Ready",
        detail: "Ready",
        observation_available: true,
        services: [
          {
            service_id: "chat",
            name: "Local chat",
            engine_label: "Local chat",
            capability_title: "Chat",
            capability_labels: ["Chat"],
            icon_key: "chat",
            configured: true,
            enabled: true,
            role: "primary",
            routable: true,
            observation: "up",
            tone: "ok",
            last_error: null,
          },
        ],
      },
    },
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  if (statusError) {
    client.setQueryData(["status"], status);
    vi.mocked(api.status).mockRejectedValue(new Error("offline"));
  }
  render(
    <QueryClientProvider client={client}>
      <StatusClient />
    </QueryClientProvider>,
  );
}
it("shows compact grouped engine rows and moves configuration and unknown metrics into a sheet", async () => {
  const user = userEvent.setup();
  mount();
  const engine = await screen.findByRole("button", { name: "Local chat" });
  expect(screen.getByRole("table", { name: "Engine status" })).toBeVisible();
  expect(screen.queryByRole("columnheader", { name: "Last error" })).toBeNull();
  expect(screen.queryByText("Not measured yet")).not.toBeInTheDocument();
  expect(screen.queryByText("Loaded:")).not.toBeInTheDocument();
  expect(screen.queryByText("fast")).not.toBeInTheDocument();
  await user.click(engine);
  const sheet = screen.getByRole("dialog", { name: "Local chat" });
  expect(within(sheet).queryByText("Queue")).not.toBeInTheDocument();
  expect(within(sheet).queryByText("Not measured yet")).not.toBeInTheDocument();
  expect(
    within(sheet).getByRole("link", { name: "Configure engine" }),
  ).toHaveAttribute("href", "/engines?service=chat");
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(engine).toHaveFocus();
});
it("lists disabled engines as turned off, never as ready", async () => {
  mount();
  await screen.findByRole("button", { name: "Local chat" });
  expect(screen.queryByRole("switch")).not.toBeInTheDocument();
  const disabled = screen.getByRole("button", { name: "Image engine" });
  expect(disabled).toBeVisible();
  const row = disabled.closest("tr")!;
  expect(within(row).getByText("Off")).toBeVisible();
  expect(within(row).queryByText("Ready")).not.toBeInTheDocument();
});

it("uses the registry display name and excludes providers from the engine table", async () => {
  mount(undefined, {
    ...registry,
    services: { ...registry.services, realtime: { ...registry.services.realtime, display_name: "realtime_voice" } },
  });
  expect(
    await screen.findByRole("button", { name: "Realtime Voice" }),
  ).toBeVisible();
  expect(screen.queryByRole("button", { name: "Speech To Speech" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Remote provider" })).toBeNull();
  expect(screen.queryByText("Capabilities unavailable")).toBeNull();
  // Realtime voice has no health record: it says Unknown, never Ready.
  const realtime = screen
    .getByRole("button", { name: "Realtime Voice" })
    .closest("tr")!;
  expect(within(realtime).getByText("Unknown")).toBeVisible();
  expect(within(realtime).queryByText("Ready")).toBeNull();
});
it("uses server engine names and transcription taxonomy and preserves an exact disk warning sentence", async () => {
  const user = userEvent.setup();
  mount({
    disk: {
      partitions: [
        { target: "/", display_name: "System disk (/)", pct_used: 96 },
      ],
    },
    current_health: {
      state: "warn",
      tone: "warn",
      label: "Needs attention",
      detail: "System disk (/) is almost full (96%). Remove unused files.",
      observation_available: true,
      signals: [
        {
          source: "host",
          tone: "warn",
          reasons: [
            "System disk (/) is almost full (96%). Remove unused files.",
          ],
        },
      ],
      services: [
        {
          service_id: "chat",
          name: "registry description",
          engine_label: "Canary (transcription)",
          capability_title: "Transcription",
          capability_labels: ["Transcription"],
          icon_key: "transcribe",
          enabled: true,
          configured: true,
          role: "primary",
          routable: true,
          last_error: null,
          observation: "up",
          tone: "ok",
        },
      ],
    },
  });
  await waitFor(() =>
    expect(screen.getByRole("status")).toHaveTextContent(
      "The gateway’s computer reported a disk problem",
    ),
  );
  expect(
    screen.getByRole("heading", { name: "Needs attention" }),
  ).toBeVisible();
  await user.click(screen.getByRole("button", { name: /Show all/ }));
  const engine = await screen.findByRole("button", { name: "Canary" });
  expect(engine).toBeVisible();
  expect(engine).toHaveAttribute("title", "Canary (transcription)");
  expect(screen.queryByRole("button", { name: "Local chat" })).toBeNull();
  expect(screen.getByRole("table", { name: "Engine status" })).toBeVisible();
  // D1.5: what an engine serves sits under its name (Linear's row anatomy).
  expect(
    within(screen.getByRole("rowheader", { name: /Canary/ })).getByText(
      "Transcription",
    ),
  ).toBeVisible();
  expect(
    within(
      screen
        .getByRole("heading", { name: "Status", level: 1 })
        .closest("header")!,
    ).getByText("The gateway’s computer reported a disk problem", { exact: true }),
  ).toBeVisible();
  expect(
    screen.getAllByText(
      "System disk (/) is almost full (96%). Remove unused files.",
      {
        exact: true,
      },
    ),
  ).toHaveLength(1);
  expect(
    screen.queryByText(/host needs owner action|files\.\./),
  ).not.toBeInTheDocument();
});
it("retains server labels when live observation is unavailable without showing ready", async () => {
  mount({
    current_health: {
      state: "unknown",
      tone: "neutral",
      label: "Unknown",
      detail: "Availability has not been measured.",
      observation_available: false,
      services: [
        {
          service_id: "chat",
          name: "raw-id",
          engine_label: "Canary (transcription)",
          capability_title: "Transcription",
          capability_labels: ["Transcription"],
          icon_key: "transcribe",
          enabled: true,
          configured: true,
          role: "primary",
          routable: true,
          last_error: null,
          observation: "up",
          tone: "ok",
        },
      ],
    },
  });
  const engine = await screen.findByRole("button", { name: "Canary" });
  expect(engine).toBeVisible();
  expect(engine).toHaveAttribute("title", "Canary (transcription)");
  expect(screen.queryByText("Ready", { exact: true })).not.toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("Engine health unknown");
  expect(screen.queryByText("Everything works")).toBeNull();
});

it("summarizes connection failures without putting raw transport text in the answer", async () => {
  mount({
    current_health: {
      state: "bad",
      tone: "bad",
      label: "Needs attention",
      detail: "Chat is unavailable.",
      observation_available: true,
      services: [
        {
          service_id: "chat",
          name: "Local chat",
          engine_label: "Local chat",
          capability_title: "Chat",
          capability_labels: ["Chat"],
          icon_key: "chat",
          configured: true,
          enabled: true,
          role: "primary",
          routable: true,
          observation: "down",
          tone: "bad",
          last_error: "Connection refused",
        },
      ],
    },
  });
  await waitFor(() =>
    expect(screen.getByRole("status")).toHaveTextContent("Chat down"),
  );
  // D1.6: the card's title is the consequence, once; its first fact is the
  // fault in plain words, once.
  expect(
    screen.getAllByRole("heading", { name: "Chat is unavailable right now" }),
  ).toHaveLength(1);
  expect(
    screen.getAllByText("Local chat is not running.", {
      exact: true,
    }),
  ).toHaveLength(1);
  expect(
    screen.getAllByText("Check the engine address, or start the engine.", {
      exact: true,
    }),
  ).toHaveLength(1);
  const header = screen
    .getByRole("heading", { name: "Status", level: 1 })
    .closest("header")!;
  expect(
    within(header).queryByText("Connection refused", { exact: true }),
  ).not.toBeInTheDocument();
  expect(
    within(header).queryByText("Local chat is not running", {
      exact: true,
    }),
  ).not.toBeInTheDocument();
  expect(
    screen.getByRole("link", { name: "Inspect Local chat" }),
  ).toHaveAttribute("href", "/engines/?service=chat");
  await userEvent.click(screen.getByRole("button", { name: /Show all/ }));
  await userEvent.click(screen.getByRole("button", { name: "Local chat" }));
  const body = screen.getByRole("dialog", { name: "Local chat" }).querySelector("dl")!;
  expect(body).not.toHaveTextContent("Connection refused");
  expect(within(body).getByText("Not running", { exact: true })).toBeVisible();
  expect(within(body).getByText("Check the engine address and that it is running, then refresh.")).toBeVisible();
});

it("groups engines with the same problem into one action row", async () => {
  const secondChat = {
    ...registry.services.chat,
    id: "chat-copy",
    name: "Second chat",
  };
  const serviceRegistry: ServiceRegistry = {
    ...registry,
    services: { ...registry.services, secondChat },
  };
  const downService = (serviceId: string, name: string) => ({
    service_id: serviceId,
    name,
    engine_label: "Chat",
    capability_title: "Chat",
    capability_labels: ["Chat"],
    icon_key: "chat" as const,
    configured: true,
    enabled: true,
    role: "primary",
    routable: true,
    observation: "down",
    tone: "bad",
    last_error: "Connection refused",
  });
  mount(
    {
      current_health: {
        state: "bad",
        tone: "bad",
        label: "Needs attention",
        detail: "Chat is unavailable.",
        observation_available: true,
        services: [
          downService("chat", "Local chat"),
          downService("chat-copy", "Second chat"),
        ],
      },
    },
    serviceRegistry,
  );
  await waitFor(() =>
    expect(screen.getByRole("status")).toHaveTextContent("Chat down"),
  );
  expect(screen.queryByText(/problems need you/)).toBeNull();
  const attention = screen
    .getByRole("heading", { name: "Needs attention" })
    .closest("section")!;
  expect(within(attention).getAllByRole("article")).toHaveLength(1);
  expect(
    within(attention).getAllByText(/(is|are) not running/),
  ).toHaveLength(1);
  expect(
    within(attention).getByRole("heading", {
      name: "2 engines are not running",
    }),
  ).toBeVisible();
  expect(
    within(attention).getByRole("link", { name: "Inspect engines" }),
  ).toHaveAttribute("href", "/engines/");
  expect(within(attention).getByText("Check the engine address, or start the engine.")).toBeVisible();
});

it.each(["loading", "error"])(
  "does not claim all-clear while providers are %s",
  async (state) => {
    providerState.state = state;
    mount();
    await screen.findByRole("button", { name: "Local chat" });
    expect(screen.getByRole("status")).toHaveTextContent(
      state === "loading" ? "Checking" : "Provider status unavailable",
    );
    expect(screen.queryByText("Everything works")).toBeNull();
  },
);
it("does not claim all-clear when current health is absent", async () => {
  mount({});
  await screen.findByRole("button", { name: "Local chat" });
  expect(screen.getByRole("status")).toHaveTextContent("Engine health unknown");
  expect(screen.queryByText("Everything works")).toBeNull();
});
it("does not trust stale health after a status error", async () => {
  mount(
    {
      current_health: {
        state: "ok",
        tone: "ok",
        label: "Ready",
        detail: "Ready",
        observation_available: true,
        services: [],
      },
    },
    chatRegistry,
    true,
  );
  await screen.findByText("Engine health couldn’t load");
  expect(screen.getByRole("status")).toHaveTextContent("Couldn’t check status");
  expect(screen.queryByText("Everything works")).toBeNull();
});
it("announces the answer atomically as health changes", async () => {
  const { engineHealthPresentation } =
    await import("@/lib/health-presentation");
  expect(
    engineHealthPresentation(true, true, undefined, "degraded").label,
  ).toBe("Degraded");
  mount();
  await screen.findByText("Everything works");
  const live = screen.getByRole("status");
  expect(live).toHaveAttribute("aria-live", "polite");
  expect(live).toHaveAttribute("aria-atomic", "true");
  expect(live.querySelector("table")).toBeNull();
  const status = await api.status();
  vi.mocked(api.status).mockResolvedValue({
    ...status,
    current_health: {
      ...status.current_health!,
      state: "warn",
      services: status.current_health!.services.map((service) => ({
        ...service,
        observation: "degraded",
        tone: "warn",
      })),
    },
  });
  await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
  await waitFor(() => expect(live).toHaveTextContent("Chat degraded"));
  expect(live.querySelector("table")).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: /Show all/ }));
  expect(
    within(screen.getByRole("table", { name: "Engine status" })).getByText(
      "Degraded",
      { exact: true },
    ),
  ).toBeVisible();
});
it("offers a local action when no remote providers are configured", async () => {
  providerState.state = "ready";
  mount();
  expect(await screen.findByText("No remote providers are configured.")).toBeVisible();
  expect(screen.getByRole("link", { name: "Review connections" })).toHaveAttribute("href", "/engines");
});
it("moves from partial to healthy once provider status resolves", async () => {
  providerState.state = "error";
  mount();
  await screen.findByText("Provider status unavailable");
  expect(screen.queryByText("Everything works")).toBeNull();
  providerState.state = "ready";
  await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
  await waitFor(() =>
    expect(screen.getByRole("status")).toHaveTextContent("Everything works"),
  );
});
it.each(["empty", "partial"])(
  "presents %s observation explicitly",
  async (state) => {
    providerState.state = "ready";
    mount(
      {
        current_health: {
          state: state === "empty" ? "empty" : "ok",
          tone: "neutral",
          label: "",
          detail: "",
          observation_available: state !== "empty",
          services: [],
          incomplete:
            state === "partial" ? { observations: ["chat"] } : undefined,
        },
      },
      state === "empty" ? { schema_version: 1, services: {} } : chatRegistry,
    );
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        state === "empty" ? "No engines yet" : "Some engines are unchecked",
      ),
    );
    expect(screen.queryByText("Everything works")).toBeNull();
  },
);

it("tells the 24-hour story from the strip's own hours and names who is affected (D1.3)", async () => {
  const end = 1_800_000_000;
  // One check per hour: down 2 h early in the day, down 1 h at midday, and
  // failing again for the last 2 hours.
  const series = Array.from({ length: 24 }, (_, index) => {
    const bad = [2, 3, 12, 22, 23].includes(index);
    return [end - (24 - index) * 3600 + 1800, bad ? 0 : 1];
  });
  mount({
    generated_at: end,
    current_health: {
      state: "bad",
      tone: "bad",
      label: "Needs attention",
      detail: "Chat is unavailable.",
      observation_available: true,
      services: [
        {
          service_id: "chat",
          name: "Local chat",
          engine_label: "Local chat",
          capability_title: "Chat",
          capability_labels: ["Chat"],
          icon_key: "chat",
          configured: true,
          enabled: true,
          role: "primary",
          routable: true,
          observation: "down",
          tone: "bad",
          last_error: "Connection refused",
        },
      ],
    },
    backends: [
      { key: "chat", service_id: "chat", history: { status_series: series } },
    ],
  } as unknown as StatusPayload);
  const card = (
    await screen.findByRole("heading", {
      name: "Chat is unavailable right now",
    })
  ).closest("article")!;
  // Where it stands, in the strip's own word, then the day's spells.
  expect(within(card).getByText(/^Down since about /)).toBeVisible();
  expect(
    within(card).getByText(
      "Down for 5 hours in the last 24 hours, in 3 separate stretches.",
    ),
  ).toBeVisible();
  // No narrow "Started" window that the strip contradicts.
  expect(within(card).queryByText("Started")).not.toBeInTheDocument();
  // The folded engines read as labelled lines, not a code or prose.
  expect(
    screen.getByRole("button", {
      name: "Other engines: 1 turned off. Show all",
    }),
  ).toBeVisible();
  // Affects names the scope, not the capability again.
  expect(
    within(card).getByText("Chat: no other engine serves it."),
  ).toBeVisible();
});

it("waits for the status check before drawing engine rows, never a passing Unknown", async () => {
  vi.spyOn(api.services, "registry").mockResolvedValue(chatRegistry);
  let answer: (value: StatusPayload) => void = () => undefined;
  vi.spyOn(api, "status").mockReturnValue(
    new Promise<StatusPayload>((resolve) => {
      answer = resolve;
    }),
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  render(
    <QueryClientProvider client={client}>
      <StatusClient />
    </QueryClientProvider>,
  );
  // The registry answered first: the rows still wait for their health.
  await waitFor(() => expect(api.services.registry).toHaveBeenCalled());
  await new Promise((resolve) => setTimeout(resolve, 20));
  expect(screen.queryByRole("table", { name: "Engine status" })).toBeNull();
  expect(screen.getByLabelText("Checking engines")).toHaveAttribute("aria-busy", "true");
  expect(document.body.textContent).not.toMatch(/\bUnknown\b/);
  answer({
    current_health: {
      state: "ok",
      tone: "ok",
      label: "Ready",
      detail: "Ready",
      observation_available: true,
      services: [
        {
          service_id: "chat",
          name: "Local chat",
          engine_label: "Local chat",
          capability_title: "Chat",
          capability_labels: ["Chat"],
          icon_key: "chat",
          configured: true,
          enabled: true,
          role: "primary",
          routable: true,
          observation: "up",
          tone: "ok",
          last_error: null,
        },
      ],
    },
  } as StatusPayload);
  const engine = await screen.findByRole("button", { name: "Local chat" });
  expect(within(engine.closest("tr")!).getByText("Ready")).toBeVisible();
});

it.each(["loading", "error"])("never calls a %s provider read an empty setup", async (state) => {
  providerState.state = state;
  mount({ current_health: { state: "empty", services: [], observation_available: true } } as unknown as StatusPayload, { schema_version: 1, services: {} });
  await waitFor(() => expect(api.status).toHaveBeenCalled());
  expect(await screen.findByText(state === "loading" ? "Checking providers…" : "Provider status couldn’t load.")).toBeVisible();
  expect(screen.queryByText("No engines yet")).toBeNull();
});

it.each(["pending", "error"])("does not turn %s activity into no failures", async (state) => {
  activityState.isPending = state === "pending";
  activityState.isError = state === "error";
  providerState.state = "ready";
  providerState.providers = [{ id: "remote", name: "Data provider", enabled: true, reachable: false, keyState: "set", spendText: "" }];
  mount();
  await screen.findByRole("heading", { name: "Data provider is down" });
  expect(screen.getByText("Open Providers to check its address and key, then try the connection again.")).toBeVisible();
  expect(document.body.textContent).not.toMatch(/None in the latest/);
  expect(screen.getAllByText(state === "pending" ? "Checking recent requests…" : /Recent requests couldn’t load/).length).toBeGreaterThan(0);
  if (state === "error") {
    await userEvent.click(screen.getByRole("button", { name: "Retry recent requests" }));
    expect(activityState.refetch).toHaveBeenCalledOnce();
  }
});

it("uses the loaded request count and distinguishes a missing key from an outage", async () => {
  activityState.data = [{ receipt: { request_id: "failed", backend: "remote", status: "failed" } }, { receipt: { status: "success" } }];
  providerState.state = "ready";
  providerState.providers = [{ id: "remote", name: "Data provider", enabled: true, reachable: false, keyState: "missing", spendText: "" }];
  mount();
  await screen.findByRole("heading", { name: "Data provider needs a key." });
  expect(screen.getByText(/1 of the latest 2 requests/)).toBeVisible();
  expect(screen.queryByText("Data provider is down")).toBeNull();
  expect(screen.getByRole("link", { name: "Add provider key" })).toBeVisible();
  expect(screen.getByText("Add this provider’s key in Providers, then check its connection.")).toBeVisible();
});

it("opens provider spend and cap details and restores focus", async () => {
  providerState.state = "ready";
  providerState.providers = [{ id: "remote", name: "Data provider", enabled: true, reachable: true, keyState: "set", monthlySpend: 2.5, monthlyCap: 10, spendText: "" }];
  mount();
  const trigger = await screen.findByRole("button", { name: "Data provider details" });
  await userEvent.click(trigger);
  const sheet = screen.getByRole("dialog", { name: "Data provider" });
  expect(within(sheet).getByText("USD 2.50")).toBeVisible();
  expect(within(sheet).getByText("USD 10.00")).toBeVisible();
  await userEvent.keyboard("{Escape}");
  expect(trigger).toHaveFocus();
});

it("shows a spending-limit refusal without inventing spend or a reset time", async () => {
  activityState.data = [{ created_at: Date.now() / 1000, receipt: { status: "rejected", app_profile: "p", warnings: ["Paid request refused: spending cap reached; daily spending limit exceeded; monthly budget exceeded."] } }];
  mount();
  expect(await screen.findByText("Spending limit reached")).toBeVisible();
  expect(screen.getByRole("link", { name: "Open providers" })).toBeVisible();
  expect(document.body.textContent).not.toMatch(/roll off|Monthly: on|Reset time/);
});

it.each([new TypeError("Failed to fetch"), new ApiError("failed", "private exception", 503)])("keeps registry errors scoped and retryable: %s", async (error) => {
  mount();
  vi.mocked(api.services.registry).mockRejectedValue(error);
  await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
  const title = await screen.findByRole("heading", { name: "Engine list couldn’t load" });
  const card = title.closest("article")!;
  expect(within(card).getByText(error instanceof ApiError ? "The gateway returned an error. Try again." : "We couldn’t tell where this stopped. Try again.")).toBeVisible();
  expect(within(card).getByRole("button", { name: "Retry" })).toBeVisible();
  expect(screen.queryByText("private exception")).toBeNull();
  expect(screen.queryByText("No engines yet")).toBeNull();
});

function engineSnapshot(
  health: Partial<CurrentHealthService> = {},
  observedAt: number | null = Date.now() / 1000 - 120,
): StatusPayload {
  return {
    generated_at: Date.now() / 1000,
    current_health: {
      state: "ok",
      tone: "ok",
      label: "Ready",
      detail: "Ready",
      observation_available: true,
      services: [{
        service_id: "chat",
        name: "Local chat",
        engine_label: "Local chat",
        capability_title: "Chat",
        capability_labels: ["Chat"],
        icon_key: "chat",
        configured: true,
        enabled: true,
        role: "primary",
        routable: true,
        observation: "up",
        tone: "ok",
        in_use: true,
        last_error: null,
        ...health,
      }],
    },
    backends: [{
      service_id: "chat",
      observed_at_s: observedAt,
      history: { status_series: [[Date.now() / 1000 - 60, 0]] },
    }],
  } as unknown as StatusPayload;
}

async function visibleChatRow() {
  await waitFor(() => {
    expect(
      screen.queryByRole("button", { name: "Local chat" }) ??
      screen.queryByRole("button", { name: /Show all/ }),
    ).not.toBeNull();
  });
  const fold = screen.queryByRole("button", { name: /Show all/ });
  if (fold) await userEvent.click(fold);
  return (await screen.findByRole("button", { name: "Local chat" })).closest("tr")!;
}

it.each(["down", "stopped"])(
  "shows a neutral idle row for a stopped standby engine (%s), even with an old refusal",
  async (observation) => {
    mount(engineSnapshot({
      observation, tone: "neutral", in_use: false, last_error: "Connection refused",
    }));
    const row = await visibleChatRow();
    const idle = within(row).getByText("Idle, starts when needed");
    expect(idle).toBeVisible();
    expect(row).not.toHaveTextContent(/Ready|Down|Not running|h up/);
    expect(row.querySelector('[class*="danger"], [class*="warning"], [class*="destructive"]')).toBeNull();
    expect(within(row).queryByRole("img")).toBeNull();
    expect(screen.queryByRole("heading", { name: "Needs attention" })).toBeNull();
    await userEvent.click(within(row).getByRole("button", { name: "Local chat" }));
    expect(within(screen.getByRole("dialog", { name: "Local chat" })).getByText("Chat · Idle, starts when needed", { exact: true })).toBeVisible();
  },
);

it("counts idle engines separately from ready engines in the folded summary", async () => {
  const status = engineSnapshot({ observation: "down", tone: "neutral", in_use: false });
  status.current_health!.signals = [{
    source: "disk", tone: "warn", reasons: ["Disk needs attention."],
  }];
  mount(status);
  expect(await screen.findByRole("button", {
    name: "Engines: 1 idle, 1 turned off. Show all",
  })).toBeVisible();
});

it.each([
  ["chat", "Chat", "degraded"],
  ["video", "Video", "degraded"],
  ["chat", "Chat", "down"],
  ["video", "Video", "down"],
])("describes a sole %s (%s) engine that is %s without sampled failures", async (capability, label, observation) => {
  const serviceRegistry = {
    ...chatRegistry,
    services: { chat: { ...registry.services.chat, capabilities: [capability] } },
  };
  mount(engineSnapshot({
    observation,
    tone: observation === "down" ? "bad" : "warn",
    capability_title: label,
    capability_labels: [label],
  }), serviceRegistry);
  const title = await screen.findByRole("heading", {
    name: new RegExp(`^${label} (requests may be slow or fail|is unavailable) right now$`),
  });
  if (observation === "degraded") {
    expect(title).not.toHaveTextContent(/unavailable/i);
    expect(title).toHaveTextContent(`${label} requests may be slow or fail right now`);
  } else {
    expect(title).toHaveTextContent(`${label} is unavailable right now`);
  }
});

it.each([
  ["ConnectTimeout: timed out", "Did not answer in time"],
  ["Connection timed out", "Did not answer in time"],
  ["connect ETIMEDOUT 127.0.0.1:8000", "Did not answer in time"],
  ["Connection refused", "Not running"],
  ["ConnectionRefusedError", "Not running"],
  ["connect ECONNREFUSED 127.0.0.1:8000", "Not running"],
])("uses plain connection-failure copy for %s", async (last_error, word) => {
  mount(engineSnapshot({ observation: "down", tone: "bad", last_error }));
  await screen.findByRole("heading", { name: "Needs attention" });
  const row = await visibleChatRow();
  expect(within(row).getByText(word, { exact: true })).toBeVisible();
  const currentState = within(row).getAllByRole("cell").at(-1)!;
  expect(within(currentState).queryByText("Down", { exact: true })).toBeNull();
  expect(within(row).queryByText(last_error, { exact: true })).toBeNull();
  await userEvent.click(within(row).getByRole("button", { name: "Local chat" }));
  const sheet = screen.getByRole("dialog", { name: "Local chat" });
  expect(within(sheet).getByText(`Chat · ${word}`)).toBeVisible();
  const body = sheet.querySelector("dl")!;
  expect(body).not.toHaveTextContent(last_error);
  expect(body).not.toHaveTextContent("127.0.0.1:8000");
  expect(within(body).getByText(word, { exact: true })).toBeVisible();
  expect(within(body).getByText("Check the engine address and that it is running, then refresh.")).toBeVisible();
});

it("does not call a disabled engine idle", async () => {
  const disabledRegistry = {
    ...chatRegistry,
    services: { ...chatRegistry.services, chat: { ...chatRegistry.services.chat, enabled: false } },
  };
  mount(engineSnapshot({ observation: "down", tone: "neutral", in_use: false }), disabledRegistry);
  const row = await visibleChatRow();
  expect(within(row).getByText("Off", { exact: true })).toBeVisible();
  expect(within(row).queryByText("Idle, starts when needed")).toBeNull();
});

it("does not let an old refusal override a currently healthy engine", async () => {
  mount(engineSnapshot({ last_error: "Connection refused" }));
  const row = await visibleChatRow();
  expect(within(row).getByText("Ready", { exact: true })).toBeVisible();
  expect(within(row).queryByText("Not running")).toBeNull();
  await userEvent.click(within(row).getByRole("button", { name: "Local chat" }));
  expect(within(screen.getByRole("dialog", { name: "Local chat" })).queryByText("Next step")).toBeNull();
});

it("does not turn unavailable observations into idle or connection-failure claims", async () => {
  const status = engineSnapshot({ observation: "down", tone: "neutral", in_use: false, last_error: "Connection refused" });
  status.current_health!.observation_available = false;
  mount(status);
  const row = await visibleChatRow();
  expect(within(row).getByText("Unknown", { exact: true })).toBeVisible();
  expect(within(row).queryByText(/Idle, starts when needed|Not running/)).toBeNull();
});

it("shows recorded engine and provider check times in the always-visible state cell", async () => {
  const now = 1_800_000_000;
  vi.spyOn(Date, "now").mockReturnValue(now * 1000);
  homeCapabilities.rows = freshChat();
  providerState.state = "ready";
  providerState.providers = [{
    id: "remote", name: "Data provider", enabled: true, reachable: true,
    keyState: "set", spendText: "", checkedAt: now - 180,
  }];
  mount(engineSnapshot({}, now - 120));
  const engine = await visibleChatRow();
  const provider = screen.getByRole("button", { name: "Data provider details" }).closest("tr")!;
  for (const row of [engine, provider]) {
    const checked = within(row).getByText(/Last checked/);
    expect(checked).toHaveTextContent(/^Last checked .+ago$/);
    expect(checked).not.toHaveTextContent(/not recorded|just now/);
    expect(checked.closest("td")).not.toHaveClass("hidden");
  }
  expect(within(engine).getByText(/Last checked/)).not.toHaveTextContent(
    within(provider).getByText(/Last checked/).textContent!,
  );
});

it.each([null, 0, -1, Number.NaN, Number.POSITIVE_INFINITY])(
  "does not invent a check time from the payload generation time when the recorded time is %s",
  async (observedAt) => {
    mount(engineSnapshot({}, observedAt));
    const row = await visibleChatRow();
    expect(within(row).getByText("Last checked: not recorded")).toBeVisible();
  },
);

it("removes stale idle copy and check times after a failed refresh", async () => {
  mount(engineSnapshot({ observation: "down", tone: "neutral", in_use: false }));
  const row = await visibleChatRow();
  expect(within(row).getByText("Idle, starts when needed")).toBeVisible();
  vi.mocked(api.status).mockRejectedValue(new Error("offline"));
  await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
  await screen.findByRole("heading", { name: "Engine health couldn’t load" });
  const refreshed = await visibleChatRow();
  expect(within(refreshed).getByText("Unknown", { exact: true })).toBeVisible();
  expect(within(refreshed).getByText("Last checked: not recorded")).toBeVisible();
  expect(within(refreshed).queryByText("Idle, starts when needed")).toBeNull();
});

it("does not hide an unhealthy unused engine unless the gateway marks it neutral", async () => {
  mount(engineSnapshot({ observation: "down", tone: "bad", in_use: false, last_error: "Connection refused" }));
  const title = await screen.findByRole("heading", { name: "Chat is unavailable right now" });
  const card = title.closest("article")!;
  expect(within(card).getByText("Check the engine address, or start the engine.")).toBeVisible();
  expect(within(card).getByRole("link", { name: "Inspect Local chat" })).toHaveAttribute("href", "/engines/?service=chat");
  const row = await visibleChatRow();
  expect(within(row).getByText("Not running", { exact: true })).toBeVisible();
  expect(within(row).queryByText("Idle, starts when needed")).toBeNull();
});

it("gives a stale unchecked engine an honest next step instead of diagnosing an outage", async () => {
  mount(engineSnapshot({ observation: "unknown", tone: "neutral" }, Date.now() / 1000 - 86400));
  const title = await screen.findByRole("heading", { name: /Local chat has not answered a check in/ });
  const card = title.closest("article")!;
  expect(within(card).getByText("We cannot confirm what this engine can do until a new check succeeds.")).toBeVisible();
  expect(within(card).getByText("Check that the engine is running and its health address answers, then refresh status.")).toBeVisible();
  expect(within(card).getByRole("link", { name: "Inspect Local chat" })).toHaveAttribute("href", "/engines/?service=chat");
  expect(within(card).queryByText(/unavailable right now|is not running/)).toBeNull();
  const row = await visibleChatRow();
  expect(within(row).getByText("Unknown", { exact: true })).toBeVisible();
});

it.each([
  new TypeError("private transport detail"),
  new ApiError("failed", "private server detail", 503),
])("retries only failed health and restores current observations after refresh: %s", async (error) => {
  mount(engineSnapshot());
  await screen.findByText("Everything works");
  vi.mocked(api.status).mockRejectedValue(error);
  await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
  const card = (await screen.findByRole("heading", { name: "Engine health couldn’t load" })).closest("article")!;
  expect(within(card).getByText(error instanceof ApiError ? "The gateway returned an error. Try again." : "We couldn’t tell where this stopped. Try again.")).toBeVisible();
  expect(screen.queryByText(/private transport detail|private server detail/)).toBeNull();
  expect(screen.queryByText("Everything works")).toBeNull();
  const refreshed = await visibleChatRow();
  expect(within(refreshed).getByText("Unknown", { exact: true })).toBeVisible();
  expect(within(refreshed).getByText("Last checked: not recorded")).toBeVisible();
  const statusCalls = vi.mocked(api.status).mock.calls.length;
  const registryCalls = vi.mocked(api.services.registry).mock.calls.length;
  vi.mocked(api.status).mockResolvedValue(engineSnapshot());
  await userEvent.click(within(card).getByRole("button", { name: "Retry" }));
  await screen.findByText("Everything works");
  expect(api.status).toHaveBeenCalledTimes(statusCalls + 1);
  expect(api.services.registry).toHaveBeenCalledTimes(registryCalls);
  expect(screen.queryByRole("heading", { name: "Engine health couldn’t load" })).toBeNull();
  expect(within(await visibleChatRow()).getByText("Ready", { exact: true })).toBeVisible();
});

it("honesty: an intentionally off engine is Off despite a failed last observation", async () => {
  const disabledRegistry = {
    ...chatRegistry,
    services: { chat: { ...chatRegistry.services.chat, enabled: false } },
  };
  mount(engineSnapshot({ observation: "down", tone: "bad", last_error: "Connection refused" }), disabledRegistry);
  const row = await visibleChatRow();
  expect(within(row).getByText("Off", { exact: true })).toBeVisible();
  expect(row).not.toHaveTextContent(/Down|Not running|Ready|Degraded/);
  expect(screen.queryByRole("heading", { name: "Needs attention" })).toBeNull();
});

it("honesty: a failed engine reports Down and its unavailable capability", async () => {
  mount(engineSnapshot({ observation: "down", tone: "bad", last_error: null }));
  expect(await screen.findByRole("heading", { name: "Chat is unavailable right now" })).toBeVisible();
  const row = await visibleChatRow();
  const currentState = within(row).getAllByRole("cell").at(-1)!;
  expect(within(currentState).getByText("Down", { exact: true })).toBeVisible();
  expect(row).not.toHaveTextContent(/Ready|Off|Degraded/);
});

it("honesty: a failed refresh removes stale ready state and recovers after retry", async () => {
  const status = engineSnapshot();
  mount(status);
  const row = await visibleChatRow();
  expect(within(row).getByText("Ready", { exact: true })).toBeVisible();
  vi.mocked(api.status).mockRejectedValue(new Error("offline"));
  await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
  const heading = await screen.findByRole("heading", { name: "Engine health couldn’t load" });
  expect(screen.getByRole("status")).toHaveTextContent("Couldn’t check status");
  const refreshed = await visibleChatRow();
  expect(within(refreshed).getByText("Unknown", { exact: true })).toBeVisible();
  expect(within(refreshed).getByText("Last checked: not recorded")).toBeVisible();
  expect(within(refreshed).queryByText("Ready", { exact: true })).toBeNull();
  vi.mocked(api.status).mockResolvedValue(status);
  await userEvent.click(within(heading.closest("article")!).getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(within(screen.getByRole("button", { name: "Local chat" }).closest("tr")!).getByText("Ready", { exact: true })).toBeVisible());
  expect(screen.queryByRole("heading", { name: "Engine health couldn’t load" })).toBeNull();
});

it("honesty: a slow engine stays degraded without claiming its capability is unavailable", async () => {
  mount(engineSnapshot({ observation: "degraded", tone: "warn", last_error: null }));
  const row = await visibleChatRow();
  expect(within(row).getByText("Degraded", { exact: true })).toBeVisible();
  expect(screen.getByRole("status")).toHaveTextContent("Chat degraded");
  expect(screen.queryByRole("heading", { name: /Chat is unavailable right now/ })).toBeNull();
});

it("labels every history segment with its time range and worst state, with a matching down pattern", () => {
  const end = new Date(2026, 9, 14, 1, 30).getTime() / 1000;
  const start = end - 24 * 3600;
  render(<UptimeBar label="History" end={end} series={[
    [start + 1800, 1],
    [start + 3600 + 1800, 0.5],
    [start + 7200 + 900, 1],
    [start + 7200 + 1800, 0],
    [end, 1],
  ]} />);
  const history = screen.getByRole("group", { name: /History: 2 of 4 checked hours up/ });
  const segments = within(history).getAllByRole("img");
  expect(segments).toHaveLength(24);
  const time = (ts: number) => new Date(ts * 1000).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short",
  });
  segments.forEach((segment, index) => {
    const state = index === 0 || index === 23 ? "up" : index === 1 ? "degraded" : index === 2 ? "down" : "no check";
    const range = `${time(start + index * 3600)} to ${time(start + (index + 1) * 3600)}: ${state}`;
    expect(segment).toHaveAccessibleName(range);
    expect(segment).toHaveAttribute("title", range);
  });
  for (const word of ["Up", "Degraded", "Down", "No check"]) {
    expect(screen.getByText(word, { exact: true })).toBeVisible();
  }
  const down = segments[2] as HTMLElement;
  expect(down.style.backgroundImage).toContain("repeating-linear-gradient");
  expect(screen.getByText("Down", { exact: true }).querySelector("span")?.style.backgroundImage).toBe(down.style.backgroundImage);
  expect((segments[1] as HTMLElement).style.backgroundImage).toBe("");
  expect((segments[0] as HTMLElement).style.backgroundImage).toBe("");
});

it("keeps unobserved history explicit rather than reporting it up", () => {
  const end = 1_800_000_000;
  render(<UptimeBar label="History" end={end} series={[[NaN, 0], [end, NaN], [end - 86401, 0]]} />);
  const history = screen.getByRole("group", { name: "History: no checks recorded in the last 24 hours" });
  expect(within(history).getAllByRole("img", { name: /: no check$/ })).toHaveLength(24);
  expect(screen.getByText("No check", { exact: true })).toBeVisible();
  expect(screen.queryByText("Up", { exact: true })).toBeNull();
  expect(screen.queryByText("Down", { exact: true })).toBeNull();
});

it("updates disabled history labels and legend without retaining down cues", () => {
  const end = 1_800_000_000;
  const { rerender } = render(<UptimeBar label="History" end={end} series={[[end, 0]]} />);
  expect(screen.getByRole("img", { name: /: down$/ })).toBeInTheDocument();
  rerender(<UptimeBar label="History" end={end} series={[[end, 0]]} enabled={false} />);
  const history = screen.getByRole("group", { name: "History: turned off" });
  const segments = within(history).getAllByRole("img", { name: /: turned off$/ });
  expect(segments).toHaveLength(24);
  segments.forEach((segment) => expect((segment as HTMLElement).style.backgroundImage).toBe(""));
  expect(screen.getByText("Turned off", { exact: true })).toBeVisible();
  expect(screen.queryByText("Down", { exact: true })).toBeNull();
  expect(screen.queryByText("No check", { exact: true })).toBeNull();
});
