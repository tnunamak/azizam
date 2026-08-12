import type { ComponentPropsWithoutRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { axe } from "jest-axe";
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
});

describe("admin Status", () => {
  it("renders a page-level heading that passes axe while observations are loading", async () => {
    statusState.load = () => new Promise(() => undefined);
    renderStatus();

    expect(screen.getByText("Checking current gateway health.")).toBeVisible();
    expect(
      screen.getByRole("heading", { name: "Status", level: 1 }),
    ).toBeVisible();
    expect(document.querySelectorAll("h1")).toHaveLength(1);
    await expectPageHasHeadingOne();
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

  it("keeps an error page with an axe-valid heading honest and retryable", async () => {
    let attempts = 0;
    statusState.load = async () => {
      attempts += 1;
      if (attempts === 1) throw new Error("offline");
      return { current_health: health() };
    };
    renderStatus();

    expect(await screen.findByText("Could not load status")).toBeVisible();
    expect(
      screen.getByRole("heading", { name: "Status", level: 1 }),
    ).toBeVisible();
    expect(document.querySelectorAll("h1")).toHaveLength(1);
    await expectPageHasHeadingOne();
    await screen.getByRole("button", { name: "Retry" }).click();
    expect(
      await screen.findByText("All configured services are available"),
    ).toBeVisible();
  });
});
