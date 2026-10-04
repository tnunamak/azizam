import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api, type ServiceRegistry, type StatusPayload } from "@/lib/api";
import { StatusClient } from "./status-client";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@/lib/provider-status", () => ({
  useOptionalProviderStatus: () => ({ state: "absent", refetch: vi.fn() }),
}));
vi.mock("next/link", () => ({
  default: ({ children, href }: React.ComponentProps<"a">) => (
    <a href={href}>{children}</a>
  ),
}));
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
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
  },
};
function mount(status?: StatusPayload) {
  vi.spyOn(api.services, "registry").mockResolvedValue(registry);
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
  render(
    <QueryClientProvider
      client={
        new QueryClient({
          defaultOptions: { queries: { retry: false, gcTime: 0 } },
        })
      }
    >
      <StatusClient />
    </QueryClientProvider>,
  );
}
it("shows compact grouped engine rows and moves configuration and unknown metrics into a sheet", async () => {
  const user = userEvent.setup();
  mount();
  const engine = await screen.findByRole("button", { name: "Local chat" });
  expect(
    screen.getByRole("table", { name: /Chat engine status/ }),
  ).toBeVisible();
  expect(screen.queryByText("Not measured yet")).not.toBeInTheDocument();
  expect(screen.queryByText("Loaded:")).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Image engine" }),
  ).not.toBeInTheDocument();
  expect(screen.queryByText("fast")).not.toBeInTheDocument();
  await user.click(engine);
  const sheet = screen.getByRole("dialog", { name: "Local chat" });
  expect(within(sheet).getByText("Queue")).toBeVisible();
  expect(within(sheet).getByText("Not measured yet")).toBeVisible();
  expect(
    within(sheet).getByRole("link", { name: "Configure engine" }),
  ).toHaveAttribute("href", "/engines?service=chat");
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(engine).toHaveFocus();
});
it("reveals disabled engines only on request", async () => {
  const user = userEvent.setup();
  mount();
  await screen.findByRole("button", { name: "Local chat" });
  await user.click(screen.getByRole("switch"));
  expect(screen.getByRole("button", { name: "Image engine" })).toBeVisible();
  expect(screen.getByText("Disabled")).toBeVisible();
});
it("uses server engine names and transcription taxonomy and preserves an exact disk warning sentence", async () => {
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
  expect(
    await screen.findByRole("button", { name: "Canary (transcription)" }),
  ).toBeVisible();
  expect(
    screen.getByRole("table", { name: /Transcription engine status/ }),
  ).toBeVisible();
  expect(screen.getByRole("cell", { name: "Transcription" })).toBeVisible();
  expect(screen.getByText("System disk (/)", { exact: true })).toBeVisible();
  expect(
    within(
      screen
        .getByRole("heading", { name: "Status", level: 1 })
        .closest("header")!,
    ).getByText("System disk (/) is almost full (96%). Remove unused files.", {
      exact: true,
    }),
  ).toBeVisible();
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
  expect(
    await screen.findByRole("button", { name: "Canary (transcription)" }),
  ).toBeVisible();
  expect(screen.queryByText("Ready", { exact: true })).not.toBeInTheDocument();
  expect(screen.getByText("Unknown", { exact: true })).toBeVisible();
});
