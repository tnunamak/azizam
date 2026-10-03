import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
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
              key_set: true,
              spend_usd: 1.25,
              cap_usd: 10,
              secret_ref: "OPENROUTER_API_KEY",
            },
          ],
        }),
      ),
    );
    renderWithQuery(<ProvidersPage />);
    expect(
      await screen.findByRole("heading", { name: "OpenRouter" }),
    ).toBeVisible();
    expect(screen.getByText("Ready")).toBeVisible();
    expect(screen.getByText("Set")).toBeVisible();
    expect(screen.getByText("$1.25 of $10.00")).toBeVisible();
    await waitFor(() =>
      expect(screen.queryByText("OPENROUTER_API_KEY")).toBeNull(),
    );
  });

  it("does not call unknown or disabled providers Starting", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          providers: [
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
    expect(screen.getByText("Unavailable")).toBeVisible();
    expect(screen.getByText("Provider is disabled.")).toBeVisible();
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
        if (url === "/me/api/media?limit=5") {
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
      await screen.findByRole("heading", { name: "Recent creations" }),
    ).toBeVisible();
    expect(await screen.findByText("A quiet gateway")).toBeVisible();
    expect(
      screen.getByRole("link", { name: /Image A quiet gateway/ }),
    ).toHaveAttribute("href", "/library");
    expect(requested).toContain("/me/api/media?limit=5");
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
        if (url === "/me/api/media?limit=5") {
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
      await screen.findByText("Recent creations could not be loaded."),
    ).toBeVisible();
    expect(requested).toContain("/me/api/media?limit=5");
    expect(requested.some((url) => url.includes("scope=everyone"))).toBe(false);
  });
});
