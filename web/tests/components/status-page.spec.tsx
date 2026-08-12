import type { ComponentPropsWithoutRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const statusState = vi.hoisted(() => ({
  load: async (): Promise<any> => ({ current_health: null }),
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("next/link", () => ({
  default: ({ href, children, ...props }: ComponentPropsWithoutRef<"a">) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

vi.mock("@/lib/api", () => ({
  api: { status: () => statusState.load() },
  queryKeys: { status: ["status"] },
}));

import AdminStatusPage from "@/app/admin/status/page";

function health(overrides: Record<string, unknown> = {}) {
  return {
    state: "ok",
    tone: "ok",
    label: "All configured services are available",
    detail: "Live observations are current.",
    observation_available: true,
    services: [
      {
        service_id: "gateway",
        name: "Gateway API",
        configured: true,
        enabled: true,
        role: "required",
        routable: true,
        observation: "up",
        tone: "ok",
        last_error: null,
      },
    ],
    ...overrides,
  };
}

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
});

describe("admin Status", () => {
  it("renders a read-first loading state before observations arrive", () => {
    statusState.load = () => new Promise(() => undefined);
    renderStatus();

    expect(screen.getByText("Checking current gateway health.")).toBeVisible();
  });

  it("uses one factual metric strip for a healthy projection", async () => {
    statusState.load = async () => ({ current_health: health() });
    renderStatus();

    expect(
      await screen.findByText("All configured services are available"),
    ).toBeVisible();
    const strip = document.querySelector<HTMLElement>(".vf-metric-strip");
    if (!strip) throw new Error("Expected the Status metric strip");
    expect(within(strip).getByText("Observed")).toBeVisible();
    expect(within(strip).getByText("Routable")).toBeVisible();
    expect(within(strip).getByText("Needs attention")).toBeVisible();
    expect(
      within(strip).getAllByText("1 service", { exact: true }),
    ).toHaveLength(2);
    expect(screen.getByText("Gateway API")).toBeVisible();
  });

  it("does not present unavailable or degraded observations as all clear", async () => {
    statusState.load = async () => ({
      current_health: health({
        state: "unavailable",
        tone: "neutral",
        label: "Live status unavailable",
        detail: "The gateway could not observe services.",
        observation_available: false,
        services: [
          {
            ...health().services[0],
            observation: "unavailable",
            tone: "neutral",
            routable: null,
          },
        ],
      }),
    });
    renderStatus();

    expect(await screen.findByText("Live status unavailable")).toBeVisible();
    expect(screen.getByText("Unavailable", { exact: true })).toBeVisible();
    expect(screen.getByText("live status unavailable")).toBeVisible();
  });

  it("keeps the error state honest and retryable", async () => {
    let attempts = 0;
    statusState.load = async () => {
      attempts += 1;
      if (attempts === 1) throw new Error("offline");
      return { current_health: health() };
    };
    renderStatus();

    expect(await screen.findByText("Could not load status")).toBeVisible();
    await screen.getByRole("button", { name: "Retry" }).click();
    expect(
      await screen.findByText("All configured services are available"),
    ).toBeVisible();
  });
});
