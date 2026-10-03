import type { ComponentPropsWithoutRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { axe } from "jest-axe";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const state = vi.hoisted(() => ({
  status: async (): Promise<any> => seededStatus,
  registry: async (): Promise<any> => seededRegistry,
  providers: {
    state: "hidden" as const,
    providers: [],
    raw: null,
    message: "hidden",
    refetch: vi.fn(),
  } as any,
  search: "",
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(state.search),
}));

vi.mock("next/link", () => ({
  default: ({ href, children, ...props }: ComponentPropsWithoutRef<"a">) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {},
  queryKeys: { status: ["status"], serviceRegistry: ["serviceRegistry"] },
  api: {
    status: () => state.status(),
    services: { registry: () => state.registry() },
  },
}));

vi.mock("@/lib/provider-status", () => ({
  useOptionalProviderStatus: () => state.providers,
}));

import AdminStatusPage from "@/app/admin/status/page";

const seededRegistry = {
  schema_version: 1,
  services: {
    chat: {
      id: "chat",
      name: "Local chat engine",
      kind: "llama",
      source: "ui",
      enabled: true,
      editable: true,
      capabilities: ["chat"],
      role: "primary chat",
      config: {
        base_url: "http://127.0.0.1:18081/v1",
        protocol: "openai-compatible",
        models: [{ model_group: "demo-chat", upstream_model: "demo-chat" }],
      },
    },
    image: {
      id: "image",
      name: "Local image engine",
      kind: "comfy",
      source: "ui",
      enabled: false,
      editable: true,
      capabilities: ["image", "video"],
      role: "image",
      config: { base_url: "http://127.0.0.1:18082" },
    },
    imageOnly: {
      id: "imageOnly",
      name: "Image-only engine",
      kind: "comfy",
      source: "ui",
      enabled: true,
      editable: true,
      capabilities: ["image"],
      role: "image",
      config: { base_url: "http://127.0.0.1:18083" },
    },
  },
};

const seededStatus = {
  current_health: {
    state: "warn",
    tone: "warn",
    label: "One warning",
    detail: "Disk warning.",
    observation_available: true,
    signals: [
      {
        source: "disk",
        tone: "warn",
        reasons: ["Media disk is above the warning threshold"],
      },
    ],
    services: [
      {
        service_id: "chat",
        name: "Local chat engine",
        configured: true,
        enabled: true,
        role: "primary chat",
        routable: true,
        observation: "up",
        tone: "ok",
        last_error: null,
      },
      {
        service_id: "image",
        name: "Local image engine",
        configured: true,
        enabled: false,
        role: "image",
        routable: null,
        observation: "disabled",
        tone: "neutral",
        last_error: null,
      },
      {
        service_id: "imageOnly",
        name: "Image-only engine",
        configured: true,
        enabled: true,
        role: "image",
        routable: true,
        observation: "up",
        tone: "ok",
        last_error: null,
      },
    ],
  },
  backends: [{ service_id: "chat", url: "http://127.0.0.1:18081/v1" }],
  gpus: [{ index: 0, memory_used_mb: 12288, memory_total_mb: 24576 }],
  budgets: { gpu0: { total_gb: 24, headroom_gb: 4 } },
  ram_attribution: {
    total_kb: 64 * 1024 * 1024,
    available_kb: 24 * 1024 * 1024,
  },
  disk: {
    partitions: [
      {
        source: "/dev/nvme0n1p2",
        target: "/media",
        size_bytes: 1000,
        used_bytes: 780,
        avail_bytes: 220,
        pct_used: 78,
      },
    ],
    hotspots: [],
  },
  thermal: {
    amdgpu_edge_c: 95,
    amdgpu_thermal_state: "warn",
  },
};

function renderStatus() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <AdminStatusPage />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  state.status = async () => seededStatus;
  state.registry = async () => seededRegistry;
  state.providers = {
    state: "hidden",
    providers: [],
    raw: null,
    message: "hidden",
    refetch: vi.fn(),
  };
  state.search = "";
});

describe("status page", () => {
  it("renders seeded engine, hardware and host warning data honestly", async () => {
    renderStatus();
    expect(
      await screen.findByRole("heading", { name: "Status", level: 1 }),
    ).toBeVisible();
    expect(
      await screen.findByText(
        "disk needs owner action: Media disk is above the warning threshold.",
      ),
    ).toBeVisible();
    expect(screen.getByText("Chat (Openai Compatible)")).toBeVisible();
    expect(screen.getByText("Image (Comfy)")).toBeVisible();
    expect(screen.getAllByText("Loaded:").length).toBeGreaterThanOrEqual(1);
    expect(
      screen.getAllByText("Not measured yet").length,
    ).toBeGreaterThanOrEqual(2);
    expect(
      screen.getByText("Budget: 20 GB allocatable from 24 GB total."),
    ).toBeVisible();
    expect(screen.getByText("/media")).toBeVisible();
    expect(screen.getByText("78% used")).toBeVisible();
    expect(screen.getByText(/amdgpu_edge_c: 95 °C/)).toBeVisible();
    expect(screen.getByText(/Watch cooling before long jobs/)).toBeVisible();
    expect(
      screen.getAllByText(/Media disk is above the warning threshold/).length,
    ).toBeGreaterThanOrEqual(1);
  });

  it("hides disabled engines until requested", async () => {
    renderStatus();
    await screen.findByText("Chat (Openai Compatible)");
    expect(screen.queryByText("Image & video (Comfy)")).toBeNull();
    await userEvent.click(screen.getByText("Show disabled"));
    expect(screen.getByText("Image & video (Comfy)")).toBeVisible();
  });

  it("preserves query-string service focus even when disabled engines are hidden", async () => {
    state.search = "service=image";
    renderStatus();
    const selected = await screen.findByText("Image & video (Comfy)");
    expect(selected.closest("article")).toHaveAttribute("aria-current", "true");
  });

  it("does not paint unmeasured observations green when a tone is contradictory", async () => {
    state.status = async () => ({
      ...seededStatus,
      current_health: {
        ...seededStatus.current_health,
        signals: [],
        services: [
          {
            ...seededStatus.current_health.services[0],
            observation: "unavailable",
            tone: "ok",
          },
        ],
      },
    });
    state.registry = async () => ({
      ...seededRegistry,
      services: { chat: seededRegistry.services.chat },
    });
    renderStatus();
    const row = (await screen.findByText("Chat (Openai Compatible)")).closest(
      "article",
    );
    expect(row).not.toBeNull();
    expect(
      within(row as HTMLElement).getAllByText("Not measured yet").length,
    ).toBeGreaterThanOrEqual(1);
    expect(within(row as HTMLElement).queryByText("Ready")).toBeNull();
  });

  it("does not call a degraded engine busy or a standby engine starting", async () => {
    state.status = async () => ({
      current_health: {
        ...seededStatus.current_health,
        signals: [],
        services: [
          {
            ...seededStatus.current_health.services[0],
            observation: "degraded",
            tone: "warn",
          },
          {
            ...seededStatus.current_health.services[0],
            service_id: "standby",
            name: "Standby",
            observation: "standby_stopped",
            tone: "neutral",
          },
        ],
      },
      backends: [],
    });
    state.registry = async () => ({
      ...seededRegistry,
      services: {
        ...seededRegistry.services,
        standby: {
          ...seededRegistry.services.chat,
          id: "standby",
          name: "Standby",
        },
      },
    });
    renderStatus();
    expect(await screen.findByText("Degraded")).toBeVisible();
    expect(screen.getByText("Standby, stopped")).toBeVisible();
    expect(screen.queryByText("Busy")).toBeNull();
    expect(screen.queryByText("Starting")).toBeNull();
  });

  it("reports enabled provider failures before all-clear engine copy", async () => {
    state.status = async () => ({
      ...seededStatus,
      current_health: { ...seededStatus.current_health, signals: [] },
    });
    state.providers = {
      state: "ready",
      providers: [
        {
          id: "openrouter",
          name: "OpenRouter",
          enabled: true,
          reachable: false,
          keyState: "set",
          spendText: "$0.00",
          tone: "bad",
        },
      ],
      raw: { providers: [] },
      message: "ready",
      refetch: vi.fn(),
    };
    renderStatus();
    expect(
      await screen.findByText(
        "OpenRouter needs owner action: provider is unreachable.",
      ),
    ).toBeVisible();
    expect(screen.getByText("OpenRouter")).toBeVisible();
  });

  it("renders empty and error states without green claims", async () => {
    state.registry = async () => ({ schema_version: 1, services: {} });
    state.status = async () => ({
      current_health: {
        state: "empty",
        tone: "neutral",
        label: "No engines",
        detail: "None",
        observation_available: false,
        services: [],
      },
    });
    const { rerender } = renderStatus();
    expect(
      await screen.findByText("No engines are configured yet."),
    ).toBeVisible();
    cleanup();
    state.registry = async () => seededRegistry;
    state.status = async () => {
      throw new Error("Status probe failed");
    };
    renderStatus();
    expect(
      await screen.findByText(
        "Status is unavailable; the engine configuration is shown without live health.",
      ),
    ).toBeVisible();
    expect(screen.getByText("Status probe failed")).toBeVisible();
    void rerender;
  });

  it("has no serious accessibility violations in the seeded state", async () => {
    renderStatus();
    await screen.findByText("Chat (Openai Compatible)");
    const accessibility = await axe(document.body);
    expect(
      accessibility.violations.filter((v) =>
        ["serious", "critical"].includes(v.impact ?? ""),
      ),
    ).toEqual([]);
  });
});
