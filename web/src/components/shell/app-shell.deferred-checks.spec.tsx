import { cleanup, render, screen, waitFor } from "@testing-library/react";
import type { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AppShell } from "./app-shell";
import { resetSessionCache } from "@/lib/api";
import { SHELL_CHECKS_DELAY_MS } from "./use-after-first-load";

// The owner's gateway-wide checks (status, providers, registry, recent requests) wait until the
// page has had its turn. Found on a copy of the live data: every page started seven owner
// requests beside its own, and a page's own request took ten times longer for it.

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
vi.mock("next/navigation", () => ({
  usePathname: () => "/usage",
  useSearchParams: () => new URLSearchParams(""),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

const owner = { id: "owner", name: "Alex Owner", role: "owner", email: "owner@example.test" };
const requested: string[] = [];

function json(body: unknown) {
  return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } }));
}

beforeEach(() => {
  clients.forEach((client) => client.clear());
  resetSessionCache();
  requested.length = 0;
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    requested.push(new URL(url, "http://x.test").pathname);
    if (url.endsWith("/me/api/session")) return json({ authenticated: true, csrf_token: "fixture", apps: [], user: owner });
    if (url.endsWith("/me/api/home")) return json({ authenticated: true, user: owner, aliases: [], receipts: [], capabilities: [] });
    if (url.endsWith("/health")) return json({ status: "healthy" });
    if (url.includes("/admin/api/activity/requests")) return json({ records: [] });
    if (url.includes("/admin/api/providers")) return json({ providers: [] });
    if (url.endsWith("/admin/api/operator/service-registry")) return json({ service_registry: { schema_version: 1, services: {} } });
    return json({ status: {} });
  }));
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false, media: query, onchange: null, addEventListener: vi.fn(), removeEventListener: vi.fn(),
      addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
    })),
  });
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  HTMLCanvasElement.prototype.getContext = vi.fn(() => null);
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const ownerChecks = ["/admin/api/status", "/admin/api/providers", "/admin/api/operator/service-registry", "/admin/api/activity/requests"];

describe("shell owner checks", () => {
  it("wait for the page, then run, and say Checking meanwhile", async () => {
    render(<AppShell><h1>Usage</h1></AppShell>);
    await waitFor(() => expect(requested).toContain("/me/api/home"));
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(requested.filter((path) => ownerChecks.includes(path))).toEqual([]);
    expect((await screen.findAllByRole("button", { name: /Checking/ })).length).toBeGreaterThan(0);

    await waitFor(
      () => expect(requested).toEqual(expect.arrayContaining(ownerChecks)),
      { timeout: SHELL_CHECKS_DELAY_MS + 3000 },
    );
  }, SHELL_CHECKS_DELAY_MS + 8000);
});
