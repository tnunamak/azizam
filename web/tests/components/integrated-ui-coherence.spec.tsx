import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import HomePage from "@/app/home/page";
import ProvidersPage from "@/app/providers/page";
import ShowcasePage from "@/app/showcase/page";
import { canonicalDestinationPath } from "@/lib/product";

function renderWithQuery(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>{ui}</QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("integrated UI coherence", () => {
  it("treats /ui/home as the Home destination", () => {
    expect(canonicalDestinationPath("/")).toBe("/");
    expect(canonicalDestinationPath("/ui/")).toBe("/");
    expect(canonicalDestinationPath("/ui/home/")).toBe("/");
    expect(canonicalDestinationPath("/home")).toBe("/");
    expect(canonicalDestinationPath("/ui/models/")).toBe("/models");
  });

  it("announces Showcase recorded-example loading as a status", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>(() => {})),
    );
    renderWithQuery(<ShowcasePage />);
    const loading = screen.getByRole("status", {
      name: "Loading recorded examples",
    });
    expect(loading).toHaveAttribute("aria-busy", "true");
  });

  it("feature-detects a missing provider API before showing provider controls", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 404 })),
    );
    renderWithQuery(<ProvidersPage />);
    expect(
      await screen.findByText("Provider controls are not installed"),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
  });

  it("renders normalized provider status without secret fields", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          providers: [
            {
              id: "openrouter",
              name: "OpenRouter",
              enabled: true,
              reachable: true,
              key_status: {
                state: "set",
                source: "secret_store",
                suffix: "4f2a",
              },
              spend: { daily_usd: 1.25, monthly_usd: 1.25, status: "measured" },
              caps: { daily_usd: 10, monthly_usd: 10 },
              catalog: { base_url: "" },
              models: [
                {
                  model: "openrouter/anthropic/claude-sonnet-4.5",
                  upstream_model: "anthropic/claude-sonnet-4.5",
                  name: "Claude Sonnet 4.5",
                  enabled: true,
                  price: {
                    input_usd_per_million_tokens: "3.00",
                    output_usd_per_million_tokens: "15.00",
                  },
                },
              ],
              secret_ref: "OPENROUTER_API_KEY",
            },
            {
              id: "tinyspend",
              name: "Tiny Spend",
              enabled: true,
              reachable: true,
              key_status: {
                state: "set",
                source: "secret_store",
                suffix: "9a11",
              },
              spend: {
                daily_usd: 0,
                monthly_usd: 0.000025368,
                status: "measured",
              },
              caps: { daily_usd: 0.01, monthly_usd: 1.25 },
              catalog: { base_url: "" },
              models: [],
            },
            {
              id: "cent",
              name: "Cent Provider",
              enabled: true,
              reachable: true,
              key_status: {
                state: "set",
                source: "secret_store",
                suffix: "1c00",
              },
              spend: {
                daily_usd: 0.01,
                monthly_usd: 0.000000042,
                status: "measured",
              },
              caps: { daily_usd: 1.25, monthly_usd: 10 },
              catalog: { base_url: "" },
              models: [],
            },
          ],
        }),
      ),
    );
    renderWithQuery(<ProvidersPage />);
    expect(
      await screen.findByRole("heading", { name: "OpenRouter" }),
    ).toBeVisible();
    expect(screen.getAllByText("Ready")).toHaveLength(3);
    expect(screen.getByText("Key ends in 4f2a")).toBeVisible();
    for (const manage of screen.getAllByText("Manage", { exact: true })) {
      await user.click(manage);
    }
    expect(screen.getAllByText("$1.25 of $10.00")).toHaveLength(3);
    expect(screen.getByText("$0.00 of $0.01")).toBeVisible();
    expect(screen.getByText("$0.01 of $1.25")).toBeVisible();
    expect(screen.getAllByText("<$0.01 of $1.25")).toHaveLength(2);
    expect(screen.getAllByText("<$0.01 of $10.00")).toHaveLength(2);
    expect(screen.getByRole("cell", { name: "3" })).toBeVisible();
    expect(screen.getByRole("cell", { name: "15" })).toBeVisible();
    await waitFor(() =>
      expect(screen.queryByText("OPENROUTER_API_KEY")).toBeNull(),
    );
  });

  it("shows reconciliation state without inventing a pending count", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          providers: [
            {
              id: "openrouter",
              name: "OpenRouter",
              enabled: true,
              reachable: true,
              key_status: {
                state: "set",
                source: "secret_store",
                suffix: "4f2a",
              },
              spend: {
                daily_usd: 1.25,
                monthly_usd: 1.25,
                status: "needs_reconciliation",
              },
              caps: { daily_usd: 10, monthly_usd: 10 },
              models: [],
            },
          ],
        }),
      ),
    );
    renderWithQuery(<ProvidersPage />);
    await user.click(await screen.findByText("Manage", { exact: true }));
    expect(
      await screen.findByText("Charges need reconciliation"),
    ).toBeVisible();
    expect(screen.queryByText("1 charge need reconciliation")).toBeNull();
    expect(screen.getByText(/verified provider charge/i)).toBeVisible();
  });

  it("does not call unknown or disabled providers Starting", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          providers: [
            {
              id: "unchecked-remote",
              name: "Unchecked remote",
              enabled: true,
              reachable: null,
              key_status: { state: "set" },
            },
            {
              id: "disabled-remote",
              name: "Disabled remote",
              enabled: false,
              reachable: null,
              key_set: null,
            },
          ],
        }),
      ),
    );
    renderWithQuery(<ProvidersPage />);
    expect(
      await screen.findByRole("heading", { name: "Disabled remote" }),
    ).toBeVisible();
    expect(
      screen.getByRole("heading", { name: "Unchecked remote" }),
    ).toBeVisible();
    expect(screen.queryByText("Unknown")).toBeNull();
    expect(screen.queryByText("Configured, observation unknown.")).toBeNull();
    expect(screen.getByText("Unavailable")).toBeVisible();
    expect(screen.getByText("Configured, currently disabled.")).toBeVisible();
    expect(screen.queryByText("Starting")).toBeNull();
  });

  it("loads Home recent creations from the signed-in Library API scope", async () => {
    const requested: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        requested.push(url);
        if (url === "/me/api/home") {
          return Response.json({
            authenticated: true,
            user: {
              id: "user-home",
              name: "Test user",
              email: "test@example.test",
              role: "member",
            },
            apps: [],
            csrf_token: "csrf",
            public_base_url: "https://gateway.example",
            api_base_url: "https://gateway.example/openai/v1",
            capabilities: [],
            aliases: [],
            credential_aliases: {},
            new_app_aliases: [],
            status: { state: "ready", reason: "All systems ready." },
            receipts: [],
            creations: null,
          });
        }
        if (url === "/me/api/media?limit=50") {
          return Response.json({
            assets: [
              {
                asset_id: "img_recent",
                kind: "image",
                prompt: "A quiet gateway",
                model: "image",
                created_at: 1700000000,
                download_url: "/me/api/media/img_recent/download",
              },
            ],
            next_cursor: null,
          });
        }
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );
    renderWithQuery(<HomePage />);
    expect(
      await screen.findByRole("heading", { name: "Recent" }),
    ).toBeVisible();
    expect(await screen.findByText("A quiet gateway")).toBeVisible();
    expect(
      screen.getByRole("link", { name: /A quiet gateway/ }),
    ).toHaveAttribute("href", "/library?asset=img_recent");
    expect(requested).toContain("/me/api/media?limit=50");
    expect(requested.some((url) => url.includes("scope=everyone"))).toBe(false);
  });

  it("shows a Home recent creations error without using owner scope", async () => {
    const requested: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        requested.push(url);
        if (url === "/me/api/home") {
          return Response.json({
            authenticated: true,
            user: {
              id: "user-home",
              name: "Test user",
              email: "test@example.test",
              role: "member",
            },
            apps: [],
            csrf_token: "csrf",
            public_base_url: "https://gateway.example",
            api_base_url: "https://gateway.example/openai/v1",
            capabilities: [],
            aliases: [],
            credential_aliases: {},
            new_app_aliases: [],
            status: { state: "ready", reason: "All systems ready." },
            receipts: [],
            creations: null,
          });
        }
        if (url === "/me/api/media?limit=50") {
          return Response.json(
            { error: { message: "Library unavailable" } },
            { status: 503 },
          );
        }
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );
    renderWithQuery(<HomePage />);
    expect(
      await screen.findByText(
        "Recent creations could not be loaded. Try again.",
      ),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Try again" })).toBeVisible();
    expect(requested).toContain("/me/api/media?limit=50");
    expect(requested.some((url) => url.includes("scope=everyone"))).toBe(false);
  });
});
