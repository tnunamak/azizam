import { afterEach, expect, it, vi } from "vitest";
import {
  cleanup,
  render,
  screen,
  within,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api, type ServiceRegistry, type StatusPayload } from "@/lib/api";
import { StatusClient } from "./status-client";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));
const providerState = vi.hoisted(() => ({ state: "hidden" }));
vi.mock("@/lib/provider-status", () => ({
  useOptionalProviderStatus: () => ({
    ...providerState,
    providers: [],
    refetch: vi.fn(),
  }),
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
});

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
  expect(within(row).getByText("Turned off")).toBeVisible();
  expect(within(row).queryByText("Ready")).not.toBeInTheDocument();
});

it("uses the registry display name and excludes providers from the engine table", async () => {
  mount(undefined, registry);
  expect(
    await screen.findByRole("button", { name: "Realtime voice" }),
  ).toBeVisible();
  expect(screen.queryByRole("button", { name: "Speech To Speech" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Remote provider" })).toBeNull();
  expect(screen.queryByText("Capabilities unavailable")).toBeNull();
  expect(screen.queryByText("Unknown")).toBeNull();
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
      "The gateway’s computer needs attention.",
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
  expect(screen.getByRole("cell", { name: "Transcription" })).toBeVisible();
  expect(
    within(
      screen
        .getByRole("heading", { name: "Status", level: 1 })
        .closest("header")!,
    ).getByText("The gateway’s computer needs attention.", { exact: true }),
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
  expect(screen.getByRole("status")).toHaveTextContent(
    "Engine health is unknown.",
  );
  expect(screen.queryByText("Everything is working.")).toBeNull();
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
    expect(screen.getByRole("status")).toHaveTextContent(
      "Chat is unavailable.",
    ),
  );
  expect(
    screen.getAllByText("Local chat is not accepting connections.", {
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
    within(header).queryByText("Local chat is not accepting connections.", {
      exact: true,
    }),
  ).not.toBeInTheDocument();
  expect(
    screen.getByRole("link", { name: "Inspect Local chat" }),
  ).toHaveAttribute("href", "/engines/?service=chat");
  await userEvent.click(screen.getByRole("button", { name: /Show all/ }));
  await userEvent.click(screen.getByRole("button", { name: "Local chat" }));
  expect(
    within(screen.getByRole("dialog", { name: "Local chat" })).getByText(
      "Connection refused",
      { exact: true },
    ),
  ).toBeVisible();
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
    expect(screen.getByRole("status")).toHaveTextContent(
      "Chat is unavailable.",
    ),
  );
  expect(screen.queryByText(/problems need you/)).toBeNull();
  const attention = screen
    .getByRole("heading", { name: "Needs attention" })
    .closest("section")!;
  expect(within(attention).getAllByRole("article")).toHaveLength(1);
  expect(
    within(attention).getAllByText(/is not accepting connections\./),
  ).toHaveLength(1);
  expect(
    within(attention).getByText(/^2 engines:/),
  ).toBeVisible();
  expect(
    within(attention).getByRole("link", { name: "Inspect engines" }),
  ).toHaveAttribute("href", "/engines/");
});

it.each(["loading", "error"])(
  "does not claim all-clear while providers are %s",
  async (state) => {
    providerState.state = state;
    mount();
    await screen.findByRole("button", { name: "Local chat" });
    expect(screen.getByRole("status")).toHaveTextContent(
      state === "loading" ? "Checking…" : "Engines work; providers unknown.",
    );
    expect(screen.queryByText("Everything is working.")).toBeNull();
    expect(screen.queryByText("Engines are working.")).toBeNull();
  },
);
it("does not claim all-clear when current health is absent", async () => {
  mount({});
  await screen.findByRole("button", { name: "Local chat" });
  expect(screen.getByRole("status")).toHaveTextContent(
    "Engine health is unknown.",
  );
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
  await screen.findByText("Could not check engine status");
  expect(screen.getByRole("status")).toHaveTextContent(
    "Couldn’t check status.",
  );
});
it("announces the answer atomically as health changes", async () => {
  const { engineHealthPresentation } =
    await import("@/lib/health-presentation");
  expect(
    engineHealthPresentation(true, true, undefined, "degraded").label,
  ).toBe("Degraded");
  mount();
  await screen.findByText("Everything is working.");
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
  await waitFor(() => expect(live).toHaveTextContent("Chat is degraded."));
  expect(live.querySelector("table")).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: /Show all/ }));
  expect(screen.getByText("Degraded", { exact: true })).toBeVisible();
});
it("moves from partial to healthy once provider status resolves", async () => {
  providerState.state = "error";
  mount();
  await screen.findByText("Engines work; providers unknown.");
  expect(screen.queryByText("Everything is working.")).toBeNull();
  providerState.state = "ready";
  await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
  await waitFor(() =>
    expect(screen.getByRole("status")).toHaveTextContent(
      "Everything is working.",
    ),
  );
});
it.each(["empty", "partial"])(
  "presents %s observation explicitly",
  async (state) => {
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
        state === "empty" ? "No engines yet." : "Some engines are unchecked.",
      ),
    );
    expect(screen.queryByText("Everything is working.")).toBeNull();
  },
);
