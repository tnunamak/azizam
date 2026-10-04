import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { axe } from "jest-axe";
import { AppShell } from "./app-shell";
import type { QueryClient } from "@tanstack/react-query";

const clients = vi.hoisted(() => [] as QueryClient[]);
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    createQueryClient: () => {
      const client = actual.createQueryClient();
      clients.push(client);
      return client;
    },
  };
});

const push = vi.fn();
let pathname = "/home";

vi.mock("next/navigation", () => ({
  usePathname: () => pathname,
  useRouter: () => ({ push }),
}));

const ownerSession = {
  authenticated: true,
  csrf_token: "csrf",
  user: {
    id: "owner",
    name: "Alex Owner",
    email: "owner@example.test",
    role: "owner",
  },
  apps: [{ id: "browser-test-app", name: "Browser test app" }],
};

function jsonResponse(body: unknown, init: ResponseInit = {}) {
  return Promise.resolve(
    new Response(JSON.stringify(body), {
      status: init.status ?? 200,
      headers: { "content-type": "application/json", ...init.headers },
    }),
  );
}

beforeEach(() => {
  clients.forEach((client) => client.clear());
  pathname = "/home";
  push.mockReset();
  localStorage.clear();
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
  vi.stubGlobal(
    "ResizeObserver",
    class ResizeObserver {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  HTMLCanvasElement.prototype.getContext = vi.fn(() => null);
  Element.prototype.scrollIntoView = vi.fn();
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/me/api/session")) return jsonResponse(ownerSession);
      if (url.endsWith("/.well-known/ai-gateway")) {
        return jsonResponse({
          service: { name: "Kitchen Gateway" },
          presentation: { theme_default: "light" },
          recipes: {
            openai: { canonical_base_url: "http://127.0.0.1:18180/openai/v1" },
          },
        });
      }
      if (url.endsWith("/health")) return jsonResponse({ status: "healthy" });
      if (url.includes("/admin/api/status")) {
        return jsonResponse({
          status: { current_health: { state: "healthy", reason: "Ready" } },
        });
      }
      if (url.endsWith("/admin/api/users")) return jsonResponse({ users: [] });
      if (url.endsWith("/admin/api/operator/service-registry")) {
        return jsonResponse({ services: {} });
      }
      return jsonResponse({});
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("AppShell accessible names", () => {
  it("keeps natural visible labels while exposing dynamic account and brand names", async () => {
    render(
      <AppShell>
        <h1>Home</h1>
      </AppShell>,
    );

    expect(
      await screen.findAllByRole("button", {
        name: "Account menu: Alex Owner",
      }),
    ).not.toHaveLength(0);
    expect(
      screen.getByRole("link", { name: "Vivid Fish Kitchen Gateway Home" }),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: /^Search$/ })).toBeVisible();
    expect(screen.queryByRole("button", { name: /⌘K/ })).toBeNull();
  });

  it("does not regress label-content-name-mismatch or serious wcag axe rules", async () => {
    const { container } = render(
      <AppShell>
        <h1>Home</h1>
      </AppShell>,
    );

    await screen.findAllByRole("button", { name: "Account menu: Alex Owner" });
    const wcagResult = await axe(container);
    const labelContentResult = await axe(container, {
      runOnly: {
        type: "rule",
        values: ["label-content-name-mismatch"],
      },
    });
    expect(
      wcagResult.violations.filter((violation) =>
        ["serious", "critical"].includes(violation.impact ?? ""),
      ),
    ).toEqual([]);
    expect(
      labelContentResult.violations.filter(
        (violation) => violation.id === "label-content-name-mismatch",
      ),
    ).toEqual([]);
  });
});

describe("Service admin session", () => {
  it("shows an honest state on person-only pages without rendering their API consumers", async () => {
    const originalFetch = fetch;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/me/api/session"))
          return jsonResponse({ authenticated: false });
        if (url.endsWith("/admin/api/session"))
          return jsonResponse({
            actor: {
              subject: "automation",
              roles: ["admin"],
              kind: "service_account",
              label: "CI admin",
              csrf_token: "",
            },
          });
        return originalFetch(input, init);
      }),
    );
    render(
      <AppShell>
        <h1>Private library consumer</h1>
      </AppShell>,
    );
    expect(
      await screen.findByRole("heading", { name: "Service admin session" }),
    ).toBeVisible();
    expect(screen.queryByText("Private library consumer")).toBeNull();
    expect(
      screen.getByRole("link", { name: "Sign in as a person" }),
    ).toHaveAttribute("href", "/ui/sign-in/");
    expect(screen.queryByRole("link", { name: "People" })).toBeNull();
  });
});
