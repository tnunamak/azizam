import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AppShell } from "./app-shell";
import { resetSessionCache } from "@/lib/api";

// The shell health pill must say what its dot says (DESIGN.md, Status model
// rule 3), and the owner alert banner must name only a proven engine outage.
// Moved down from the health-pill, health-explanation and owner-alert browser
// specs; the browser keeps the real Status navigation and focus hand-off.

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

// The owner checks wait SHELL_CHECKS_DELAY_MS after mount (use-after-first-load.ts); these
// specs are about what the checks say, so they start at once. app-shell.deferred-checks.spec.tsx
// covers the delay itself.
vi.mock("./use-after-first-load", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./use-after-first-load")>()),
  useAfterFirstLoad: () => true,
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/models",
  useSearchParams: () => new URLSearchParams(""),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

const member = {
  id: "member-pill",
  name: "Member",
  role: "member",
  email: "member@example.test",
};
const owner = { ...member, id: "owner", name: "Alex Owner", role: "owner" };

function json(body: unknown, status = 200) {
  return Promise.resolve(
    new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    }),
  );
}

type Row = ReturnType<typeof capability>;
function capability(
  key: string,
  title: string,
  state: "ready" | "unknown" | "down",
) {
  return {
    capability: key,
    capability_title: title,
    icon_key: key,
    intent: `${key}.run`,
    model: "default",
    available: state === "ready",
    status:
      state === "down" ? "unavailable" : state === "unknown" ? "unknown" : "ready",
    reason_code: state === "down" ? "unavailable" : state,
    ...(state === "ready"
      ? {
          availability: {
            evidence: {
              source: "health_check",
              observed_at_s: Math.floor(Date.now() / 1000),
            },
          },
        }
      : {}),
  };
}

interface World {
  user: typeof member;
  rows: Row[];
  health?: () => Promise<Response>;
  status?: unknown;
  providers?: unknown;
}
const requested: string[] = [];

function install(world: World) {
  requested.length = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      requested.push(new URL(url, "http://x.test").pathname);
      if (url.endsWith("/me/api/session"))
        return json({
          authenticated: true,
          csrf_token: "fixture",
          apps: [],
          user: world.user,
        });
      if (url.endsWith("/me/api/home"))
        return json({
          authenticated: true,
          user: world.user,
          aliases: [],
          receipts: [],
          capabilities: world.rows,
        });
      if (url.endsWith("/health"))
        return world.health ? world.health() : json({ status: "healthy" });
      if (url.includes("/admin/api/status")) return json({ status: world.status ?? {} });
      if (url.includes("/admin/api/providers"))
        return json(world.providers ?? { providers: [] });
      if (url.endsWith("/admin/api/operator/service-registry"))
        return json({ service_registry: { schema_version: 1, services: {} } });
      return json({});
    }),
  );
}

beforeEach(() => {
  clients.forEach((client) => client.clear());
  resetSessionCache();
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
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  HTMLCanvasElement.prototype.getContext = vi.fn(() => null);
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function openPill(name: string | RegExp) {
  render(
    <AppShell>
      <h1>Models</h1>
    </AppShell>,
  );
  const pill = (await screen.findAllByRole("button", { name }))[0];
  await userEvent.setup().click(pill);
  return { pill, popover: await screen.findByLabelText("Gateway health") };
}

const dotOf = (pill: HTMLElement) => pill.querySelector("span[aria-hidden]")!;

describe("shell health pill", () => {
  it("reads one Unknown capability as 'Not checked yet' with the grey ring dot", async () => {
    install({
      user: member,
      rows: [capability("chat", "Chat", "ready"), capability("image", "Images", "unknown")],
    });
    render(
      <AppShell>
        <h1>Models</h1>
      </AppShell>,
    );
    const pills = await screen.findAllByRole("button", {
      name: "Gateway status: Not checked yet",
    });
    const pill = pills[0];
    expect(pill).toHaveTextContent("Not checked yet");
    expect(screen.queryByRole("button", { name: /\bissues?$/ })).toBeNull();
    expect(dotOf(pill).className).toMatch(/border-neutral/);
    expect(dotOf(pill).className).not.toMatch(
      /bg-danger-fill|bg-warning-fill|bg-success-fill/,
    );
  });

  it("reads one Down capability as '1 issue' with the red dot", async () => {
    install({
      user: member,
      rows: [capability("chat", "Chat", "ready"), capability("image", "Images", "down")],
    });
    render(
      <AppShell>
        <h1>Models</h1>
      </AppShell>,
    );
    const pill = (
      await screen.findAllByRole("button", { name: "Gateway status: 1 issue" })
    )[0];
    expect(dotOf(pill).className).toMatch(/bg-danger-fill/);
  });

  it("lists the capability behind the count and links to Status", async () => {
    install({
      user: member,
      rows: [capability("chat", "Chat", "ready"), capability("image", "Images", "unknown")],
    });
    const { popover } = await openPill("Gateway status: Not checked yet");
    expect(
      within(popover)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["Images · Unknown"]);
    expect(within(popover).getByRole("link", { name: "See why" })).toHaveAttribute(
      "href",
      "/status",
    );
  });

  it("explains member health from their allowed models without owner requests", async () => {
    install({
      user: member,
      rows: [capability("chat", "Chat", "ready"), capability("image", "Images", "down")],
    });
    const { popover } = await openPill("Gateway status: 1 issue");
    expect(popover).toHaveTextContent("Your AIImages down");
    expect(popover).toHaveTextContent("GatewayReachable");
    expect(within(popover).getByRole("link", { name: "See why" })).toHaveAttribute(
      "href",
      "/status",
    );
    expect(requested.filter((path) => path.startsWith("/admin/api/"))).toEqual([]);
  });

  it("puts an unreachable gateway before unavailable member models", async () => {
    install({
      user: member,
      rows: [capability("image", "Images", "down")],
      health: () => json({ error: "unavailable" }, 503),
    });
    const { popover } = await openPill("Gateway status: Gateway unreachable");
    expect(popover).toHaveTextContent("GatewayNot reachable");
    expect(within(popover).queryByText("Your AI")).toBeNull();
  });
});

type Mode = "down" | "unobserved" | "disabled" | "unavailable" | "hardware" | "unused" | "unknown-usage";

function ownerStatus(mode: Mode) {
  return {
    current_health: {
      state: "warn",
      tone: "warn",
      label: "Needs attention",
      detail: "",
      observation_available: mode !== "unavailable" && mode !== "unobserved",
      signals: [{ source: "hardware", tone: "bad", reasons: ["Disk is full"] }],
      services:
        mode === "hardware"
          ? []
          : [
              {
                service_id: "image",
                name: "Image engine",
                configured: true,
                enabled: mode !== "disabled",
                in_use: mode === "unknown-usage" ? null : mode !== "unused",
                role: "required",
                routable: false,
                // "unobserved": a stale down row with no current observation behind it.
                observation: mode === "unavailable" ? "unavailable" : "down",
                tone: "bad",
                last_error: "Connection refused",
              },
            ],
    },
  };
}

const ENGINE_ALERT = "Image engine can’t take requests right now.";

describe("owner alert banner", () => {
  it("names a down, enabled, in-use engine and links to its Status section", async () => {
    install({ user: owner, rows: [], status: ownerStatus("down") });
    render(
      <AppShell>
        <h1>Models</h1>
      </AppShell>,
    );
    const banner = (await screen.findByText(ENGINE_ALERT, { exact: false })).closest("p")!;
    expect(banner).toHaveAttribute("role", "status");
    expect(within(banner).getByRole("link", { name: "See Status" })).toHaveAttribute(
      "href",
      "/status#engines",
    );
  });

  it("repeats the engine alert in the owner's health popover", async () => {
    install({ user: owner, rows: [], status: ownerStatus("down") });
    const { popover } = await openPill(/^Gateway status:/);
    expect(popover).toHaveTextContent(`Engines${ENGINE_ALERT}`);
    expect(popover).toHaveTextContent("GatewayReachable");
  });

  it.each(["disabled", "unavailable", "unobserved", "hardware", "unused", "unknown-usage"] as const)(
    "shows no banner for %s",
    async (mode) => {
      install({ user: owner, rows: [], status: ownerStatus(mode) });
      render(
        <AppShell>
          <h1>Models</h1>
        </AppShell>,
      );
      // The status query has landed once the pill leaves "Checking".
      const pill = (
        await screen.findAllByRole("button", { name: /^Gateway status: (?!Checking)/ })
      )[0];
      await userEvent.setup().click(pill);
      await screen.findByText("Your AI");
      await waitFor(() =>
        expect(requested.filter((path) => path === "/admin/api/status").length).toBeGreaterThan(0),
      );
      expect(screen.queryByText(ENGINE_ALERT)).toBeNull();
      expect(screen.queryByRole("status")).toBeNull();
      // Hardware signals never become an owner-wide banner.
      expect(screen.queryByText("Disk is full")).toBeNull();
    },
  );

  it("names an enabled provider that is explicitly unreachable", async () => {
    install({
      user: owner,
      rows: [],
      providers: {
        providers: [
          { name: "Remote provider", enabled: true, reachable: false, key_set: true },
        ],
      },
    });
    render(
      <AppShell>
        <h1>Models</h1>
      </AppShell>,
    );
    const banner = (
      await screen.findByText("Remote provider can’t take requests right now.", { exact: false })
    ).closest("p")!;
    expect(banner).toHaveAttribute("role", "status");
    expect(within(banner).getByRole("link", { name: "See Status" })).toHaveAttribute(
      "href",
      "/status#providers",
    );
  });
});
