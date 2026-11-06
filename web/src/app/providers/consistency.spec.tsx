import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import ProvidersPage from "./page";

// Moved down from the consistency-polish2 browser spec. The bodies are the
// ones the browser intercepted at GET /admin/api/providers; the page fetches
// them through the real query code, nothing is primed in the cache.

beforeAll(() => {
  Element.prototype.hasPointerCapture ??= () => false;
  Element.prototype.setPointerCapture ??= () => undefined;
  Element.prototype.releasePointerCapture ??= () => undefined;
  Element.prototype.scrollIntoView ??= () => undefined;
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function gateway(providers: Response | (() => Response), extra: Record<string, unknown> = {}) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      calls.push(path);
      if (path === "/admin/api/providers")
        return typeof providers === "function" ? providers() : providers.clone();
      if (path in extra) return Response.json(extra[path]);
      return Response.json({});
    }),
  );
  return calls;
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ProvidersPage />
    </QueryClientProvider>,
  );
}

async function row(name: string) {
  const heading = await screen.findByRole("heading", { name });
  return heading.closest("li") as HTMLElement;
}

describe("Providers (current API shape)", () => {
  it("shows the API's spend, key and name without inventing readiness or names", async () => {
    gateway(
      Response.json({
        global: { spend: { status: "unknown", monthly_usd: 888 } },
        providers: [
          {
            id: "internal-provider-record",
            name: "internal-provider-record",
            enabled: true,
            key_status: { state: "set" },
            spend: { status: "known", daily_usd: 1, monthly_usd: 12.5 },
            caps: { daily_usd: 2, monthly_usd: 50 },
          },
          {
            id: "missing-key",
            name: "Missing key provider",
            enabled: true,
            key_status: { state: "missing" },
            spend: { status: "unknown", monthly_usd: 999 },
          },
        ],
      }),
    );
    renderPage();
    const user = userEvent.setup();
    // An internal record name is never shown as the provider's name.
    const provider = await row("Remote provider");
    await user.click(within(provider).getByText("Manage", { exact: true }));
    await user.click(within(provider).getByText("Provider details", { exact: true }));
    expect(
      within(provider).getByText("Provider ID: internal-provider-record", { exact: true }),
    ).toBeVisible();
    expect(within(provider).getByText("Key set", { exact: true })).toBeVisible();
    expect(
      within(provider).queryByText("Provider connection not confirmed.", { exact: true }),
    ).toBeNull();
    expect(provider).toHaveTextContent("This monthUSD 12.50 of USD 50.00");

    const missing = await row("Missing key provider");
    expect(within(missing).getByText("Unavailable", { exact: true })).toBeVisible();
    expect(within(missing).getByText("Missing key", { exact: true })).toBeVisible();
    await user.click(within(missing).getByText("Manage", { exact: true }));
    // The collapsed row's month, then the overview's Today and This month.
    const unknownSpend = within(missing).getAllByText("Cost not recorded", { exact: true });
    expect(unknownSpend).toHaveLength(3);
    unknownSpend.forEach((node) => expect(node).toBeVisible());
    expect(missing).not.toHaveTextContent("999");
    expect(screen.getByText("Global spend cap")).toBeVisible();
    expect(document.body).not.toHaveTextContent("888");
  });

  it("keeps unknown readiness distinct from observed unavailability", async () => {
    gateway(
      Response.json({
        providers: [
          { id: "unknown", name: "Unchecked provider", enabled: true, reachable: null, key_set: true },
          { id: "down", name: "Unreachable provider", enabled: true, reachable: false, key_set: true },
          { id: "ready", name: "Reachable provider", enabled: true, reachable: true, key_set: true },
        ],
      }),
    );
    renderPage();
    const unknown = await row("Unchecked provider");
    for (const word of ["Provider connection not confirmed.", "Unknown", "Starting", "Unavailable"])
      expect(within(unknown).queryByText(word, { exact: true })).toBeNull();
    const down = await row("Unreachable provider");
    expect(within(down).getByText("Unavailable", { exact: true })).toBeVisible();
    expect(
      within(down).getByText("Provider connection check observed it down.", { exact: true }),
    ).toBeVisible();
    const ready = await row("Reachable provider");
    expect(within(ready).getByText("Ready", { exact: true })).toBeVisible();
  });
});

describe("Providers (empty and failed)", () => {
  it("offers Add provider when none exist", async () => {
    gateway(Response.json({ providers: [] }), {
      "/admin/api/providers/-/catalog": {
        providers: [
          {
            id: "openrouter",
            display_name: "OpenRouter",
            base_url: "https://openrouter.ai/api/v1",
            protocol: "openai-chat",
            billing_dimensions: ["prompt_tokens", "completion_tokens"],
          },
        ],
      },
      "/admin/api/access-plans": { access_plans: [] },
    });
    renderPage();
    expect(await screen.findByText("No providers yet", { exact: true })).toBeVisible();
    await userEvent.setup().click(screen.getAllByRole("button", { name: "Add provider" })[0]);
    expect(await screen.findByRole("heading", { name: "Add provider", level: 2 })).toBeVisible();
  });

  it("treats a bare 404 as a load failure with a retry", async () => {
    const calls = gateway(Response.json({}, { status: 404 }));
    renderPage();
    expect(
      await screen.findByText("Provider controls could not be loaded. Try again.", { exact: true }),
    ).toBeVisible();
    const retry = screen.getByRole("button", { name: "Retry providers" });
    expect(retry).toBeVisible();
    expect(screen.queryByText("No providers yet")).toBeNull();
    const before = calls.filter((path) => path === "/admin/api/providers").length;
    await userEvent.setup().click(retry);
    await vi.waitFor(() =>
      expect(calls.filter((path) => path === "/admin/api/providers").length).toBeGreaterThan(before),
    );
  });
});
