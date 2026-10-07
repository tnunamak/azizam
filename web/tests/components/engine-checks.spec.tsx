import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  applyDetectedModels,
  EngineChecks,
} from "../../src/app/engines/engine-checks";

const apiMock = vi.hoisted(() => ({
  services: {
    testConnection: vi.fn(),
    detectModels: vi.fn(),
  },
}));
const adminRegistryMutationMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  api: apiMock,
  adminRegistryMutation: adminRegistryMutationMock,
}));

const service = {
  id: "svc-demo",
  name: "Demo engine",
  kind: "openai-compatible",
  source: "ui",
  role: "optional",
  priority: 10,
  enabled: true,
  editable: true,
  capabilities: ["chat"],
  config: {
    host: "127.0.0.1",
    port: 18123,
    base_path: "/v1",
    models: [
      {
        id: "legacy-chat",
        model_group: "legacy-chat",
        upstream_model: "legacy-upstream",
        routing_hint: "keep-me",
      },
    ],
  },
};

function renderChecks(overrides: Partial<typeof service> = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <EngineChecks
        service={{ ...service, ...overrides } as never}
        endpoint="http://127.0.0.1:18123/v1"
        onSaved={vi.fn()}
        onEdit={vi.fn()}
      />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

function registryResponse({
  etag = "registry-v1",
  services = { [service.id]: service },
  ok = true,
  status = 200,
}: {
  etag?: string | null;
  services?: Record<string, unknown>;
  ok?: boolean;
  status?: number;
} = {}) {
  return {
    ok,
    status,
    headers: { get: vi.fn((name: string) => (name === "etag" ? etag : null)) },
    json: vi.fn(async () => ({ service_registry: { services } })),
  };
}

function mockRegistryFetch(response: unknown) {
  vi.stubGlobal("fetch", vi.fn(async () => response));
}

describe("EngineChecks", () => {
  it("previews detected models without saving before access consent", async () => {
    const user = userEvent.setup();
    apiMock.services.detectModels.mockResolvedValue({
      ok: true,
      models: ["demo-chat"],
      error: "",
    });

    renderChecks();
    await user.click(screen.getByRole("button", { name: "Detect" }));

    expect(await screen.findByText("Preview detected models")).toBeVisible();
    expect(screen.getByDisplayValue("demo-chat")).toBeVisible();
    expect(
      screen.getByText(/No access plan is edited here/i),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Apply models" })).toBeDisabled();
    expect(adminRegistryMutationMock).not.toHaveBeenCalled();
  });

  it("applies edited public mappings after explicit access consent", async () => {
    const user = userEvent.setup();
    apiMock.services.detectModels.mockResolvedValue({
      ok: true,
      models: ["demo-chat"],
      error: "",
    });
    mockRegistryFetch(
      registryResponse({
        etag: "registry-v7",
        services: {
          [service.id]: {
            ...service,
            config: {
              ...service.config,
              models: [
                ...service.config.models,
                {
                  id: "concurrent",
                  model_group: "concurrent",
                  upstream_model: "other-tab",
                  request_policy: { keep: true },
                },
              ],
            },
          },
        },
      }),
    );
    adminRegistryMutationMock.mockResolvedValue({
      service_registry: { services: {} },
    });

    renderChecks();
    await user.click(screen.getByRole("button", { name: "Detect" }));
    await user.clear(await screen.findByDisplayValue("demo-chat"));
    await user.type(screen.getByLabelText("Public name for demo-chat"), "chat");
    await user.click(
      screen.getByRole("checkbox", {
        name: "Use existing access rules for these models",
      }),
    );
    await user.click(screen.getByRole("button", { name: "Apply models" }));

    await waitFor(() =>
      expect(adminRegistryMutationMock).toHaveBeenCalledOnce(),
    );
    expect(fetch).toHaveBeenCalledWith("/admin/api/operator/service-registry", {
      credentials: "same-origin",
      cache: "no-store",
    });
    expect(adminRegistryMutationMock).toHaveBeenCalledWith(
      "/admin/api/operator/services/svc-demo",
      {
        method: "PATCH",
        headers: { "If-Match": "registry-v7" },
        body: {
          model_policy_schema_version: 1,
          models: [
            {
              id: "legacy-chat",
              model_group: "legacy-chat",
              upstream_model: "legacy-upstream",
              routing_hint: "keep-me",
            },
            {
              id: "concurrent",
              model_group: "concurrent",
              upstream_model: "other-tab",
              request_policy: { keep: true },
            },
            {
              id: "chat",
              model_group: "chat",
              upstream_model: "demo-chat",
              transforms: [],
            },
          ],
        },
      },
    );
    expect(await screen.findByText(/Public model names are saved/i)).toBeVisible();
  });

  it("names failed connection target, status, and recovery actions", async () => {
    const user = userEvent.setup();
    apiMock.services.testConnection.mockResolvedValue({
      ok: false,
      url: "http://127.0.0.1:18123/v1/models",
      status: 503,
      error: "backend unavailable",
    });

    renderChecks();
    await user.click(screen.getByRole("button", { name: "Test" }));

    expect(await screen.findByText("Demo engine: connection failed")).toBeVisible();
    expect(
      screen.getByText(/http:\/\/127\.0\.0\.1:18123\/v1\/models \(HTTP 503\)/),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Retry test" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Edit endpoint" })).toBeVisible();
  });

  it("handles empty detection, request errors, cancel, and retry", async () => {
    const user = userEvent.setup();
    apiMock.services.detectModels
      .mockResolvedValueOnce({ ok: true, models: [], error: "" })
      .mockRejectedValueOnce(new Error("models endpoint timed out"))
      .mockResolvedValueOnce({ ok: true, models: ["retry-chat"], error: "" });

    renderChecks();
    await user.click(screen.getByRole("button", { name: "Detect" }));
    expect(await screen.findByText("Demo engine: no models detected")).toBeVisible();
    expect(screen.getByText(/Load a model, then detect again/i)).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Detect" }));
    expect(await screen.findByText("Demo engine: detection failed")).toBeVisible();
    expect(screen.getByText(/models endpoint timed out/i)).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Retry detection" }));
    expect(await screen.findByDisplayValue("retry-chat")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Cancel preview" }));
    expect(screen.queryByText("Preview detected models")).not.toBeInTheDocument();
  });

  it("fails apply when the registry cannot be safely refreshed", async () => {
    mockRegistryFetch(registryResponse({ ok: false, status: 503 }));
    await expect(applyDetectedModels("svc-demo", [])).rejects.toThrow(
      "HTTP 503",
    );

    mockRegistryFetch(registryResponse({ etag: null }));
    await expect(applyDetectedModels("svc-demo", [])).rejects.toThrow(
      "Registry version is unavailable",
    );

    mockRegistryFetch(registryResponse({ services: {} }));
    await expect(applyDetectedModels("svc-demo", [])).rejects.toThrow(
      "This engine was removed",
    );

    mockRegistryFetch(
      registryResponse({
        services: { [service.id]: { ...service, editable: false } },
      }),
    );
    await expect(applyDetectedModels("svc-demo", [])).rejects.toThrow(
      "managed by the gateway",
    );
    expect(adminRegistryMutationMock).not.toHaveBeenCalled();
  });
});

describe("EngineChecks managed recovery", () => {
  it("hides endpoint editing after a managed connection failure and keeps retry working", async () => {
    const user = userEvent.setup();
    apiMock.services.testConnection
      .mockResolvedValueOnce({
        ok: false,
        url: "http://127.0.0.1:18123/v1/models",
        status: 503,
        error: "backend unavailable",
      })
      .mockResolvedValueOnce({
        ok: true,
        url: "http://127.0.0.1:18123/v1/models",
        status: 200,
      });

    renderChecks({ editable: false });
    await user.click(screen.getByRole("button", { name: "Test" }));
    expect(await screen.findByText("Demo engine: connection failed")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Edit endpoint" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Detect" })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: "Retry test" }));
    expect(await screen.findByText("Demo engine: connection passed")).toBeVisible();
    expect(apiMock.services.testConnection).toHaveBeenCalledTimes(2);
  });

  it("hides endpoint editing after managed detection errors and keeps retry working", async () => {
    const user = userEvent.setup();
    apiMock.services.detectModels
      .mockRejectedValueOnce(new Error("models endpoint timed out"))
      .mockResolvedValueOnce({ ok: true, models: ["retry-chat"], error: "" });

    renderChecks({ editable: false });
    await user.click(screen.getByRole("button", { name: "Detect" }));
    expect(await screen.findByText("Demo engine: detection failed")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Edit endpoint" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Test" })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: "Retry detection" }));
    expect(await screen.findByDisplayValue("retry-chat")).toBeVisible();
    expect(apiMock.services.detectModels).toHaveBeenCalledTimes(2);
    expect(
      screen.queryByRole("button", { name: "Apply models" }),
    ).not.toBeInTheDocument();
  });
});
