import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import HomePage from "../page";
import { ViewerContext } from "@/components/shell/session";

const app = {
  id: "app-browser",
  name: "Browser test app",
  kind: "personal",
  disabled_at: null,
  created_at: 1700000000,
  profile: null,
  can_mint_keys: true,
  credentials: [
    {
      id: "cred-browser",
      subject_kind: "app",
      subject_id: "app-browser",
      app_id: "app-browser",
      profile_id: null,
      prefix: "opk_test",
      display_name: "Browser key",
      scopes: [],
      created_at: 1700000000,
      expires_at: null,
      revoked_at: null,
      last_used_at: null,
    },
  ],
};

const home = {
  authenticated: true,
  csrf_token: "csrf",
  user: { id: "owner", name: "Alex", email: null, role: "owner" },
  apps: [app],
  public_base_url: "https://gateway.example",
  api_base_url: "https://gateway.example/openai/v1",
  capabilities: [],
  aliases: [
    {
      id: "fast",
      operation: "chat.completions",
      available: true,
      status: "ready",
      reason: "Chat is ready.",
    },
  ],
  credential_aliases: {
    "cred-browser": [
      {
        id: "fast",
        operation: "chat.completions",
        available: true,
        status: "ready",
        reason: "Chat is ready.",
      },
    ],
  },
  new_app_aliases: [
    {
      id: "fast",
      operation: "chat.completions",
      available: true,
      status: "ready",
      reason: "Chat is ready.",
    },
  ],
  status: { state: "ready", reason: "Ready." },
  receipts: [
    {
      request_id: "req-1",
      status: "succeeded",
      requested_model: "fast",
      intent: "chat.completions",
      _stored_at: 1700000000,
    },
  ],
  creations: null,
};

function progress(evidence = {}) {
  return {
    complete: false,
    steps: [
      {
        id: "connect_service",
        done: true,
        optional: false,
        reason: "A service is connected.",
        href: "/ui/engines/",
      },
      {
        id: "test_request",
        done: true,
        optional: false,
        reason: "A gateway request succeeded.",
        href: "#connect",
      },
      {
        id: "connect_app",
        done: true,
        optional: false,
        reason: "Legacy key-use evidence must not decide this step.",
        href: "#connect",
      },
    ],
    evidence: {
      gateway_request: { recorded: true, source: "receipt" },
      settings_copied: { recorded: false, source: "none" },
      external_client_confirmed: { recorded: false, source: "none" },
      receipt_viewed: { recorded: false, source: "none" },
      ...evidence,
    },
  };
}

function renderHome() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ViewerContext.Provider
        value={{
          authenticated: true,
          name: "Alex",
          email: null,
          role: "owner",
          admin: true,
        }}
      >
        <HomePage />
      </ViewerContext.Provider>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

beforeAll(() => {
  Element.prototype.hasPointerCapture ??= () => false;
  Element.prototype.setPointerCapture ??= () => undefined;
  Element.prototype.releasePointerCapture ??= () => undefined;
  Element.prototype.scrollIntoView ??= () => undefined;
});

describe("Home audit component behavior", () => {
  it("uses server capability names, model recommendations, icons and summary", async () => {
    const serverHome = {
      ...home,
      status: { state: "ready", reason: "Everything you can use is ready." },
      capabilities: [
        {
          capability: "music",
          capability_title: "Music",
          icon_key: "music",
          title: "ACE-Step XL Turbo",
          recommended_model: "ACE-Step XL Turbo",
          summary: "Create music from a prompt.",
          model: "music",
          use_name: "song",
          use_names: ["music", "song"],
          available: true,
          status: "ready",
          reason: "Ready.",
        },
        {
          capability: "sfx",
          capability_title: "Sound effects",
          icon_key: "sfx",
          title: "Sound generator",
          recommended_model: "Sound generator",
          summary: "Create sound effects from a prompt.",
          model: "sfx",
          use_name: "sfx",
          available: true,
          status: "ready",
          reason: "Ready.",
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const route = String(input);
        if (route === "/me/api/home") return Response.json(serverHome);
        if (route === "/me/api/media?limit=5")
          return Response.json({ assets: [] });
        if (route === "/admin/api/setup-progress")
          return Response.json({
            ...progress(),
            complete: true,
            dismissed: true,
          });
        return Response.json({});
      }),
    );
    renderHome();
    expect(
      await screen.findByText("Everything you can use is ready."),
    ).toBeVisible();
    await waitFor(() =>
      expect(screen.getByText("ACE-Step XL Turbo")).toBeVisible(),
    );
    expect(screen.getByRole("img", { name: "Music" })).toBeVisible();
    expect(screen.getByRole("img", { name: "Sound effects" })).toBeVisible();
    expect(screen.getByText("song")).toBeVisible();
    expect(screen.getByText("Create music from a prompt.")).toBeVisible();
    expect(
      screen.getByText("Create sound effects from a prompt."),
    ).toBeVisible();
  });
  it("lets the owner dismiss setup and shows capabilities after the saved dismissal", async () => {
    let dismissed = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        if (path === "/.well-known/ai-gateway")
          return Response.json({ service: { name: "Family gateway" } });
        if (path === "/me/api/home") return Response.json(home);
        if (path === "/me/api/media?limit=5")
          return Response.json({ assets: [] });
        if (path === "/me/api/session")
          return Response.json({ authenticated: true, csrf_token: "csrf" });
        if (path === "/admin/api/setup-progress")
          return Response.json({ ...progress(), dismissed });
        if (path === "/admin/api/setup-dismiss") {
          expect(init?.method).toBe("POST");
          expect(new Headers(init?.headers).get("x-ai-gateway-csrf")).toBe(
            "csrf",
          );
          expect(
            new Headers(init?.headers).get("x-ai-gateway-user-action"),
          ).toBe("1");
          dismissed = true;
          return Response.json({ dismissed: true });
        }
        throw new Error(`Unexpected request: ${path}`);
      }),
    );
    renderHome();
    await userEvent.click(
      await screen.findByRole("button", { name: "Dismiss setup" }),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("heading", { name: "Finish setting up" }),
      ).toBeNull(),
    );
    expect(
      screen.getByRole("heading", { name: "What you can use" }),
    ).toBeVisible();
    expect(dismissed).toBe(true);
  });

  it("keeps setup evidence explicit and posts only when confirmation buttons are clicked", async () => {
    const evidencePosts: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        if (path === "/.well-known/ai-gateway")
          return Response.json({ service: { name: "Family gateway" } });
        if (path === "/me/api/home") return Response.json(home);
        if (path === "/me/api/media?limit=5")
          return Response.json({ assets: [] });
        if (path === "/me/api/session")
          return Response.json({ authenticated: true, csrf_token: "csrf" });
        if (path === "/admin/api/setup-progress")
          return Response.json(progress());
        if (path === "/me/api/connection-test")
          return Response.json({
            ok: true,
            latency_ms: 12,
            reason: "Gateway check passed.",
            path: "/openai/v1/chat/completions",
            model: "fast",
            request_id: "req-1",
          });
        if (path === "/admin/api/setup-evidence") {
          const body = JSON.parse(String(init?.body)) as { event: string };
          evidencePosts.push(body);
          return Response.json({
            event: body.event,
            recorded: true,
            source: "user_confirmation",
          });
        }
        throw new Error(`Unexpected request: ${path}`);
      }),
    );

    renderHome();
    expect(
      await screen.findByText("Next: Copy settings into your client"),
    ).toBeVisible();
    expect(evidencePosts).toEqual([]);

    await userEvent.click(screen.getByRole("combobox", { name: "App key" }));
    await userEvent.click(
      await screen.findByRole("option", { name: /Browser test app/ }),
    );
    await userEvent.type(
      screen.getByLabelText("Paste your key"),
      "opk_test_secret",
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Gateway check" }),
    );
    await screen.findByText("Gateway check passed.");
    expect(evidencePosts).toEqual([]);
    await userEvent.click(
      screen.getByRole("button", { name: "I copied the settings" }),
    );
    await waitFor(() => expect(evidencePosts).toHaveLength(1));
    await userEvent.click(
      screen.getByRole("button", { name: "My client sent a request" }),
    );
    await waitFor(() => expect(evidencePosts).toHaveLength(2));
    await userEvent.click(screen.getByText("Setup details"));
    await userEvent.click(
      screen.getByRole("button", { name: "I viewed this receipt" }),
    );
    await waitFor(() => expect(evidencePosts).toHaveLength(3));
    expect(evidencePosts).toEqual([
      { event: "settings_copied", app_id: "app-browser" },
      { event: "external_client_confirmed", app_id: "app-browser" },
      { event: "receipt_viewed", request_id: "req-1" },
    ]);
  });
  it("does not infer missing chat access from a missing recipe alias", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path === "/.well-known/ai-gateway")
          return Response.json({ service: { name: "Family gateway" } });
        if (path === "/me/api/home")
          return Response.json({
            ...home,
            user: { ...home.user, role: "member" },
            aliases: [],
            new_app_aliases: [],
            credential_aliases: { "cred-browser": [] },
            capabilities: [
              {
                capability: "chat",
                intent: "chat.completions",
                model: "declared-model",
                available: true,
                status: "ready",
                reason: "Chat is ready.",
              },
            ],
          });
        if (path === "/me/api/media?limit=5")
          return Response.json({ assets: [] });
        throw new Error(`Unexpected request: ${path}`);
      }),
    );
    renderHome();
    expect(
      await screen.findByText("No chat alias is advertised for this key yet"),
    ).toBeVisible();
    expect(
      screen.queryByText("No chat model is available to this key yet"),
    ).toBeNull();
    expect(
      screen.getByText(/Models without an alias may still be available/),
    ).toBeVisible();
  });
});
