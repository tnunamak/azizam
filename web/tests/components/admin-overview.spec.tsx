import type { ComponentPropsWithoutRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const apiState = vi.hoisted(() => ({
  apps: vi.fn(),
  audit: vi.fn(),
  credentials: vi.fn(),
  overview: vi.fn(),
  registry: vi.fn(),
  status: vi.fn(),
  users: vi.fn(),
}));

vi.mock("next/link", () => ({
  default: ({ href, children, ...props }: ComponentPropsWithoutRef<"a">) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

vi.mock("@/lib/api", () => ({
  api: {
    apps: { list: apiState.apps },
    audit: { list: apiState.audit },
    credentials: { list: apiState.credentials },
    overview: apiState.overview,
    services: { registry: apiState.registry },
    status: apiState.status,
    users: { list: apiState.users },
  },
  queryKeys: {
    apps: ["apps"],
    auditRecent: ["audit", "recent"],
    credentials: ["credentials"],
    overview: ["overview"],
    serviceRegistry: ["serviceRegistry"],
    status: ["status"],
    users: ["users"],
  },
}));

import AdminOverviewPage from "@/app/admin/page";

const app = {
  id: "app_1",
  kind: "service" as const,
  name: "Relay",
  owner_user_id: "user_1",
  owner_service_account_id: null,
  default_profile_id: null,
  created_at: 2,
  disabled_at: null,
  effective_profile_id: "profile_1",
  effective_profile_name: "Default",
  effective_profile_source: "fallback" as const,
};
const user = {
  id: "user_1",
  issuer: "local",
  subject: "owner",
  email: "owner@example.test",
  display_name: "Owner",
  name: null,
  created_at: 1,
  last_login_at: null,
  roles: [],
};
const credential = {
  id: "cred_1",
  subject_kind: "app" as const,
  subject_id: "app_1",
  app_id: "app_1",
  profile_id: null,
  prefix: "opk_test",
  display_name: "Relay key",
  scopes: [],
  created_at: 1,
  expires_at: null,
  revoked_at: null,
  last_used_at: null,
};
const audit = {
  id: "audit_1",
  created_at: 1,
  actor_kind: "user" as const,
  actor_id: "user_1",
  action: "app.create",
  target_kind: "app",
  target_id: "app_1",
  metadata: { name: "Relay" },
};

function pending<T>() {
  return new Promise<T>(() => undefined);
}

function renderOverview(seed?: (client: QueryClient) => void) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  seed?.(client);
  return render(
    <QueryClientProvider client={client}>
      <AdminOverviewPage />
    </QueryClientProvider>,
  );
}

function setResolvedDefaults() {
  apiState.apps.mockResolvedValue([app]);
  apiState.audit.mockResolvedValue({ records: [audit] });
  apiState.credentials.mockResolvedValue([credential]);
  apiState.overview.mockResolvedValue({ attention: [] });
  apiState.registry.mockResolvedValue({ services: {} });
  apiState.status.mockResolvedValue({ current_health: null });
  apiState.users.mockResolvedValue([user]);
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Admin Overview query-state behavior", () => {
  it("keeps loaded activity visible while user attribution is pending", async () => {
    setResolvedDefaults();
    apiState.users.mockImplementation(() => pending());

    renderOverview((client) => {
      client.setQueryData(["audit", "recent"], [audit]);
    });

    expect(await screen.findByTitle("Relay")).toBeVisible();
    expect(screen.getByText("Created app")).toBeVisible();
    expect(screen.getByText("Owner loading")).toBeVisible();
    expect(screen.queryByText("Unknown user")).not.toBeInTheDocument();
    expect(document.querySelectorAll(".animate-pulse")).toHaveLength(0);
  });

  it("keeps loaded apps visible while key attribution is pending", async () => {
    setResolvedDefaults();
    apiState.credentials.mockImplementation(() => pending());

    const { container } = renderOverview();

    expect(await screen.findByTitle("Relay")).toBeVisible();
    expect(screen.getByText("Keys loading")).toBeVisible();
    expect(screen.queryByText("Keys unavailable")).not.toBeInTheDocument();
    expect(screen.queryByText("0 keys")).not.toBeInTheDocument();
    expect(container.querySelectorAll(".animate-pulse")).toHaveLength(0);
  });

  it("shows independent users and credentials failures without clearing app rows, and retries locally", async () => {
    setResolvedDefaults();
    apiState.users.mockRejectedValue(new Error("users unavailable"));
    apiState.credentials.mockRejectedValue(
      new Error("credentials unavailable"),
    );

    renderOverview();

    expect(await screen.findByText("Couldn't load app owners")).toBeVisible();
    expect(screen.getByText("Couldn't load app key facts")).toBeVisible();
    expect(screen.getByTitle("Relay")).toBeVisible();
    expect(screen.getByText("Owner unavailable")).toBeVisible();
    expect(screen.getByText("Keys unavailable")).toBeVisible();

    const usersRetry = within(
      screen.getByText("Couldn't load app owners").closest('[role="alert"]')!,
    ).getByRole("button", { name: "Retry" });
    await usersRetry.click();
    expect(apiState.users).toHaveBeenCalledTimes(2);
  });

  it("retains known app owner and key facts when a refresh fails", async () => {
    setResolvedDefaults();
    apiState.users.mockRejectedValue(new Error("users refresh unavailable"));
    apiState.credentials.mockRejectedValue(
      new Error("credentials refresh unavailable"),
    );

    renderOverview((client) => {
      client.setQueryData(["apps"], [app]);
      client.setQueryData(["users"], [user]);
      client.setQueryData(["credentials"], [credential]);
    });

    expect(
      await screen.findByText("App owner details may be stale"),
    ).toBeVisible();
    expect(screen.getByText("App key facts may be stale")).toBeVisible();
    expect(screen.getAllByText("Owner").length).toBeGreaterThan(0);
    expect(screen.getByText("1 key")).toBeVisible();
  });

  it("uses activity-only loading geometry and one visible page h1", async () => {
    setResolvedDefaults();
    apiState.audit.mockImplementation(() => pending());

    const { container } = renderOverview();

    expect(
      await screen.findByRole("heading", { name: "Overview", level: 1 }),
    ).toBeVisible();
    await screen.findByTitle("Relay");
    expect(container.querySelectorAll("h1")).toHaveLength(1);
    expect(container.querySelector("h1.sr-only")).toBeNull();
    expect(document.querySelectorAll(".animate-pulse")).toHaveLength(3);
  });

  it("does not claim freshness is unavailable when the source omits a timestamp on an otherwise healthy projection", async () => {
    setResolvedDefaults();
    apiState.status.mockResolvedValue({
      current_health: {
        state: "ok",
        tone: "ok",
        label: "Operational",
        detail: "Current configured-service health is normal.",
        observation_available: true,
        services: [],
      },
      // generated_at intentionally omitted, as a synthetic/fixture status
      // provider may do -- this must not be read as "health is stale".
    });

    renderOverview();

    expect(await screen.findByText("Operational")).toBeVisible();
    expect(
      screen.getByText("Current configured-service health is normal."),
    ).toBeVisible();
    expect(screen.queryByText(/Freshness unavailable/i)).not.toBeInTheDocument();
  });

  it("shows real freshness when the source supplies a generation timestamp", async () => {
    setResolvedDefaults();
    const generatedAt = Math.floor(Date.now() / 1000) - 5;
    apiState.status.mockResolvedValue({
      generated_at: generatedAt,
      current_health: {
        state: "ok",
        tone: "ok",
        label: "Operational",
        detail: "Current configured-service health is normal.",
        observation_available: true,
        services: [],
      },
    });

    renderOverview();

    expect(await screen.findByText("Operational")).toBeVisible();
    expect(screen.getByText(/Updated just now/i)).toBeVisible();
  });

  it("caps and groups a large unbounded attention list instead of dumping every row", async () => {
    setResolvedDefaults();
    const attention = Array.from({ length: 24 }, (_, i) => ({
      kind: "app",
      severity: "attention" as const,
      title: `Fixture page ${String(i).padStart(2, "0")} has no active key`,
      detail: "Mint a key so it can reach the gateway.",
      target_kind: "app",
      target_id: `app_${i}`,
    }));
    apiState.overview.mockResolvedValue({ attention });

    renderOverview();

    expect(await screen.findByText("24 items need attention")).toBeVisible();
    // Grouped into one summarized row rather than 24 identical rows.
    expect(screen.getByText("24 apps need attention")).toBeVisible();
    expect(
      screen.queryByText("Fixture page 00 has no active key"),
    ).not.toBeInTheDocument();
  });

  it("keeps small distinct attention groups as individual rows with real targets", async () => {
    setResolvedDefaults();
    apiState.overview.mockResolvedValue({
      attention: [
        {
          kind: "app",
          severity: "attention",
          title: "Relay has no active key",
          detail: "Mint a key so it can reach the gateway.",
          target_kind: "app",
          target_id: "app_1",
        },
        {
          kind: "service",
          severity: "problem",
          title: "Backend is down",
          detail: "A required service cannot route requests.",
          target_kind: "service",
          target_id: "svc_1",
        },
      ],
    });

    renderOverview();

    expect(await screen.findByText("2 items need attention")).toBeVisible();
    expect(screen.getByText("Relay has no active key")).toBeVisible();
    expect(screen.getByText("Backend is down")).toBeVisible();
  });

  it("shows all four quickstart steps including sending a test request", async () => {
    setResolvedDefaults();

    renderOverview();

    await screen.findByText("Create an app");
    expect(screen.getByText("Generate an API key")).toBeVisible();
    expect(screen.getByText("Configure a service")).toBeVisible();
    expect(screen.getByText("Send a test request")).toBeVisible();
  });

  it("marks the test-request step complete once real request telemetry exists", async () => {
    setResolvedDefaults();
    apiState.status.mockResolvedValue({
      current_health: null,
      auth_keys: [{ app_id: "app_1", requests_24h: 42 }],
    });

    renderOverview();

    await screen.findByText("Send a test request");
    await waitFor(() => {
      const step = screen
        .getByText("Send a test request")
        .closest('[role="listitem"]');
      expect(step).toHaveClass("vf-quickstart-strip-step-done");
    });
  });
});
