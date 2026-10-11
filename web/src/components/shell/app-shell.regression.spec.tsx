import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { axe } from "jest-axe";
import userEvent from "@testing-library/user-event";
import { AppShell } from "./app-shell";
import type { QueryClient } from "@tanstack/react-query";
import { resetSessionCache } from "@/lib/api";

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
const replace = vi.fn();
let pathname = "/home";
let search = "";

vi.mock("next/navigation", () => ({
  usePathname: () => pathname,
  useSearchParams: () => new URLSearchParams(search),
  useRouter: () => ({ push, replace }),
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
  resetSessionCache();
  pathname = "/home";
  search = "";
  window.history.replaceState(null, "", "/ui/home/");
  push.mockReset();
  replace.mockReset();
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
      if (url.endsWith("/.well-known/azizam")) {
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
  it("labels missing capability evidence as unknown rather than an issue", async () => {
    const originalFetch = fetch;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).endsWith("/admin/api/operator/service-registry")) {
          return jsonResponse({
            service_registry: { schema_version: 1, services: {} },
          });
        }
        if (String(input).endsWith("/me/api/home")) {
          return jsonResponse({
            authenticated: true,
            capabilities: [
              {
                capability: "chat",
                icon_key: "chat",
                capability_title: "Chat",
                status: "unknown",
                available: false,
                reason_code: "unknown",
                availability: {
                  evidence: { source: "health", kind: "no_response" },
                },
              },
            ],
            aliases: [],
            receipts: [],
          });
        }
        return originalFetch(input, init);
      }),
    );
    render(
      <AppShell>
        <h1>Home</h1>
      </AppShell>,
    );
    const buttons = await screen.findAllByRole("button", {
      name: "Gateway status: Not checked yet",
    });
    expect(buttons.length).toBeGreaterThan(0);
    expect(
      screen.queryByRole("button", { name: "Gateway status: 1 issue" }),
    ).toBeNull();
  });

  it("signs out through a POST with the current session CSRF token", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith("/me/api/session")) return jsonResponse(ownerSession);
      if (url.endsWith("/logout")) return jsonResponse({}, { status: 403 });
      return jsonResponse({});
    });
    render(
      <AppShell>
        <h1>Home</h1>
      </AppShell>,
    );
    const user = userEvent.setup();
    const menus = await screen.findAllByRole("button", {
      name: "Account menu: Alex Owner",
    });
    await user.click(menus[0]);
    await user.click(await screen.findByRole("menuitem", { name: "Sign out" }));
    expect(fetchMock).toHaveBeenCalledWith(
      "/logout",
      expect.objectContaining({
        method: "POST",
        credentials: "same-origin",
        headers: { "x-azizam-csrf": "csrf", accept: "application/json" },
      }),
    );
  });

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
    const brandLinks = screen.getAllByRole("link", {
      name: "Azizam Kitchen Gateway Home",
    });
    expect(brandLinks).toHaveLength(2);
    for (const link of brandLinks) {
      expect(link).toBeVisible();
      expect(link).toHaveTextContent("Azizam");
      expect(link).toHaveTextContent("Kitchen Gateway");
    }
    expect(screen.getByRole("button", { name: /^Go to$/ })).toBeVisible();
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
    ).toHaveAttribute("href", "/ui/sign-in/?return_to=%2Fui%2Fhome%2F");
    expect(screen.queryByRole("link", { name: "People" })).toBeNull();
  });
});

describe("Signed-out private pages", () => {
  it("redirects without showing navigation or private content and keeps the current page", async () => {
    pathname = "/models";
    search = "capability=chat";
    window.history.replaceState(null, "", "/ui/models/?capability=chat");
    const originalFetch = fetch;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/me/api/session"))
          return jsonResponse({ authenticated: false });
        if (url.endsWith("/admin/api/session"))
          return jsonResponse(
            {
              error: {
                code: "authentication_required",
                message: "Authentication required",
              },
            },
            { status: 401 },
          );
        return originalFetch(input, init);
      }),
    );

    render(
      <AppShell>
        <h1>Models consumer</h1>
      </AppShell>,
    );

    await waitFor(() =>
      expect(replace).toHaveBeenCalledWith(
        "/sign-in/?return_to=%2Fui%2Fmodels%2F%3Fcapability%3Dchat",
      ),
    );
    expect(screen.queryByText("Models consumer")).toBeNull();
    expect(screen.queryByRole("navigation")).toBeNull();
    expect(screen.queryByRole("button", { name: /Account menu/ })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("keeps the shell and private consumers unmounted until the session is known", async () => {
    vi.mocked(fetch).mockImplementation(() => new Promise(() => {}));
    render(
      <AppShell>
        <h1>Private consumer</h1>
      </AppShell>,
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "Checking your session",
    );
    expect(screen.queryByRole("navigation")).toBeNull();
    expect(screen.queryByText("Private consumer")).toBeNull();
    expect(screen.queryByRole("button", { name: /Account menu/ })).toBeNull();
    expect(replace).not.toHaveBeenCalled();
  });

  it("keeps a failed session check distinct from signed out and allows retry", async () => {
    vi.mocked(fetch).mockRejectedValue(new Error("Offline"));
    clients.forEach((client) =>
      client.setDefaultOptions({ queries: { retry: false } }),
    );
    render(
      <AppShell>
        <h1>Private consumer</h1>
      </AppShell>,
    );
    expect(await screen.findByText("Couldn’t load your account")).toBeVisible();
    expect(screen.getByRole("button", { name: "Try again" })).toBeVisible();
    expect(screen.queryByRole("navigation")).toBeNull();
    expect(screen.queryByText("Private consumer")).toBeNull();
    expect(replace).not.toHaveBeenCalled();
  });
});
