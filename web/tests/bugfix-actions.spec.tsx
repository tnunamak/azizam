import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import AppsPage from "@/app/apps/page";
import ProvidersPage from "@/app/providers/page";

const accountMocks = vi.hoisted(() => ({
  discovery: vi.fn(),
  session: vi.fn(),
  createApp: vi.fn(),
  mintKey: vi.fn(),
  revokeKey: vi.fn(),
  deleteApp: vi.fn(),
  search: new URLSearchParams(),
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => accountMocks.search,
}));
vi.mock("@/lib/account-api", () => ({ accountApi: accountMocks }));

function renderWithQuery(ui: React.ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
  return client;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("bugfix actions", () => {
  beforeEach(() => {
    accountMocks.search = new URLSearchParams();
    accountMocks.discovery.mockResolvedValue({
      recipes: {
        openai: { canonical_base_url: "https://gateway.example/openai/v1" },
      },
    });
  });

  it("removes an existing app through the account API", async () => {
    const user = userEvent.setup();
    const app = {
      id: "app-removable",
      name: "Old app",
      kind: "personal",
      disabled_at: null,
      created_at: 1710000000,
      profile: { name: "Local only" },
      can_mint_keys: true,
      credentials: [],
    };
    accountMocks.session
      .mockResolvedValueOnce({
        authenticated: true,
        csrf_token: "csrf",
        user: { email: "member@example.test" },
        apps: [app],
      })
      .mockResolvedValue({
        authenticated: true,
        csrf_token: "csrf",
        user: { email: "member@example.test" },
        apps: [],
      });
    accountMocks.deleteApp.mockResolvedValue({});

    renderWithQuery(<AppsPage />);
    expect(
      await screen.findByRole("heading", { name: "Old app" }),
    ).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Actions for Old app" }),
    );
    await user.click(screen.getByRole("menuitem", { name: "Remove app" }));
    await user.click(screen.getByRole("button", { name: "Remove app" }));

    await waitFor(() =>
      expect(accountMocks.deleteApp).toHaveBeenCalledWith("app-removable"),
    );
    expect(await screen.findByText("Connect your first app")).toBeVisible();
  });

  it("shows the provider connection check error returned by the API", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/admin/api/session")) {
          return new Response(
            JSON.stringify({
              actor: {
                subject: "owner",
                email: null,
                roles: ["owner"],
                csrf_token: "csrf",
              },
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        if (url.endsWith("/admin/api/access-plans")) {
          return new Response(JSON.stringify({ access_plans: [] }), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        if (url.endsWith("/admin/api/providers/-/catalog")) {
          return new Response(
            JSON.stringify({
              providers: [
                {
                  id: "anthropic",
                  display_name: "Anthropic",
                  logo_key: "an",
                  protocol: "openai-chat",
                  base_url: "https://api.anthropic.com/v1",
                  billing_dimensions: ["input tokens"],
                },
              ],
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        if (url.endsWith("/admin/api/providers/-/test")) {
          expect(init?.method).toBe("POST");
          return new Response(
            JSON.stringify({
              error: {
                code: "provider_connection_failed",
                message:
                  "Gateway check could not reach the provider. Check the endpoint and try again.",
              },
            }),
            { status: 400, headers: { "content-type": "application/json" } },
          );
        }
        if (url.endsWith("/admin/api/providers")) {
          return new Response(
            JSON.stringify({
              providers: [],
              global: { spend: { status: "known" } },
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        return new Response("{}", { status: 404 });
      }),
    );

    renderWithQuery(<ProvidersPage />);
    await user.click(
      await screen.findByRole("button", { name: "Add provider" }),
    );
    await user.type(screen.getByLabelText("Search providers"), "anthropic");
    await user.click(screen.getByRole("button", { name: /Anthropic/ }));
    await user.type(
      screen.getByLabelText("Provider API key"),
      "sk-ant-invalid",
    );
    await user.click(
      screen.getByRole("button", { name: "Check provider connection" }),
    );

    expect(
      await screen.findByText(
        "Gateway check could not reach the provider. Check the endpoint and try again.",
      ),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });
});
