import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import HomePage from "../home-page";
import { ViewerContext } from "@/components/shell/session";
import { mediaGridGap } from "@/components/product/audio-cover";

const app = {
  id: "app-browser",
  name: "Browser test app",
  kind: "personal",
  disabled_at: null,
  created_at: 1700000000,
  access_plan: null,
  can_mint_keys: true,
  credentials: [
    {
      id: "cred-browser",
      subject_kind: "app",
      subject_id: "app-browser",
      app_id: "app-browser",
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
        href: "/engines/",
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
  function mockCraftHome(
    media: Promise<Response> = Promise.resolve(
      Response.json({
        assets: [
          {
            asset_id: "craft-image",
            kind: "image",
            prompt: "A garden",
            download_url: "https://media.example/garden.png",
          },
        ],
      }),
    ),
  ) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path === "/me/api/home")
          return Response.json({
            ...home,
            capabilities: [
              {
                capability: "music",
                icon_key: "music",
                model: "music",
                use_name: "song",
                available: true,
                status: "ready",
                reason_code: "ready",
              },
              {
                capability: "sfx",
                icon_key: "sfx",
                model: "sfx",
                use_name: "sfx",
                available: true,
                status: "busy",
                reason_code: "busy",
              },
              {
                capability: "video",
                icon_key: "video",
                model: "video",
                available: false,
                status: "unavailable",
                reason_code: "unavailable",
              },
            ].map((capability) => ({
              ...capability,
              availability: {
                evidence: { source: "status", observed_at_s: Date.now() / 1000 },
              },
            })),
            receipts: [],
          });
        if (path === "/me/api/media?limit=12") return media;
        if (path === "/admin/api/setup-progress")
          return Response.json({ ...progress(), complete: true });
        return Response.json({});
      }),
    );
  }

  it("asks for Recent creations while Home is still loading", async () => {
    const requested: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        requested.push(path);
        if (path === "/me/api/home") return new Promise<Response>(() => {}); // Home never settles
        return Response.json({ assets: [] });
      }),
    );
    renderHome();
    await waitFor(() => expect(requested).toContain("/me/api/media?limit=12"));
    expect(requested).toContain("/me/api/home");
  });

  it("keeps the loading header, Recent strip and task rows in the settled grid", async () => {
    let finishHome!: (value: Response) => void;
    let finishMedia!: (value: Response) => void;
    const homeResponse = new Promise<Response>((resolve) => { finishHome = resolve; });
    const mediaResponse = new Promise<Response>((resolve) => { finishMedia = resolve; });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/me/api/home") return homeResponse;
      if (path === "/me/api/media?limit=12") return mediaResponse;
      return Response.json({});
    }));
    renderHome();

    const loading = screen.getByRole("status", { name: "Loading Home" });
    const recentPlaceholder = loading.querySelector('[data-slot="home-recent-loading"]')!;
    const taskPlaceholder = loading.querySelector('[data-slot="home-tasks-loading"]')!;
    expect(recentPlaceholder.nextElementSibling).toBe(taskPlaceholder);
    expect(recentPlaceholder.parentElement).toHaveClass("flex", "flex-col", "gap-(--space-section)");
    expect(recentPlaceholder.firstElementChild).toHaveClass("mb-3", "min-h-8", "max-sm:min-h-11");
    const taskRows = taskPlaceholder.querySelectorAll('[data-slot="list-row"]');
    expect(taskRows).toHaveLength(7);
    for (const row of taskRows) {
      expect(row).toHaveClass("min-h-(--row-1)", "max-sm:min-h-(--row-phone-1)");
    }
    const initialStrip = within(loading).getByRole("status", { name: "Loading recent creations" });
    const stripClasses = initialStrip.className;
    expect(initialStrip.children).toHaveLength(4);
    for (const tile of initialStrip.children) {
      expect(tile).toHaveClass("aspect-square", "rounded-media", "motion-reduce:animate-none");
    }
    const header = screen.getByRole("heading", { name: "Home", level: 1 }).closest("header")!;
    expect(header.lastElementChild).toHaveClass("mt-1");
    expect(Array.from(header.querySelectorAll("*")).some((element) => element.classList.contains("lg:hidden"))).toBe(true);

    finishHome(Response.json(home));
    await waitFor(() => expect(screen.queryByRole("status", { name: "Loading Home" })).toBeNull());
    const recent = screen.getByRole("region", { name: "Recent" });
    const strip = within(recent).getByRole("status", { name: "Loading recent creations" });
    expect(strip.className).toBe(stripClasses);
    expect(recent.parentElement).toHaveClass("flex", "flex-col", "gap-(--space-section)");
    expect(screen.getByRole("heading", { name: "Home", level: 1 }).closest("header")!.lastElementChild).toHaveClass("mt-1");

    finishMedia(Response.json({ assets: [{
      asset_id: "loaded-image",
      kind: "image",
      prompt: "A garden",
      download_url: "/garden.png",
    }] }));
    const image = await within(recent).findByRole("link", { name: "Open Image in Library: A garden" });
    expect(image).toHaveClass("aspect-square", "rounded-media");
    expect(recent.querySelector("ul")!.className).toBe(stripClasses);
    expect(within(recent).queryByRole("status", { name: "Loading recent creations" })).toBeNull();
  });

  it("reserves a square media row when Recent settles empty", async () => {
    let finishMedia!: (value: Response) => void;
    mockCraftHome(new Promise<Response>((resolve) => { finishMedia = resolve; }));
    renderHome();
    const recent = await screen.findByRole("region", { name: "Recent" });
    await within(recent).findByRole("status", { name: "Loading recent creations" });
    const body = recent.querySelector('[data-slot="recent-body"]')!;
    const reserve = body.firstElementChild!;
    expect(body).toHaveClass("grid", "grid-cols-4", ...mediaGridGap.split(" "));
    expect(reserve).toHaveAttribute("aria-hidden", "true");
    expect(reserve).toHaveClass("invisible", "aspect-square", "col-start-1", "row-start-1");
    expect(body.lastElementChild).toHaveClass("col-span-4", "col-start-1", "row-start-1");

    finishMedia(Response.json({ assets: [] }));
    expect(await within(recent).findByText("Your creations will appear here.")).toBeVisible();
    expect(body.firstElementChild).toBe(reserve);
    expect(within(recent).queryByRole("status", { name: "Loading recent creations" })).toBeNull();
  });

  it("uses a reduced-motion-safe skeleton inside a pending video poster tile", async () => {
    let finishPoster!: (value: Response) => void;
    const posterResponse = new Promise<Response>((resolve) => { finishPoster = resolve; });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/me/api/home") return Response.json(home);
      if (path === "/me/api/media?limit=12") return Response.json({ assets: [{
        asset_id: "delayed-video",
        kind: "video",
        prompt: "A fox",
        download_url: "/fox.mp4",
        poster_asset_id: "delayed-poster",
      }] });
      if (path === "/me/api/media/delayed-poster/download?scope=me") return posterResponse;
      return Response.json({});
    }));
    renderHome();
    const pending = await screen.findByRole("status", { name: "Loading video poster" });
    expect(pending).toHaveAttribute("aria-busy", "true");
    expect(pending).toHaveClass("size-full");
    expect(pending.firstElementChild).toHaveClass("size-full", "rounded-none", "motion-reduce:animate-none");
    expect(pending.querySelector("svg")).toBeNull();
    const tile = screen.getByRole("link", { name: "Open Video in Library: A fox" });
    expect(tile).toHaveClass("aspect-square", "rounded-media");

    finishPoster(Response.json({ download_url: "/fox.jpg" }));
    expect(await screen.findByRole("img", { name: "A fox" })).toHaveAttribute("src", "/fox.jpg");
    expect(screen.getByRole("link", { name: "Open Video in Library: A fox" })).toBe(tile);
    expect(screen.queryByRole("status", { name: "Loading video poster" })).toBeNull();
  });

  it("stacks Recent and Make something with only the section gap", async () => {
    mockCraftHome();
    renderHome();
    const tasks = (
      await screen.findByRole("heading", { name: "Make something" })
    ).closest("section")!;
    const recent = (
      await screen.findByRole("heading", { name: "Recent" })
    ).closest("section")!;
    expect(recent.nextElementSibling).toBe(tasks);
    expect(tasks.parentElement).toHaveClass(
      "flex",
      "flex-col",
      "gap-(--space-section)",
    );
    expect(tasks.parentElement).not.toHaveClass("page-stack");
    expect(tasks).not.toHaveAttribute("style");
    expect(recent).not.toHaveAttribute("style");
  });

  it("spaces the Recent strip with the Library grid gutter", async () => {
    mockCraftHome();
    renderHome();
    const recent = (
      await screen.findByRole("heading", { name: "Recent" })
    ).closest("section")!;
    const strip = await waitFor(() => {
      const list = recent.querySelector("ul");
      expect(list).not.toBeNull();
      return list!;
    });
    expect(strip).toHaveClass(...mediaGridGap.split(" "));
    expect(mediaGridGap).toBe("gap-1 sm:gap-1.5");
  });

  it("uses one ListCard for favourite and other tasks with ink names and row tap targets", async () => {
    mockCraftHome();
    renderHome();
    const tasks = (
      await screen.findByRole("heading", { name: "Make something" })
    ).closest("section")!;
    expect(tasks.querySelectorAll('[data-slot="list-card"]')).toHaveLength(1);
    const list = within(tasks).getByRole("list", { name: "Make something" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows.length).toBeGreaterThanOrEqual(3);
    for (const row of rows) {
      expect(row).toHaveAttribute("data-slot", "list-row");
      expect(row).toHaveClass("min-h-(--row-1)", "max-sm:min-h-(--row-phone-1)");
      expect(row).not.toHaveClass("text-muted-foreground");
      expect(row.querySelector("p")).toHaveClass("text-body", "font-medium");
      expect(row.querySelector('[data-slot="list-row-leading"] svg')).toHaveClass(
        "text-foreground",
        "size-4",
      );
    }
    const music = within(tasks).getByRole("link", { name: "Make music" });
    expect(music).toHaveAttribute("href", "/try?capability=music&model=song");
    expect(music).toHaveClass("after:absolute", "after:inset-0");
    expect(within(music.closest("li")!).queryByText("Ready")).toBeNull();
    const sfx = within(tasks).getByRole("link", { name: "Make a sound effect" });
    expect(sfx).toHaveAttribute("href", "/try?capability=sfx&model=sfx");
    expect(within(sfx.closest("li")!).getByText("Busy")).toBeVisible();
    const video = within(tasks).getByText("Make a video").closest("li")!;
    expect(within(video).queryByRole("link")).toBeNull();
    expect(within(video).getByText("Down")).toBeVisible();
  });

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
        if (path === "/me/api/media?limit=12")
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
    // D1.5: the audio tile is a waveform and a play badge; its words are
    // the link's name.
    expect(
      await screen.findByRole("link", {
        name: "Open Audio in Library: gentle rain over a quiet porch",
      }),
    ).toBeInTheDocument();
    expect(screen.queryByText("gentle rain over a quiet porch")).toBeNull();
    const audio = document.querySelector("audio");
    expect(audio).toHaveAttribute("src", "https://media.example/audio.wav");
    expect(
      screen.getByRole("button", {
        name: "Play gentle rain over a quiet porch",
      }),
    ).toBeVisible();
    // Video tiles carry the prompt in their accessible name, not as text.
    expect(
      await screen.findByRole("link", {
        name: "Open Video in Library: a fox walking through snow",
      }),
    ).toBeVisible();
    expect(screen.queryByText("a fox walking through snow")).toBeNull();
    const poster = await waitFor(() => {
      const element = document.querySelector(
        'img[src="https://media.example/poster.jpg"]',
      );
      expect(element).not.toBeNull();
      return element;
    });
    expect(poster).toHaveAttribute("alt", "a fox walking through snow");
    const video = document.querySelector(
      'video[src="https://media.example/video-no-poster.mp4"]',
    );
    expect(video).toHaveAttribute("preload", "none");
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
        if (path === "/me/api/media?limit=12")
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
          availability: {
            evidence: { source: "status", observed_at_s: Date.now() / 1000 },
          },
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
          availability: {
            evidence: { source: "status", observed_at_s: Date.now() / 1000 },
          },
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const route = String(input);
        if (route === "/me/api/home") return Response.json(serverHome);
        if (route === "/me/api/media?limit=12")
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
    // Tasks are named for what the person does; they open Try in the right
    // mode with the server's recommended use name.
    const music = await screen.findByRole("link", { name: /Make music/ });
    const sfx = screen.getByRole("link", { name: /Make a sound effect/ });
    // Ready stays quiet on tasks; the verdict carries it.
    expect(within(music).queryByText("Ready")).toBeNull();
    expect(within(sfx).queryByText("Ready")).toBeNull();
    const header = screen
      .getByRole("heading", { level: 1, name: "Home" })
      .closest("header")!;
    expect(within(header).getByRole("status")).toHaveTextContent(
      "Everything works",
    );
    expect(screen.queryByText("Details")).toBeNull();
    expect(music).toHaveAttribute(
      "href",
      expect.stringMatching(/^\/try\/?\?capability=music&model=song$/),
    );
    expect(sfx).toHaveAttribute(
      "href",
      expect.stringMatching(/^\/try\/?\?capability=sfx&model=sfx$/),
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
        if (path === "/me/api/media?limit=12")
          return Response.json({ assets: [] });
        if (path === "/admin/api/setup-progress")
          return Response.json(progress());
        throw new Error(`Unexpected request: ${path}`);
      }),
    );
    renderHome();
    expect(
      await screen.findByRole("heading", { name: "Make something" }),
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
        if (path === "/me/api/media?limit=12")
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
      screen.getByLabelText("Paste your API Key"),
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
  it("does not infer missing chat access from a missing guide model", async () => {
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
        if (path === "/me/api/media?limit=12")
          return Response.json({ assets: [] });
        throw new Error(`Unexpected request: ${path}`);
      }),
    );
    renderHome();
    await userEvent.click(
      await screen.findByRole("button", { name: "Open WebUI" }),
    );
    await screen.findByText(
      "This key has no chat models. Choose another key or ask the owner to update its access.",
    );
    expect(screen.queryByRole("textbox", { name: "Model" })).toBeNull();
    expect(
      screen.queryByText("No chat model is available to this key yet"),
    ).toBeNull();
  });

  it.each([
    ["Python", "Install the openai package: pip install openai.", "https://github.com/openai/openai-python#usage"],
    ["JavaScript", "Install the openai package: npm install openai.", "https://github.com/openai/openai-node#usage"],
    ["curl", "Set GATEWAY_API_KEY to your key, then run this. It sends one chat request.", "https://platform.openai.com/docs/api-reference/chat/create"],
  ])("makes %s reachable under Another app with Python as default", async (client, firstStep, docs) => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path === "/.well-known/azizam")
          return Response.json({ service: { name: "Family gateway" } });
        if (path === "/me/api/home") return Response.json(home);
        if (path === "/me/api/media?limit=12")
          return Response.json({ assets: [] });
        if (path === "/admin/api/setup-progress")
          return Response.json({ ...progress(), complete: true });
        throw new Error(`Unexpected request: ${path}`);
      }),
    );
    renderHome();
    await userEvent.click(await screen.findByRole("button", { name: "Another app" }));
    expect(screen.getByRole("combobox", { name: "Language or tool" })).toHaveTextContent("Python");
    expect(screen.getByRole("button", { name: "Create key for Python" })).toBeVisible();
    await userEvent.click(screen.getByRole("combobox", { name: "Language or tool" }));
    await userEvent.click(await screen.findByRole("option", { name: client }));
    expect(screen.getByRole("combobox", { name: "Language or tool" })).toHaveTextContent(client);
    expect(screen.getByRole("button", { name: `Create key for ${client}` })).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Setup notes" }));
    const notes = await screen.findByRole("dialog", { name: "Setup notes" });
    expect(within(notes).getByText(firstStep)).toBeVisible();
    // The sheet shows the guide's steps and a snippet; never a key.
    expect(within(notes).getAllByRole("listitem").length).toBeGreaterThan(0);
    expect(notes.textContent).not.toMatch(/opk_|oak_/);
    expect(within(notes).getByRole("link", { name: `${client} docs` })).toHaveAttribute("href", docs);
    await userEvent.keyboard("{Escape}");
    await userEvent.click(screen.getByRole("button", { name: "Open WebUI" }));
    expect(screen.queryByRole("combobox", { name: "Language or tool" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Another app" }));
    expect(screen.getByRole("combobox", { name: "Language or tool" })).toHaveTextContent("Python");
  });

  it("offers OpenWhispr in the chooser, not Phone, and names its two models", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path === "/.well-known/azizam")
          return Response.json({ service: { name: "Family gateway" } });
        if (path === "/me/api/home")
          return Response.json({
            ...home,
            new_app_aliases: [
              ...home.new_app_aliases,
              {
                id: "house-whisper",
                operation: "audio.transcriptions",
                available: true,
                status: "ready",
                reason_code: "ready",
              },
            ],
          });
        if (path === "/me/api/media?limit=12")
          return Response.json({ assets: [] });
        if (path === "/admin/api/setup-progress")
          return Response.json({ ...progress(), complete: true });
        throw new Error(`Unexpected request: ${path}`);
      }),
    );
    renderHome();
    await userEvent.click(await screen.findByRole("button", { name: "OpenWhispr" }));
    expect(screen.queryByRole("button", { name: "Phone" })).toBeNull();
    expect(screen.queryByRole("combobox", { name: "Language or tool" })).toBeNull();
    expect(screen.getByRole("button", { name: "Create key for OpenWhispr" })).toBeVisible();
    expect(screen.getByRole("textbox", { name: "Speech-to-Text model" })).toHaveValue("house-whisper");
    expect(screen.getByRole("textbox", { name: "Dictation Cleanup model" })).toHaveValue("default");
    expect(screen.queryByRole("textbox", { name: "Model" })).toBeNull();
    // The key field of OpenWhispr is labelled as the app labels it.
    await userEvent.click(screen.getByRole("combobox", { name: "API key" }));
    await userEvent.click(await screen.findByRole("option", { name: /Browser test app/ }));
    expect(screen.getByLabelText("Paste your API Key (Optional)")).toBeVisible();
  });
});
