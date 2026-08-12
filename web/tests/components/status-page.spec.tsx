import type { ComponentPropsWithoutRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { axe } from "jest-axe";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const statusState = vi.hoisted(() => ({
  load: async (): Promise<any> => ({ current_health: null }),
  search: "",
  push: vi.fn(),
  replace: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(statusState.search),
  useRouter: () => ({ push: statusState.push, replace: statusState.replace }),
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

async function expectPageHasHeadingOne() {
  const accessibility = await axe(document.body);
  expect(
    accessibility.violations.find(
      (violation) => violation.id === "page-has-heading-one",
    ),
  ).toBeUndefined();
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  statusState.search = "";
  statusState.push.mockReset();
  statusState.replace.mockReset();
});

describe("admin Status", () => {
  it("renders a page-level heading that passes axe while observations are loading", async () => {
    statusState.load = () => new Promise(() => undefined);
    renderStatus();

    expect(screen.getByText("Checking current gateway health.")).toBeVisible();
    expect(
      screen.getByRole("heading", { name: "System Status", level: 1 }),
    ).toBeVisible();
    expect(document.querySelectorAll("h1")).toHaveLength(1);
    await expectPageHasHeadingOne();
  });

  it("uses the shared health projection as the first-viewport state", async () => {
    statusState.load = async () => ({ current_health: health() });
    renderStatus();

    expect(
      await screen.findByText("All configured services are available"),
    ).toBeVisible();
    expect(screen.getByText("Observed")).toBeVisible();
    expect(screen.getByText("Needs attention")).toBeVisible();
    expect(screen.getByText("No current advisories")).toBeVisible();
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

  it("keeps an error page with an axe-valid heading honest and retryable", async () => {
    let attempts = 0;
    statusState.load = async () => {
      attempts += 1;
      if (attempts === 1) throw new Error("offline");
      return { current_health: health() };
    };
    renderStatus();

    expect(await screen.findByText("Status unavailable")).toBeVisible();
    expect(
      screen.getByRole("heading", { name: "System Status", level: 1 }),
    ).toBeVisible();
    expect(document.querySelectorAll("h1")).toHaveLength(1);
    await expectPageHasHeadingOne();
    await screen.getByRole("button", { name: "Retry" }).click();
    expect(
      await screen.findByText("All configured services are available"),
    ).toBeVisible();
  });

  it("keeps no-history treatment scalar and does not render a decorative chart", async () => {
    statusState.load = async () => ({
      current_health: health(),
      backends: [
        {
          service_id: "gateway",
          latency_ms: 123,
          history: { latency_series: [[1, 120]], status_series: [[1, 1]] },
        },
      ],
    });
    statusState.search = "service=gateway";
    renderStatus();

    expect(
      (await screen.findAllByText("Latency: 123 ms")).length,
    ).toBeGreaterThan(0);
    expect(screen.getAllByText("No history available").length).toBeGreaterThan(
      0,
    );
    expect(document.querySelector(".vf-chart-sparkline")).toBeNull();
  });

  it("uses the stable service id for diagnostic and configuration navigation", async () => {
    statusState.load = async () => ({ current_health: health() });
    statusState.search = "service=gateway";
    renderStatus();

    const configurations = await screen.findAllByRole("link", {
      name: "Open Services configuration",
    });
    expect(configurations).not.toHaveLength(0);
    for (const configuration of configurations) {
      expect(configuration).toHaveAttribute(
        "href",
        "/admin/services?service=gateway",
      );
    }
    expect(
      screen.getByRole("button", { name: /Gateway API/i }),
    ).toHaveAttribute("aria-pressed", "true");
  });

  it("does not emit a Services configuration target for an unregistered service", async () => {
    statusState.load = async () => ({
      current_health: health({
        services: [
          {
            ...health().services[0],
            configured: false,
            tone: "warn",
            observation: "degraded",
          },
        ],
      }),
    });
    statusState.search = "service=gateway";
    renderStatus();

    expect(await screen.findByText("Unregistered service")).toBeVisible();
    expect(
      screen.queryByRole("link", { name: "Open Services configuration" }),
    ).toBeNull();
    expect(screen.getByRole("link", { name: "Inspect" })).toHaveAttribute(
      "href",
      "/admin/status?service=gateway",
    );
    expect(screen.queryByRole("link", { name: "Services" })).toBeNull();
  });

  it("selects a service with its stable id rather than its display name", async () => {
    statusState.load = async () => ({ current_health: health() });
    renderStatus();

    const service = await screen.findByRole("button", { name: /Gateway API/i });
    service.click();
    expect(statusState.push).toHaveBeenCalledWith(
      "/admin/status?service=gateway",
    );
  });

  it("retains known facts and exposes Retry after a refresh failure", async () => {
    statusState.load = async () => ({ current_health: health() });
    renderStatus();
    await screen.findByText("All configured services are available");
    statusState.load = async () => {
      throw new Error("offline");
    };

    screen.getByRole("button", { name: "Refresh" }).click();
    expect(await screen.findByText("Showing last known status")).toBeVisible();
    expect(screen.getByRole("button", { name: "Retry" })).toBeVisible();
    expect(screen.getByText("Gateway API")).toBeVisible();
  });

  it("renders chart semantics only for three real samples", async () => {
    statusState.load = async () => ({
      current_health: health(),
      backends: [
        {
          service_id: "gateway",
          history: {
            latency_series: [
              [1, 100],
              [2, 110],
              [3, 120],
            ],
            status_series: [
              [1, 1],
              [2, 0.5],
              [3, 1],
            ],
          },
        },
      ],
    });
    statusState.search = "service=gateway";
    renderStatus();

    const latencyTables = await screen.findAllByRole("table", {
      name: "Latency observations",
    });
    expect(latencyTables).not.toHaveLength(0);
    expect(latencyTables[0]).toHaveTextContent("100");
    expect(latencyTables[0]).toHaveTextContent("120");
    const availabilityTables = screen.getAllByRole("table", {
      name: "Availability observations",
    });
    expect(availabilityTables).not.toHaveLength(0);
    expect(availabilityTables[0]).toHaveTextContent("0.5");
  });

  it("counts host advisories as Needs attention", async () => {
    statusState.load = async () => ({
      current_health: health({
        signals: [
          {
            source: "Host signal needs attention",
            tone: "warn",
            reasons: ["Disk space is low."],
          },
        ],
      }),
    });
    renderStatus();

    expect(
      await screen.findByText("Host signal needs attention"),
    ).toBeVisible();
    expect(screen.getByText("Needs attention").parentElement).toHaveTextContent(
      "1",
    );
    expect(screen.getByText("1 current advisory")).toBeVisible();
  });

  it("shows an old generated timestamp without claiming it is current", async () => {
    const twoYearsAgo = Math.floor(Date.now() / 1000) - 2 * 365 * 24 * 60 * 60;
    statusState.load = async () => ({
      current_health: health(),
      generated_at: twoYearsAgo,
    });
    renderStatus();

    expect(await screen.findByText("Timestamp supplied")).toBeVisible();
    expect(screen.getByText("freshness is not supplied")).toBeVisible();
    expect(screen.getByText("2y ago")).toBeVisible();
    expect(screen.queryByText("Current", { exact: true })).toBeNull();
  });
});
