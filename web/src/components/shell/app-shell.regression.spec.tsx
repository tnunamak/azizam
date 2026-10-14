import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { axe } from "jest-axe";
import userEvent from "@testing-library/user-event";
import { AppShell } from "./app-shell";
import type { QueryClient } from "@tanstack/react-query";
import { resetSessionCache } from "@/lib/api";
import { ErrorState } from "@/components/product/error-state";
import { CopyButton } from "@/components/ui/copy-button";
import { CodeBlock } from "@/components/product/code-block";
import { Progress } from "@/components/ui/progress";
import { healthHistory, healthStory, hourSlots, UptimeBar } from "@/components/product/uptime-bar";

const clients = vi.hoisted(() => [] as QueryClient[]);
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    createQueryClient: () => {
      const client = actual.createQueryClient();
      // Production retries a 5xx twice with backoff, which no test waits for.
      // Set once here, keeping the other defaults, so no test depends on which
      // earlier test switched retries off.
      const defaults = client.getDefaultOptions();
      client.setDefaultOptions({ ...defaults, queries: { ...defaults.queries, retry: false } });
      clients.push(client);
      return client;
    },
  };
});

const push = vi.fn();
const replace = vi.fn();
let pathname = "/";
let search = "";

// The owner checks wait SHELL_CHECKS_DELAY_MS after mount (use-after-first-load.ts); these
// specs are about what the checks say, so they start at once. app-shell.deferred-checks.spec.tsx
// covers the delay itself.
vi.mock("./use-after-first-load", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./use-after-first-load")>()),
  useAfterFirstLoad: () => true,
}));

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
  pathname = "/";
  search = "";
  window.history.replaceState(null, "", "/");
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
        return jsonResponse({ service_registry: { schema_version: 1, services: {} } });
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
    expect(await screen.findByText("We couldn't confirm you were signed out. Refresh this page and try again.")).toBeVisible();
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

describe("AppShell navigation tooltips", () => {
  function mockSidebarViewport(wide: boolean) {
    const original = vi.mocked(window.matchMedia).getMockImplementation()!;
    const listeners = new Set<() => void>();
    const sidebar = {
      ...original("(min-width: 64rem)"),
      matches: wide,
      addEventListener: vi.fn((_event: string, listener: () => void) => {
        listeners.add(listener);
      }),
      removeEventListener: vi.fn((_event: string, listener: () => void) => {
        listeners.delete(listener);
      }),
    };
    vi.mocked(window.matchMedia).mockImplementation((query) =>
      query === sidebar.media ? sidebar : original(query),
    );
    return (nextWide: boolean) => {
      sidebar.matches = nextWide;
      act(() => listeners.forEach((listener) => listener()));
    };
  }

  it("does not repeat visible desktop navigation labels on hover or focus", async () => {
    mockSidebarViewport(true);
    render(<AppShell><h1>Home</h1></AppShell>);
    const nav = await screen.findByRole("navigation", { name: "Main navigation" });
    for (const link of within(nav).getAllByRole("link")) {
      expect(link).not.toHaveAttribute("title");
      expect(link).not.toHaveAttribute("data-slot", "tooltip-trigger");
    }
    const user = userEvent.setup();
    await user.hover(within(nav).getByRole("link", { name: "Usage" }));
    act(() => within(nav).getByRole("link", { name: "Workflows" }).focus());
    expect(screen.queryByRole("tooltip")).toBeNull();
    expect(within(nav).getByRole("link", { name: "Home" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("button", { name: "Go to" }).querySelector("kbd")).toHaveTextContent(/Ctrl K|⌘K/);
  });

  it("keeps collapsed-rail tooltips and accessible names for hover and keyboard focus", async () => {
    mockSidebarViewport(false);
    render(<AppShell><h1>Home</h1></AppShell>);
    const nav = await screen.findByRole("navigation", { name: "Main navigation" });
    const usage = within(nav).getByRole("link", { name: "Usage" });
    expect(usage).toHaveAttribute("data-slot", "tooltip-trigger");
    const user = userEvent.setup();
    await user.hover(usage);
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Usage");
    await user.unhover(usage);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("tooltip")).toBeNull());
    act(() => within(nav).getByRole("link", { name: "Workflows" }).focus());
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Workflows");
  });

  it.each(["Expand navigation", "More"])("omits redundant tooltips in the labeled %s sheet", async (name) => {
    mockSidebarViewport(false);
    render(<AppShell><h1>Home</h1></AppShell>);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name }));
    const dialog = await screen.findByRole("dialog");
    const nav = within(dialog).getByRole("navigation", { name: "Main navigation" });
    for (const link of within(nav).getAllByRole("link")) {
      expect(link).not.toHaveAttribute("title");
      expect(link).not.toHaveAttribute("data-slot", "tooltip-trigger");
    }
    await user.hover(within(nav).getByRole("link", { name: "Usage" }));
    act(() => within(nav).getByRole("link", { name: "Workflows" }).focus());
    expect(screen.queryByRole("tooltip")).toBeNull();
    await user.click(within(nav).getByRole("link", { name: "Home" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("updates tooltip availability when the sidebar changes width", async () => {
    const resize = mockSidebarViewport(false);
    render(<AppShell><h1>Home</h1></AppShell>);
    const nav = await screen.findByRole("navigation", { name: "Main navigation" });
    const usage = () => within(nav).getByRole("link", { name: "Usage" });
    act(() => usage().focus());
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Usage");
    resize(true);
    expect(usage()).not.toHaveAttribute("data-slot", "tooltip-trigger");
    expect(usage()).not.toHaveAttribute("aria-describedby");
    expect(screen.queryByRole("tooltip")).toBeNull();
    resize(false);
    expect(usage()).toHaveAttribute("data-slot", "tooltip-trigger");
    act(() => usage().focus());
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Usage");
  });
});

describe("Member session", () => {
  it("offers Status in the navigation and in Go to, and none of the owner pages", async () => {
    const originalFetch = fetch;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
      String(input).endsWith("/me/api/session")
        ? jsonResponse({
            authenticated: true,
            csrf_token: "member",
            user: { id: "member", name: "Test member", email: "member@example.test", role: "member" },
            apps: [],
          })
        : originalFetch(input, init),
    ));
    render(<AppShell><h1>Home</h1></AppShell>);
    const user = userEvent.setup();
    await screen.findAllByRole("button", { name: "Account menu: Test member" });
    expect(screen.getAllByRole("link", { name: "Status" }).length).toBeGreaterThan(0);
    for (const name of ["Engines", "Providers", "People", "Activity", "Settings"]) {
      expect(screen.queryByRole("link", { name })).toBeNull();
    }
    await user.click(screen.getByRole("button", { name: "Go to" }));
    expect(await screen.findByRole("option", { name: "Status" })).toBeVisible();
    for (const name of ["Engines", "Providers", "People", "Activity", "Settings"]) {
      expect(screen.queryByRole("option", { name })).toBeNull();
    }
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
      await screen.findByRole("heading", { name: "Admin key" }),
    ).toBeVisible();
    expect(screen.queryByText("Private library consumer")).toBeNull();
    expect(
      screen.getByRole("link", { name: "Sign in as a person" }),
    ).toHaveAttribute("href", "/sign-in/?return_to=%2F");
    expect(screen.queryByRole("link", { name: "People" })).toBeNull();
  });
});

describe("Signed-out private pages", () => {
  it("redirects without showing navigation or private content and keeps the current page", async () => {
    pathname = "/models";
    search = "capability=chat";
    window.history.replaceState(null, "", "/models/?capability=chat");
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
        "/sign-in/?return_to=%2Fmodels%2F%3Fcapability%3Dchat",
      ),
    );
    expect(screen.queryByText("Models consumer")).toBeNull();
    expect(screen.queryByRole("navigation")).toBeNull();
    expect(screen.queryByRole("button", { name: /Account menu/ })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("keeps the shell and private consumers unmounted until the session is known", async () => {
    pathname = "/library";
    window.history.replaceState(null, "", "/library/");
    vi.mocked(fetch).mockImplementation(() => new Promise(() => {}));
    render(
      <AppShell>
        <h1>Private consumer</h1>
      </AppShell>,
    );
    const loading = screen.getByRole("status", {
      name: "Loading your account",
    });
    expect(loading).toHaveTextContent("Loading your account");
    expect(loading).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByRole("navigation")).toBeNull();
    expect(screen.queryByText("Private consumer")).toBeNull();
    expect(screen.queryByRole("button", { name: /Account menu/ })).toBeNull();
    expect(replace).not.toHaveBeenCalled();
  });

  it("keeps a failed session check distinct from signed out and allows retry", async () => {
    pathname = "/library";
    window.history.replaceState(null, "", "/library/");
    vi.mocked(fetch).mockRejectedValue(new Error("Offline"));
    render(
      <AppShell>
        <h1>Private consumer</h1>
      </AppShell>,
    );
    expect(await screen.findByText("Couldn’t load your account")).toBeVisible();
    expect(
      screen.getByText("We couldn't tell why your account didn't load. Try again."),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Try again" })).toBeVisible();
    expect(screen.queryByRole("navigation")).toBeNull();
    expect(screen.queryByText("Private consumer")).toBeNull();
    expect(replace).not.toHaveBeenCalled();
  });
});

describe("WP-20 shell recovery", () => {
  it("keeps sign-out pending and rejects repeat selections", async () => {
    const originalFetch = fetch;
    const logout = vi.fn(() => new Promise<Response>(() => {}));
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
      String(input) === "/logout" ? logout() : originalFetch(input, init),
    ));
    render(<AppShell><h1>Home</h1></AppShell>);
    const user = userEvent.setup();
    await user.click((await screen.findAllByRole("button", { name: "Account menu: Alex Owner" }))[0]);
    await user.click(await screen.findByRole("menuitem", { name: "Sign out" }));
    const pending = await screen.findByRole("menuitem", { name: "Signing out…" });
    expect(pending).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(pending);
    expect(logout).toHaveBeenCalledTimes(1);
  });

  it("keeps Help in the common menu without developer links", async () => {
    render(<AppShell><h1>Home</h1></AppShell>);
    const user = userEvent.setup();
    await user.click((await screen.findAllByRole("button", { name: "Account menu: Alex Owner" }))[0]);
    expect(await screen.findByRole("menuitem", { name: "Help" })).toHaveAttribute("href", "/docs");
    expect(screen.queryByRole("menuitem", { name: "Design system" })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "API reference" })).toBeNull();
  });

  it("shows apps loading, failure and retry in Go to, then hides an empty group", async () => {
    const originalFetch = fetch;
    let commandRequest = false;
    let retry = false;
    let release!: () => void;
    const pending = new Promise<void>((resolve) => { release = resolve; });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (commandRequest && String(input).endsWith("/me/api/session")) {
        if (retry) return jsonResponse({ apps: [] });
        await pending;
        return jsonResponse({}, { status: 503 });
      }
      return originalFetch(input, init);
    }));
    render(<AppShell><h1>Home</h1></AppShell>);
    await screen.findAllByRole("button", { name: "Account menu: Alex Owner" });
    commandRequest = true;
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Go to" }));
    expect(await screen.findByText("Loading apps…")).toBeVisible();
    await user.type(screen.getByRole("combobox"), "no such app");
    expect(screen.getByText("Loading apps…")).toBeVisible();
    await act(async () => release());
    expect(await screen.findByText(/Apps couldn't be loaded/)).toBeVisible();
    retry = true;
    await user.click(screen.getByText(/Apps couldn't be loaded/).querySelector("button")!);
    expect(await screen.findByText("No matches. Try a page or app name.")).toBeVisible();
    expect(screen.queryByText(/Apps couldn't be loaded/)).toBeNull();
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Go to" }));
    expect(await screen.findByRole("combobox")).toHaveValue("");
  });

  it("does not call a responding unhealthy gateway unreachable", async () => {
    const originalFetch = fetch;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
      String(input).endsWith("/health")
        ? jsonResponse({ status: "degraded" })
        : originalFetch(input, init),
    ));
    render(<AppShell><h1>Home</h1></AppShell>);
    await screen.findAllByRole("button", { name: "Account menu: Alex Owner" });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Go to" }));
    await user.click(await screen.findByRole("option", { name: "Check gateway reachability" }));
    expect(await screen.findByText("The gateway responded, but reported a problem. View Status.")).toBeVisible();
    expect(screen.queryByText("The gateway could not be reached. Try again.")).toBeNull();
  });
});

describe("WP-20 shared widgets", () => {
  it("does not invent a failing layer when no context is known", () => {
    render(<ErrorState />);
    expect(screen.getByText("We couldn't tell where this stopped. Reload this page.")).toBeVisible();
  });

  it("keeps missing progress indeterminate and honors a custom maximum", () => {
    const { container, rerender } = render(<Progress aria-label="Loading" />);
    expect(screen.getByRole("progressbar")).not.toHaveAttribute("aria-valuenow");
    expect(container.querySelector('[data-slot="progress-indicator"]')).not.toHaveStyle({ transform: "translateX(-100%)" });
    rerender(<Progress aria-label="Loading" value={50} max={200} />);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "50");
    expect(container.querySelector('[data-slot="progress-indicator"]')).toHaveStyle({ transform: "translateX(-75%)" });
  });

  it("shows clipboard failure visibly and resets it for new content", async () => {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) } });
    Object.defineProperty(document, "execCommand", { configurable: true, value: vi.fn(() => false) });
    const { rerender } = render(<CopyButton value="first" />);
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't copy. Select the text and copy it yourself.");
    expect(screen.getByRole("alert")).not.toHaveClass("sr-only");
    rerender(<CopyButton value="second" />);
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  });

  it("clears copied code when the snippet changes", async () => {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: vi.fn().mockResolvedValue(undefined) } });
    const { rerender } = render(<CodeBlock code="first" language="Text" />);
    fireEvent.click(screen.getByRole("button", { name: "Copy Text code" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Code copied"));
    rerender(<CodeBlock code="second" language="Text" />);
    await waitFor(() => expect(screen.getByRole("status")).toBeEmptyDOMElement());
  });

  it("does not apply an old clipboard completion to new content", async () => {
    let finish!: () => void;
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: () => new Promise<void>((resolve) => { finish = resolve; }) } });
    const { rerender } = render(<CopyButton value="first" />);
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    rerender(<CopyButton value="second" />);
    await act(async () => finish());
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("uses the latest health state and never infers recovery across a gap", () => {
    const degraded = [{ start: 0, value: 0 }, { start: 3600, value: 0.5 }];
    expect(healthStory(healthHistory(degraded), degraded).current).toMatch(/^Degraded since/);
    const missing = [...degraded, { start: 7200, value: null }];
    expect(healthHistory(missing).recoveredAt).toBeNull();
    expect(healthStory(healthHistory(missing), missing).current).toBe("No recent check");
    const afterGap = [...missing, { start: 10800, value: 1 }];
    expect(healthHistory(afterGap).recoveredAt).toBeNull();
    const recovered = [{ start: 0, value: 0 }, { start: 3600, value: 1 }];
    expect(healthHistory(recovered).recoveredAt).toBe(3600);
  });

  it("keeps empty and invalid health samples as gaps", () => {
    expect(hourSlots([[1, Number.NaN]], 3600).every((slot) => slot.value === null)).toBe(true);
    render(<UptimeBar series={[]} end={86400} label="Engine" />);
    expect(screen.getByRole("group")).toHaveAttribute("aria-label", "Engine: no checks recorded in the last 24 hours");
  });
});

it("keeps the status popover as unknown when owner health fails", async () => {
  const originalFetch = fetch;
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).includes("/admin/api/status")) return jsonResponse({}, { status: 503 });
    if (String(input).endsWith("/me/api/home")) return jsonResponse({ authenticated: true, capabilities: [], aliases: [], receipts: [] });
    return originalFetch(input, init);
  }));
  render(<AppShell><h1>Home</h1></AppShell>);
  const buttons = await screen.findAllByRole("button", { name: "Gateway status: Status unknown" });
  await userEvent.setup().click(buttons[0]);
  expect(screen.queryByText("Your AI")).toBeNull();
});

describe("Reduced motion", () => {
  it("cancels animations, transitions and CSS scrolling for elements and pseudo-elements", async () => {
    const { readFileSync } = await import("node:fs");
    const css = readFileSync("src/app/globals.css", "utf8");
    const rule = css.match(/@media \(prefers-reduced-motion: reduce\)\s*\{([\s\S]*?)\n\}/)?.[1];
    expect(rule).toBeDefined();
    expect(rule).toMatch(/\*,\s*\*::before,\s*\*::after\s*\{/);
    expect(rule).toContain("animation: none !important");
    expect(rule).toContain("transition: none !important");
    expect(rule).toContain("scroll-behavior: auto !important");
  });

  it.each([false, true])("honors the current motion preference during Status navigation (reduce=%s)", async (reduce) => {
    const original = vi.mocked(window.matchMedia).getMockImplementation()!;
    let reduced = reduce;
    vi.mocked(window.matchMedia).mockImplementation((query) => ({
      ...original(query),
      matches: query === "(prefers-reduced-motion: reduce)" ? reduced : false,
    }));
    pathname = "/status";
    window.history.replaceState(null, "", "/status/#engines");
    const { rerender, unmount } = render(<AppShell><h1>Status</h1></AppShell>);
    await screen.findAllByRole("button", { name: "Account menu: Alex Owner" });
    // A heading loaded after the route mounts must still be focused and scrolled.
    rerender(<AppShell><h1>Status</h1><h2>Engines and providers</h2></AppShell>);
    const heading = await screen.findByRole("heading", { name: "Engines and providers" });
    await waitFor(() => expect(heading).toHaveFocus());
    expect(Element.prototype.scrollIntoView).toHaveBeenLastCalledWith({
      block: "start", behavior: reduce ? "instant" : "auto",
    });
    const focus = vi.spyOn(heading, "focus");
    reduced = !reduce;
    vi.mocked(Element.prototype.scrollIntoView).mockClear();
    window.history.replaceState(null, "", "/status/#providers");
    act(() => window.dispatchEvent(new HashChangeEvent("hashchange")));
    expect(focus).toHaveBeenCalledWith({ preventScroll: true });
    expect(Element.prototype.scrollIntoView).toHaveBeenLastCalledWith({
      block: "start", behavior: reduced ? "instant" : "auto",
    });
    unmount();
    vi.mocked(Element.prototype.scrollIntoView).mockClear();
    act(() => window.dispatchEvent(new HashChangeEvent("hashchange")));
    expect(Element.prototype.scrollIntoView).not.toHaveBeenCalled();
    focus.mockRestore();
  });
});
