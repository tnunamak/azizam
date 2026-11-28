import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
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
  it("marks both Checking pills busy and announces the settled status only once", async () => {
    const { rerender } = render(<AppShell><h1>Usage</h1></AppShell>);
    const pills = await screen.findAllByRole("button", { name: "Gateway status: Checking" });
    expect(pills).toHaveLength(2);
    pills.forEach((pill) => expect(pill).toHaveAttribute("aria-busy", "true"));
    const announcement = screen.getByRole("status", { name: "Gateway status announcement" });
    expect(announcement).toBeEmptyDOMElement();
    expect(announcement).toHaveAttribute("aria-live", "polite");
    expect(announcement).toHaveAttribute("aria-atomic", "true");
    const announcements: string[] = [];
    const observer = new MutationObserver(() => {
      if (announcement.textContent) announcements.push(announcement.textContent);
    });
    observer.observe(announcement, { childList: true, characterData: true, subtree: true });
    try {
      await waitFor(() => {
        pills.forEach((pill) => expect(pill).toHaveAttribute("aria-busy", "false"));
        expect(announcement).not.toBeEmptyDOMElement();
      }, { timeout: SHELL_CHECKS_DELAY_MS + 3000 });
      const settled = pills[0].getAttribute("aria-label");
      expect(announcement.textContent).toBe(settled);
      pills.forEach((pill) => expect(pill).toHaveAttribute("aria-label", settled));
      rerender(<AppShell><h1>Usage updated</h1></AppShell>);
      await waitFor(() => expect(announcements).toEqual([settled]));
      expect(screen.getAllByRole("status", { name: "Gateway status announcement" })).toHaveLength(1);
    } finally {
      observer.disconnect();
    }
  }, SHELL_CHECKS_DELAY_MS + 8000);

  it("wait for the page, then run, and say Checking meanwhile", async () => {
    vi.useFakeTimers();
    render(<AppShell><h1>Usage</h1></AppShell>);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(requested).toContain("/me/api/home");
    await act(async () => { await vi.advanceTimersByTimeAsync(150); });
    expect(requested.filter((path) => ownerChecks.includes(path))).toEqual([]);
    expect(screen.getAllByRole("button", { name: /Checking/ }).length).toBeGreaterThan(0);

    await act(async () => { await vi.advanceTimersByTimeAsync(SHELL_CHECKS_DELAY_MS); });
    expect(requested).toEqual(expect.arrayContaining(ownerChecks));
  }, SHELL_CHECKS_DELAY_MS + 8000);
});
