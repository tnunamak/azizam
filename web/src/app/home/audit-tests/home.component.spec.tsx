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
      id: "default",
      operation: "chat.completions",
      available: true,
      status: "ready",
      reason_code: "ready",
    },
  ],
  credential_aliases: {
    "cred-browser": [
      {
        id: "default",
        operation: "chat.completions",
        available: true,
        status: "ready",
        reason_code: "ready",
      },
    ],
  },
  new_app_aliases: [
    {
      id: "default",
      operation: "chat.completions",
      available: true,
      status: "ready",
      reason_code: "ready",
    },
  ],
  status: {
    state: "ready",
    reason_code: "ready",
    affected_capability_ids: [],
    counts: { total: 1, available: 1, affected: 0 },
  },
  receipts: [
    {
      request_id: "req-1",
      status: "succeeded",
      requested_model: "default",
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
  it("renders audio playback and video frames in Recent", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path === "/.well-known/azizam")
          return Response.json({ service: { name: "Family gateway" } });
        if (path === "/me/api/home") return Response.json(home);
        if (path === "/admin/api/setup-progress")
          return Response.json({ ...progress(), complete: true });
        if (path === "/me/api/media?limit=50")
          return Response.json({
            assets: [
              {
                asset_id: "audio-1",
                kind: "audio",
                prompt: "gentle rain over a quiet porch",
                model: "audio-local",
                created_at: 1,
                download_url: "https://media.example/audio.wav",
              },
              {
                asset_id: "video-1",
                kind: "video",
                prompt: "a fox walking through snow",
                model: "video-local",
                created_at: 1,
                download_url: "https://media.example/video.mp4",
                poster_asset_id: "poster-1",
              },
              {
                asset_id: "video-no-poster",
                kind: "video",
                prompt: "a small boat crossing a lake",
                download_url: "https://media.example/video-no-poster.mp4",
              },
            ],
          });
        if (path === "/me/api/media/poster-1/download?scope=me")
          return Response.json({
            download_url: "https://media.example/poster.jpg",
          });
        throw new Error(`Unexpected request: ${path}`);
      }),
    );

    renderHome();
    expect(
      await screen.findByText("gentle rain over a quiet porch"),
    ).toBeVisible();
    const audio = document.querySelector("audio");
    expect(audio).toHaveAttribute("src", "https://media.example/audio.wav");
    expect(
      screen.getByRole("button", {
        name: "Play gentle rain over a quiet porch",
      }),
    ).toBeVisible();
    expect(await screen.findByText("a fox walking through snow")).toBeVisible();
    const poster = document.querySelector(
      'img[src="https://media.example/poster.jpg"]',
    );
    expect(poster).not.toBeNull();
    const video = document.querySelector(
      'video[src="https://media.example/video-no-poster.mp4"]',
    );
    expect(video).toHaveAttribute("preload", "metadata");
    expect((video as HTMLVideoElement).muted).toBe(true);
    expect(screen.queryByText("Video preview unavailable")).toBeNull();
  });

  it("keeps neutral fallback tiles when media previews are unavailable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path === "/.well-known/azizam")
          return Response.json({ service: { name: "Family gateway" } });
        if (path === "/me/api/home") return Response.json(home);
        if (path === "/admin/api/setup-progress")
          return Response.json({ ...progress(), complete: true });
        if (path === "/me/api/media?limit=50")
          return Response.json({
            assets: [
              { asset_id: "image-no-content", kind: "image" },
              {
                asset_id: "video-no-source",
                kind: "video",
                poster_asset_id: null,
              },
              {
                asset_id: "audio-no-prompt",
                kind: "audio",
                download_url: "/audio.wav",
              },
            ],
          });
        throw new Error(`Unexpected request: ${path}`);
      }),
    );

    renderHome();
    expect(
      await screen.findByRole("heading", { name: "Recent" }),
    ).toBeVisible();
    await waitFor(() =>
      expect(document.querySelector('audio[src="/audio.wav"]')).not.toBeNull(),
    );
    expect(screen.getByRole("button", { name: "Play Audio" })).toBeVisible();
    expect(
      screen.getByRole("img", { name: "Video preview unavailable" }),
    ).toBeVisible();
    expect(document.querySelector('audio[src="/audio.wav"]')).not.toBeNull();
    expect(document.querySelector('video[src="/video.mp4"]')).toBeNull();
  });
  it("uses server capability names, model recommendations, icons and summary", async () => {
    const serverHome = {
      ...home,
      status: {
        state: "ready",
        reason_code: "ready",
        affected_capability_ids: [],
        counts: { total: 1, available: 1, affected: 0 },
      },
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
          reason_code: "ready",
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
          reason_code: "ready",
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const route = String(input);
        if (route === "/me/api/home") return Response.json(serverHome);
        if (route === "/me/api/media?limit=50")
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
    await screen.findByRole("link", { name: "Music" });
    expect(screen.queryByText("Ready")).toBeNull();
    expect(screen.queryByText("Details")).toBeNull();
    expect(screen.getByRole("img", { name: "Music" })).toBeVisible();
    expect(screen.getByRole("img", { name: "Sound effects" })).toBeVisible();
    expect(screen.getByRole("link", { name: "Music" })).toHaveAttribute(
      "href",
      "/models?capability=audio",
    );
    expect(screen.queryByText("ACE-Step XL Turbo")).toBeNull();
    expect(screen.queryByText("song")).toBeNull();
    expect(
      screen.queryByText("Create sound effects from a prompt."),
    ).toBeNull();
  });
  it("shows capabilities without setup controls on Home", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path === "/.well-known/azizam")
          return Response.json({ service: { name: "Family gateway" } });
        if (path === "/me/api/home")
          return Response.json({ ...home, receipts: [] });
        if (path === "/me/api/media?limit=50")
          return Response.json({ assets: [] });
        if (path === "/admin/api/setup-progress")
          return Response.json(progress());
        throw new Error(`Unexpected request: ${path}`);
      }),
    );
    renderHome();
    expect(
      await screen.findByRole("heading", { name: "What you can use" }),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "Dismiss setup" })).toBeNull();
    expect(
      screen.queryByRole("heading", { name: "Finish setting up" }),
    ).toBeNull();
  });

  it("does not show manual setup evidence confirmation buttons", async () => {
    const evidencePosts: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        if (path === "/.well-known/azizam")
          return Response.json({ service: { name: "Family gateway" } });
        if (path === "/me/api/home")
          return Response.json({ ...home, receipts: [] });
        if (path === "/me/api/media?limit=50")
          return Response.json({ assets: [] });
        if (path === "/me/api/session")
          return Response.json({ authenticated: true, csrf_token: "csrf" });
        if (path === "/admin/api/setup-progress")
          return Response.json(progress());
        if (path === "/me/api/connection-test")
          return Response.json({
            ok: true,
            latency_ms: 12,
            reason: "Gateway responded with this key",
            path: "/openai/v1/chat/completions",
            model: "default",
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
      await screen.findByRole("heading", { name: "Connect an app" }),
    ).toBeVisible();
    expect(evidencePosts).toEqual([]);

    await userEvent.click(screen.getByRole("button", { name: "Open WebUI" }));
    await userEvent.click(screen.getByRole("combobox", { name: "API key" }));
    await userEvent.click(
      await screen.findByRole("option", { name: /Browser test app/ }),
    );
    await userEvent.type(
      screen.getByLabelText("Paste your API key"),
      "opk_test_secret",
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Check connection" }),
    );
    await screen.findByText("Gateway responded with this key");
    expect(evidencePosts).toEqual([]);
    expect(
      screen.queryByRole("button", { name: "I copied the settings" }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: "My app sent a request" }),
    ).toBeNull();
    expect(evidencePosts).toEqual([]);
  });
  it("does not infer missing chat access from a missing recipe alias", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path === "/.well-known/azizam")
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
                reason_code: "ready",
              },
            ],
          });
        if (path === "/me/api/media?limit=50")
          return Response.json({ assets: [] });
        throw new Error(`Unexpected request: ${path}`);
      }),
    );
    renderHome();
    await userEvent.click(
      await screen.findByRole("button", { name: "Open WebUI" }),
    );
    await screen.findByText("This key does not include the default model");
    expect(screen.queryByRole("textbox", { name: "Model" })).toBeNull();
    expect(
      screen.queryByText("No chat model is available to this key yet"),
    ).toBeNull();
  });
});
