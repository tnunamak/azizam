import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import HomePage from "@/app/_home/home-page";
import { ViewerContext } from "@/components/shell/session";

const { push } = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn(), refresh: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams(),
}));

/** A capability row as the gateway sends it, with only the fields Home reads set. */
function capability(kind: string, overrides: Record<string, unknown> = {}) {
  return {
    capability: kind,
    icon_key: kind,
    capability_title: { image: "Images", chat: "Chat", video: "Video", speech: "Speech" }[kind] ?? kind,
    title: overrides.title ?? `${kind} model`,
    use_name: overrides.use_name ?? `${kind}-model`,
    use_names: overrides.use_names ?? [`${kind}-model`],
    recommended_model: overrides.use_name ?? `${kind}-model`,
    summary: `Makes ${kind} things.`,
    intent: `${kind}.generate`,
    input_modalities: [],
    output_modalities: [],
    execution: "sync",
    streaming: false,
    exposure: "default",
    trust_tier: "stable",
    resource_tier: "interactive",
    tags: [],
    available: true,
    caveats: [],
    availability: { evidence: { source: "request", observed_at_s: Math.floor(Date.now() / 1000) } },
    status: "ready",
    reason_code: "ready",
    typical_seconds: 9,
    cost_hint: { kind: "free", price_usd: null },
    ...overrides,
  };
}

function homePayload(overrides: Record<string, unknown> = {}) {
  return {
    authenticated: true,
    user: { id: "robin", name: "Robin Okafor", email: null, role: "member" },
    apps: [],
    gateway: { owner_name: "Sam" },
    has_access_plan: true,
    public_base_url: "https://gateway.example",
    api_base_url: "https://gateway.example/v1",
    csrf_token: "csrf",
    plan_spend: null,
    capabilities: [capability("image"), capability("chat"), capability("video", { status: "unavailable", available: false, reason_code: "unavailable_disabled" })],
    status: { state: "ready", reason_code: "ready", affected_capability_ids: [], counts: {} },
    receipts: [],
    recent_chats: [],
    ...overrides,
  };
}

type Routes = Record<string, () => unknown>;

/** Stubs fetch by path. A route that is not listed answers as an empty success, so a test names only what it needs. */
function stubFetch(routes: Routes = {}) {
  const calls: { path: string; method: string }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      const method = init?.method ?? "GET";
      calls.push({ path, method });
      const key = Object.keys(routes).find((candidate) => path === candidate || path.startsWith(`${candidate}?`) || path.startsWith(`${candidate}/`));
      const body = key ? routes[key]() : {};
      if (body instanceof Promise) return body;
      return Response.json(body, { status: method === "POST" ? 201 : 200 });
    }),
  );
  return calls;
}

function renderHome(viewer: { name?: string; role?: string; admin?: boolean } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ViewerContext.Provider
        value={{
          authenticated: true,
          name: viewer.name ?? "Robin Okafor",
          email: null,
          role: viewer.role ?? "member",
          admin: viewer.admin ?? false,
        } as never}
      >
        <HomePage />
      </ViewerContext.Provider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  push.mockReset();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const promptBox = () => screen.findByRole("textbox", { name: "Describe the picture you want" });

describe("H1 typing never waits for the capability list", () => {
  it("takes text before /me/api/home resolves, and Make is findable but does nothing yet", async () => {
    stubFetch({ "/me/api/home": () => new Promise(() => undefined) });
    renderHome();
    const box = await screen.findByRole("textbox", { name: "Describe what you want" });
    fireEvent.change(box, { target: { value: "A fox in snow" } });
    expect((box as HTMLTextAreaElement).value).toBe("A fox in snow");
    const make = screen.getByRole("button", { name: "Make" });
    // aria-disabled, never a faded native disabled: the solid control stays findable and focusable.
    expect(make).toHaveAttribute("aria-disabled", "true");
    expect(make).not.toBeDisabled();
  });
});

describe("H2 the chooser is the capability rows", () => {
  it("offers one option per row when there are two", async () => {
    stubFetch({ "/me/api/home": () => homePayload({ capabilities: [capability("image"), capability("chat")] }), "/me/api/media": () => ({ assets: [] }) });
    renderHome();
    const group = await screen.findByRole("group", { name: "What to make" });
    expect(within(group).getAllByRole("button")).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "More" })).toBeNull();
  });

  it("shows five options and a More menu for nine rows", async () => {
    const kinds = ["image", "chat", "video", "speech", "music", "sfx", "transcribe", "rerank2", "audio"];
    stubFetch({ "/me/api/home": () => homePayload({ capabilities: kinds.map((kind) => capability(kind)) }), "/me/api/media": () => ({ assets: [] }) });
    renderHome();
    const group = await screen.findByRole("group", { name: "What to make" });
    expect(within(group).getAllByRole("button")).toHaveLength(5);
    expect(screen.getByRole("button", { name: "More" })).toBeEnabled();
  });

  it("keeps task names out of the Home code (no hard-coded task list)", () => {
    const dir = join(__dirname);
    const sources = readdirSync(dir)
      .filter((name) => /\.(ts|tsx)$/.test(name) && !/\.spec\./.test(name))
      .map((name) => readFileSync(join(dir, name), "utf8"));
    for (const source of sources) expect(source).not.toMatch(/\bTASKS\b/);
  });
});

describe("H3 a capability that is down explains itself where the choice is made", () => {
  it("names the owner for a member and turns Make off", async () => {
    stubFetch({
      "/me/api/home": () =>
        homePayload({
          capabilities: [capability("video", { status: "unavailable", available: false, reason_code: "unavailable" }), capability("image")],
        }),
      "/me/api/media": () => ({ assets: [] }),
    });
    renderHome();
    const group = await screen.findByRole("group", { name: "What to make" });
    const video = within(group).getByRole("button", { name: /Video/ });
    expect(within(video).getByText("Down")).toBeVisible();
    fireEvent.click(video);
    expect(await screen.findByText(/Video is down\. Ask Sam to look at it\./)).toBeVisible();
    fireEvent.change(await screen.findByRole("textbox", { name: "Describe the clip you want" }), { target: { value: "a clip" } });
    expect(screen.getByRole("button", { name: "Make" })).toHaveAttribute("aria-disabled", "true");
  });
});

describe("H2 the chooser offers only what the person can use", () => {
  it("leaves out a capability that is off, and gives a working option no state word", async () => {
    stubFetch({ "/me/api/home": () => homePayload(), "/me/api/media": () => ({ assets: [] }) });
    renderHome();
    const group = await screen.findByRole("group", { name: "What to make" });
    expect(within(group).queryByRole("button", { name: /Video/ })).toBeNull();
    expect(within(group).getByRole("button", { name: /Chat/ })).toHaveTextContent(/^Chat$/);
  });
});

describe("H4 the facts line says what a run costs and how long it takes", () => {
  it("reads a local model as no charge with its time", async () => {
    stubFetch({ "/me/api/home": () => homePayload({ capabilities: [capability("image", { typical_seconds: 9 })] }), "/me/api/media": () => ({ assets: [] }) });
    renderHome();
    expect(await screen.findByText("No charge · about 9 s")).toBeVisible();
  });

  it("shows a paid model with an amount only when the data gives one", async () => {
    stubFetch({
      "/me/api/home": () =>
        homePayload({
          capabilities: [capability("image", { typical_seconds: 20, cost_hint: { kind: "paid", price_usd: 0.04 } })],
        }),
      "/me/api/media": () => ({ assets: [] }),
    });
    renderHome();
    expect(await screen.findByText("Paid model · about USD 0.04 · about 20 s")).toBeVisible();
  });

  it("omits a missing wait and never says unknown", async () => {
    stubFetch({
      "/me/api/home": () =>
        homePayload({ capabilities: [capability("image", { typical_seconds: null, cost_hint: { kind: "unknown", price_usd: null } })] }),
      "/me/api/media": () => ({ assets: [] }),
    });
    renderHome();
    await screen.findByRole("button", { name: "Make" });
    expect(screen.queryByText(/unknown/i)).toBeNull();
  });
});

describe("H5 Make hands the work to Try without the prompt in the address", () => {
  it("navigates to Try with the kind and model, and keeps the words in sessionStorage", async () => {
    stubFetch({ "/me/api/home": () => homePayload({ capabilities: [capability("image", { use_name: "krea-2-turbo" })] }), "/me/api/media": () => ({ assets: [] }) });
    renderHome();
    fireEvent.change(await promptBox(), { target: { value: "A red fox in snow" } });
    const historyBefore = window.history.length;
    fireEvent.click(screen.getByRole("button", { name: "Make" }));
    expect(push).toHaveBeenCalledWith("/try/?capability=image&model=krea-2-turbo");
    expect(String(push.mock.calls[0][0])).not.toContain("fox");
    expect(window.location.href).not.toContain("fox");
    expect(window.history.length).toBe(historyBefore);
    const stored = JSON.parse(window.sessionStorage.getItem("azizam.home.handoff") ?? "{}");
    expect(stored).toMatchObject({ capability: "image", model: "krea-2-turbo", prompt: "A red fox in snow" });
  });

  it("sends a chat message to Try's chat tab", async () => {
    stubFetch({ "/me/api/home": () => homePayload({ capabilities: [capability("chat", { use_name: "fast" })] }), "/me/api/media": () => ({ assets: [] }) });
    renderHome();
    fireEvent.change(await screen.findByRole("textbox", { name: "Ask anything" }), { target: { value: "Plan a trip" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(push).toHaveBeenCalledWith("/try/?capability=chat&model=fast");
  });
});

describe("H6 Recent is the person's own work, and a make in progress comes first", () => {
  it("puts a running make first with its time left, then results with links to Library", async () => {
    const now = Date.now() / 1000;
    stubFetch({
      "/me/api/home": () =>
        homePayload({
          receipts: [{ request_id: "job-1", intent: "image.generate", path: "/gateway/v1/media/jobs/image-generations", status: "running", accepted_at_ms: (now - 3) * 1000 }],
        }),
      "/me/api/media": () => ({ assets: [{ asset_id: "asset-1", kind: "image", prompt: "Garden", download_url: "/x.png", created_at: now - 3600 }] }),
    });
    renderHome();
    const list = await screen.findByRole("list", { name: "Recent work" });
    const tiles = within(list).getAllByRole("listitem");
    expect(tiles[0]).toHaveAttribute("data-making", "true");
    expect(within(tiles[0]).getByText(/about \d+ s left/i)).toBeVisible();
    const picture = await screen.findByRole("link", { name: /Garden/ });
    expect(picture.getAttribute("href")).toMatch(/^\/library\/?\?asset=asset-1$/);
  });

  it("reads Taking longer than usual once the typical time has passed", async () => {
    const now = Date.now() / 1000;
    stubFetch({
      "/me/api/home": () =>
        homePayload({
          receipts: [{ request_id: "job-2", intent: "image.generate", path: "/gateway/v1/media/jobs/image-generations", status: "running", accepted_at_ms: (now - 120) * 1000 }],
        }),
      "/me/api/media": () => ({ assets: [] }),
    });
    renderHome();
    expect(await screen.findByText("Taking longer than usual")).toBeVisible();
  });

  it("opens a conversation's receipt, and lists it only when history kept it", async () => {
    stubFetch({
      "/me/api/home": () =>
        homePayload({ recent_chats: [{ request_id: "req-9", record_id: "rec-9", created_at: Date.now() / 1000 - 7200, title: "Plan a two-day coastal trip" }] }),
      "/me/api/media": () => ({ assets: [] }),
    });
    renderHome();
    const link = await screen.findByRole("link", { name: /Plan a two-day coastal trip/ });
    // Next's link keeps or drops a trailing slash by config; the receipt is what matters.
    expect(link.getAttribute("href")).toMatch(/^\/usage\/?\?receipt=req-9$/);
  });
});

describe("H7 a failed Recent leaves the composer working", () => {
  it("keeps Make usable, and Try again refetches only media", async () => {
    let mediaCalls = 0;
    const calls = stubFetch({
      "/me/api/home": () => homePayload(),
      "/me/api/media": () => {
        mediaCalls += 1;
        if (mediaCalls === 1) throw new Error("boom");
        return { assets: [] };
      },
    });
    renderHome();
    fireEvent.change(await promptBox(), { target: { value: "A lake" } });
    expect(await screen.findByText("Couldn’t load your recent work.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Make" })).toBeEnabled();
    const homeCallsBefore = calls.filter((call) => call.path === "/me/api/home").length;
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(mediaCalls).toBe(2));
    expect(calls.filter((call) => call.path === "/me/api/home").length).toBe(homeCallsBefore);
  });
});

describe("H8 with no work, Home offers what can be made", () => {
  it("shows What you can make here and no Recent, and its cards are the chooser: a card chooses its kind and focuses the box", async () => {
    stubFetch({ "/me/api/home": () => homePayload(), "/me/api/media": () => ({ assets: [] }) });
    renderHome();
    expect(await screen.findByRole("heading", { name: "What you can make here" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Recent" })).toBeNull();
    expect(screen.getByRole("heading", { name: "Welcome, Robin" })).toBeVisible();
    const list = within(screen.getByRole("region", { name: "What you can make here" }));
    // One chooser on the page: the cards, not the cards and the composer's chips.
    expect(screen.getAllByRole("group", { name: "What to make" })).toHaveLength(1);
    const group = list.getByRole("group", { name: "What to make" });
    fireEvent.click(within(group).getByRole("button", { name: /^Chat/ }));
    expect(within(group).getByRole("button", { name: /^Chat/ })).toHaveAttribute("aria-pressed", "true");
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("textbox", { name: "Ask anything" })));
  });
});

describe("H8 a first visit's audio cards", () => {
  it("give speech and music a sample to play from the face, and a kind with no sample no play button", async () => {
    const audio = { output_modalities: ["audio"] };
    stubFetch({
      "/me/api/home": () =>
        homePayload({ capabilities: [capability("chat"), capability("speech", audio), capability("music", audio), capability("sfx", audio)] }),
      "/me/api/media": () => ({ assets: [] }),
    });
    renderHome();
    const group = within(await screen.findByRole("region", { name: "What you can make here" }));
    expect(group.getByRole("button", { name: "Play a Voice sample" })).toBeVisible();
    expect(group.getByRole("button", { name: "Play a Music sample" })).toBeVisible();
    expect(group.getAllByRole("button", { name: /^Play a / })).toHaveLength(2);
  });

  it("with one kind to make, shows no cards: the composer keeps its single chip", async () => {
    stubFetch({ "/me/api/home": () => homePayload({ capabilities: [capability("chat")] }), "/me/api/media": () => ({ assets: [] }) });
    renderHome();
    expect(await screen.findByRole("heading", { name: "Welcome, Robin" })).toBeVisible();
    await waitFor(() => expect(screen.getAllByRole("group", { name: "What to make" })).toHaveLength(1));
    expect(screen.queryByRole("region", { name: "What you can make here" })).toBeNull();
  });
});

describe("H9 the owner panel holds operations, never content", () => {
  it("shows the engine that is down with one action, and no prompts or titles", async () => {
    stubFetch({
      "/me/api/home": () => homePayload({ user: { id: "sam", name: "Sam Okafor", email: null, role: "owner" }, receipts: [{ request_id: "r", intent: "chat.completions", path: "/v1/chat/completions", status: "succeeded", accepted_at_ms: Date.now() }] }),
      "/me/api/media": () => ({ assets: [{ asset_id: "a", kind: "image", prompt: "Secret family photo", download_url: "/x.png" }] }),
      "/admin/api/status": () => ({
        status: {
          current_health: {
            observation_available: true,
            services: [{ service_id: "video-studio", configured: true, enabled: true, in_use: true, observation: "down", capability_title: "Video", capability_labels: ["Video"], last_error: null }],
          },
          backends: [],
        },
      }),
      "/admin/api/operator/service-registry": () => ({ services: { "video-studio": { id: "video-studio", source: "local", enabled: true, config: {} } } }),
      "/admin/api/providers": () => [],
      "/admin/api/activity/requests": () => ({ requests: [] }),
    });
    renderHome({ role: "owner", admin: true, name: "Sam Okafor" });
    const panel = await screen.findByRole("region", { name: "Your gateway" });
    // One fault is one row, and the whole row is the one link to where it is fixed.
    const engine = await within(panel).findByRole("link", { name: "Engine can’t take requests right now. Open Engines" });
    expect(engine.getAttribute("href")).toMatch(/^\/engines\/?$/);
    const rows = within(panel).getAllByRole("listitem");
    for (const row of rows) expect(within(row).getAllByRole("link")).toHaveLength(1);
    expect(panel.textContent).not.toContain("Secret family photo");
  });

  it("names what is off in one row, and never tells the owner what the owner did", async () => {
    stubFetch({
      "/me/api/home": () => homePayload({ user: { id: "sam", name: "Sam Okafor", email: null, role: "owner" } }),
      "/me/api/media": () => ({ assets: [] }),
      "/admin/api/status": () => ({ status: { current_health: { observation_available: true, services: [] }, backends: [] } }),
      "/admin/api/operator/service-registry": () => ({ services: { "local-chat": { id: "local-chat", source: "local", enabled: true, config: {} } } }),
      "/admin/api/providers": () => [],
      "/admin/api/activity/requests": () => ({ requests: [] }),
    });
    renderHome({ role: "owner", admin: true, name: "Sam Okafor" });
    const panel = await screen.findByRole("region", { name: "Your gateway" });
    await waitFor(() => expect(within(panel).getAllByText("Video is off")[0]).toBeVisible());
    expect(within(panel).getAllByRole("link", { name: /Open Engines/ })).toHaveLength(1);
    expect(panel.textContent).not.toMatch(/Turned off by the owner|Not set up yet|Everything is working/);
  });

  it("says what works in one line, with the Status link, when nothing needs the owner", async () => {
    stubFetch({
      "/me/api/home": () =>
        homePayload({
          user: { id: "sam", name: "Sam Okafor", email: null, role: "owner" },
          capabilities: [capability("image"), capability("chat")],
        }),
      "/me/api/media": () => ({ assets: [] }),
      "/admin/api/status": () => ({ status: { current_health: { observation_available: true, services: [] }, backends: [] } }),
      "/admin/api/operator/service-registry": () => ({ services: { "gateway-core": { id: "gateway-core", source: "builtin", enabled: true, config: {} }, "local-chat": { id: "local-chat", source: "local", enabled: true, config: {} } } }),
      "/admin/api/providers": () => [],
      "/admin/api/activity/requests": () => ({ requests: [] }),
    });
    renderHome({ role: "owner", admin: true, name: "Sam Okafor" });
    const panel = await screen.findByRole("region", { name: "Your gateway" });
    // The phone row says it in two words; the wide panel names what works.
    await waitFor(() => expect(within(panel).getByText("Everything is working")).toBeVisible());
    expect(within(panel).getByText(/^(Images and Chat|Chat and Images) are working$/)).toBeVisible();
    expect(within(panel).getAllByRole("link", { name: /Open Status/ })[0].getAttribute("href")).toMatch(/^\/status\/?$/);
    expect(within(panel).queryAllByRole("link", { name: /Open Engines/ })).toHaveLength(0);
  });
});

describe("H10 a member never sees the gateway panel", () => {
  it("has no Your gateway region", async () => {
    stubFetch({ "/me/api/home": () => homePayload(), "/me/api/media": () => ({ assets: [] }) });
    renderHome();
    await screen.findByRole("button", { name: "Make" });
    expect(screen.queryByRole("region", { name: "Your gateway" })).toBeNull();
  });
});

describe("H11 without an access plan, nothing can run and the member can ask", () => {
  it("removes the composer, and Ask Sam posts once and confirms in place", async () => {
    const calls = stubFetch({ "/me/api/home": () => homePayload({ has_access_plan: false }), "/me/api/media": () => ({ assets: [] }) });
    renderHome();
    expect(await screen.findByText("You can’t make anything yet.")).toBeVisible();
    expect(screen.queryByRole("textbox")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Ask Sam" }));
    expect(await screen.findByText("Sent to Sam.")).toBeVisible();
    expect(calls.filter((call) => call.path === "/me/api/access-request" && call.method === "POST")).toHaveLength(1);
  });
});

describe("H15 copy does not repeat itself", () => {
  it("names each section once and offers Connect an app beside the apps", async () => {
    stubFetch({ "/me/api/home": () => homePayload(), "/me/api/media": () => ({ assets: [] }) });
    renderHome();
    expect(await screen.findByRole("heading", { name: "What you can make here" })).toBeVisible();
    expect(screen.getAllByRole("heading", { name: "What you can make here" })).toHaveLength(1);
    expect(screen.getByRole("link", { name: /Connect an app/ }).getAttribute("href")).toMatch(/^\/connect\/?$/);
  });
});

describe("H13 no model or engine names in the Home code", () => {
  it("keeps model and engine product names out of the Home source", () => {
    const dir = join(__dirname);
    const files = readdirSync(dir).filter((name) => /\.(ts|tsx)$/.test(name) && !/\.spec\./.test(name));
    const pattern = /\b(krea|ltx|qwen|gpt-image|sora|llama|vllm|ollama|whisper|minimax|kokoro|flux)\b/i;
    for (const name of files) expect(readFileSync(join(dir, name), "utf8")).not.toMatch(pattern);
  });
});
