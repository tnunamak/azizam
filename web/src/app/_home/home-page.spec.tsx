import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import HomePage from "@/app/_home/home-page";
import { takeHomeHandoff } from "@/app/_home/handoff";
import { ViewerContext } from "@/components/shell/session";
import { PICTURE_ITEM, RECORDING_ITEM, stubbedLibrary, type StubbedItem } from "../../../tests/helpers/library-stub";

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
    capability_title: {
      image: "Images",
      chat: "Chat",
      video: "Video",
      speech: "Voice",
      music: "Music",
      sfx: "Sound effects",
    }[kind] ?? kind,
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
    action_label: "Make",
    // What the bundled manifest for this kind declares; the gateway sends the hint and the words on every row.
    prompt_hint: { image: "Describe the picture you want", video: "Describe the clip you want", chat: "Ask anything" }[kind] ?? null,
    input_words: Object.fromEntries(
      ((overrides.input_modalities as string[] | undefined) ?? [])
        .filter((modality) => modality !== "text")
        .map((modality) => [modality, { image: "picture", audio: "recording", video: "clip" }[modality] ?? modality]),
    ),
    recordable_input_modalities: [],
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
    stubFetch({ "/me/api/home": () => homePayload({ capabilities: [capability("chat", { use_name: "fast", action_label: "Send" })] }), "/me/api/media": () => ({ assets: [] }) });
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
    // The phone row says it in two words; the wide panel counts what works against what is on.
    await waitFor(() => expect(within(panel).getByText("Everything is working")).toBeVisible());
    expect(within(panel).getByText("2 of 2 working")).toBeVisible();
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

describe("H8 Enter makes", () => {
  const imageRows = () => homePayload({ capabilities: [capability("image", { use_name: "krea-2-turbo", input_modalities: ["text"], output_modalities: ["image"] })] });

  it("Enter makes; Shift+Enter starts a new line; Ctrl+Enter still makes", async () => {
    stubFetch({ "/me/api/home": imageRows, "/me/api/media": () => ({ assets: [] }) });
    renderHome();
    const box = await promptBox();
    fireEvent.change(box, { target: { value: "A red fox in snow" } });
    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    expect(push).not.toHaveBeenCalled();
    fireEvent.keyDown(box, { key: "Enter" });
    expect(push).toHaveBeenCalledTimes(1);
    expect(push).toHaveBeenLastCalledWith("/try/?capability=image&model=krea-2-turbo");
    fireEvent.keyDown(box, { key: "Enter", ctrlKey: true });
    expect(push).toHaveBeenCalledTimes(2);
  });

  it("does nothing on Enter while an IME is composing, or on the Enter that confirms its candidate (Safari)", async () => {
    stubFetch({ "/me/api/home": imageRows, "/me/api/media": () => ({ assets: [] }) });
    renderHome();
    const box = await promptBox();
    fireEvent.change(box, { target: { value: "A red fox in snow" } });
    fireEvent.keyDown(box, { key: "Enter", isComposing: true });
    expect(push).not.toHaveBeenCalled();
    fireEvent.keyDown(box, { key: "Enter", isComposing: false, keyCode: 229 });
    expect(push).not.toHaveBeenCalled();
  });
});

describe("the composer follows the chosen capability's declared inputs", () => {
  const picture = () => new File([new Uint8Array(2048)], "fox.png", { type: "image/png" });
  const textRow = capability("image", { use_name: "krea-2-turbo", input_modalities: ["text"], output_modalities: ["image"] });
  // Image only: the data declares no text input, so no text box is offered.
  const meshRow = capability("mesh", {
    capability_title: "3D models",
    use_name: "mesh.generate.trellis-2",
    title: "TRELLIS.2",
    input_modalities: ["image"],
    required_input_modalities: ["image"],
    output_modalities: ["mesh"],
  });
  // Image plus text, both required (a picture to change and words to change it by).
  const editRow = capability("edit", {
    capability_title: "Edit",
    use_name: "edit-model",
    input_modalities: ["text", "image"],
    required_input_modalities: ["text", "image"],
    output_modalities: ["image"],
  });
  // Image required, text optional.
  const noteRow = capability("restyle", {
    capability_title: "Restyle",
    use_name: "restyle-model",
    input_modalities: ["text", "image"],
    required_input_modalities: ["image"],
    output_modalities: ["image"],
  });
  const audioRow = capability("transcribe", {
    capability_title: "Transcription",
    use_name: "ear-model",
    input_modalities: ["audio"],
    required_input_modalities: ["audio"],
    output_modalities: ["text"],
    action_label: "Transcribe",
    recordable_input_modalities: ["audio"],
  });
  // An audio input that does not declare it can be recorded (a sample to analyse, say).
  const sampleRow = capability("analyse", {
    capability_title: "Analyse",
    use_name: "sample-model",
    input_modalities: ["audio"],
    required_input_modalities: ["audio"],
    output_modalities: ["text"],
  });
  // A kind that no code names: the data alone gives it a file input (no accept filter) and no text box.
  const smellRow = capability("smell", {
    capability_title: "Smell",
    use_name: "nose-model",
    input_modalities: ["chemical"],
    required_input_modalities: ["chemical"],
    output_modalities: ["smell"],
  });

  async function chooseKind(rows: unknown[], label: string) {
    stubFetch({ "/me/api/home": () => homePayload({ capabilities: rows }), "/me/api/media": () => ({ assets: [] }) });
    renderHome();
    const group = await screen.findByRole("group", { name: "What to make" });
    fireEvent.click(within(group).getByRole("button", { name: label }));
  }
  const fileInput = (name: RegExp) => screen.getByLabelText(name) as HTMLInputElement;
  const makeButton = () => screen.getByRole("button", { name: "Make" });

  it("text only: a text box and no file input", async () => {
    await chooseKind([textRow, meshRow], "Images");
    expect(await screen.findByRole("textbox", { name: "Describe the picture you want" })).toBeVisible();
    expect(document.querySelector('[data-slot="home-media-input"]')).toBeNull();
  });

  it("image only: a picture input with the image accept filter, no text box, and Make waits for a picture", async () => {
    await chooseKind([textRow, meshRow], "3D models");
    const input = fileInput(/Choose a picture/);
    expect(input.getAttribute("accept")).toBe("image/*");
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByText("3D models start from a picture.")).toBeNull();
    expect(makeButton().getAttribute("aria-disabled")).toBe("true");
    // It looks unavailable, and says what it waits for beside it and on hover.
    expect(makeButton().className).toContain("text-muted-foreground");
    expect(makeButton().className).not.toContain("bg-make-rest");
    expect(screen.getByText("Add a picture to start")).toBeVisible();
    expect(makeButton().getAttribute("title")).toBe("Add a picture to start");
    fireEvent.click(makeButton());
    expect(push).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { files: [picture()] } });
    expect(await screen.findByText("fox.png")).toBeVisible();
    expect(screen.queryByText("Add a picture to start")).toBeNull();
    expect(makeButton().getAttribute("title")).toBeNull();
    expect(makeButton().getAttribute("aria-disabled")).toBe("false");
    fireEvent.click(makeButton());
    expect(push).toHaveBeenLastCalledWith("/try/?capability=mesh&model=mesh.generate.trellis-2");
    const handoff = takeHomeHandoff("mesh");
    expect(handoff?.files?.map((file) => file.name)).toEqual(["fox.png"]);
  });

  it("image plus text, both required: Make waits for the picture and the words", async () => {
    await chooseKind([textRow, editRow], "Edit");
    expect(fileInput(/Choose a picture/)).toBeTruthy();
    const box = await screen.findByRole("textbox", { name: "Describe what you want" });
    fireEvent.change(box, { target: { value: "make it night" } });
    expect(makeButton().getAttribute("aria-disabled")).toBe("true");
    fireEvent.change(fileInput(/Choose a picture/), { target: { files: [picture()] } });
    await screen.findByText("fox.png");
    expect(makeButton().getAttribute("aria-disabled")).toBe("false");
    fireEvent.click(makeButton());
    expect(push).toHaveBeenCalledTimes(1);
    const handoff = takeHomeHandoff("edit");
    expect(handoff).toMatchObject({ prompt: "make it night" });
    expect(handoff?.files).toHaveLength(1);
  });

  it("image required, text optional: the text box says so, and a picture alone is enough", async () => {
    await chooseKind([textRow, noteRow], "Restyle");
    expect(await screen.findByRole("textbox", { name: "Add a note (optional)" })).toBeVisible();
    fireEvent.change(fileInput(/Choose a picture/), { target: { files: [picture()] } });
    await screen.findByText("fox.png");
    expect(makeButton().getAttribute("aria-disabled")).toBe("false");
  });

  it("refuses a file of the wrong type and says what is wanted", async () => {
    await chooseKind([textRow, meshRow], "3D models");
    fireEvent.change(fileInput(/Choose a picture/), { target: { files: [new File(["x"], "notes.txt", { type: "text/plain" })] } });
    expect(await screen.findByRole("alert")).toHaveTextContent("Choose a picture");
    expect(makeButton().getAttribute("aria-disabled")).toBe("true");
  });

  it("takes a dropped picture and a pasted one", async () => {
    await chooseKind([textRow, meshRow], "3D models");
    const zone = document.querySelector('[data-slot="home-media-input"]') as HTMLElement;
    fireEvent.drop(zone, { dataTransfer: { files: [picture()] } });
    expect(await screen.findByText("fox.png")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Remove fox.png" }));
    expect(screen.queryByText("fox.png")).toBeNull();
    const composer = document.querySelector('[data-slot="composer"]') as HTMLElement;
    fireEvent.paste(composer, { clipboardData: { files: [picture()] } });
    expect(await screen.findByText("fox.png")).toBeVisible();
  });

  /** Routes the Library's list, download and signed-file requests to `items`; every other request goes where it went. */
  function withLibrary(items: StubbedItem[]) {
    const base = globalThis.fetch;
    const library = stubbedLibrary(items, (url, init) => base(url, init));
    vi.stubGlobal("fetch", vi.fn(library.impl));
    return library;
  }

  it("a picture input offers the Library; a pick becomes the file Make sends to Try", async () => {
    await chooseKind([textRow, meshRow], "3D models");
    const library = withLibrary([PICTURE_ITEM]);
    fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
    fireEvent.click(await screen.findByRole("button", { name: "A teal fish" }));
    expect(await screen.findByText("A teal fish", { selector: "[data-slot=picked-summary] span" })).toBeVisible();
    expect(screen.getByText(/From your Library/)).toBeVisible();
    expect(library.listed[0]).toContain("kind=image");
    await waitFor(() => expect(makeButton().getAttribute("aria-disabled")).toBe("false"));
    fireEvent.click(makeButton());
    const file = takeHomeHandoff("mesh")?.files?.[0];
    expect(file?.name).toBe("library-picture.png");
    expect(file?.type).toBe("image/png");
  });

  it("Make waits while a Library pick is still arriving", async () => {
    await chooseKind([textRow, meshRow], "3D models");
    let release!: () => void;
    const held = new Promise<void>((resolve) => (release = resolve));
    const library = stubbedLibrary([PICTURE_ITEM]);
    const base = library.impl;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).includes("/download")) await held;
        return base(input, init);
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
    fireEvent.click(await screen.findByRole("button", { name: "A teal fish" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Adding picture…");
    expect(makeButton().getAttribute("aria-disabled")).toBe("true");
    release();
    expect(await screen.findByText("A teal fish", { selector: "[data-slot=picked-summary] span" })).toBeVisible();
    await waitFor(() => expect(makeButton().getAttribute("aria-disabled")).toBe("false"));
  });

  it("a recording input lists the Library's audio, and a pick is the recording", async () => {
    await chooseKind([textRow, audioRow], "Transcription");
    const library = withLibrary([PICTURE_ITEM, RECORDING_ITEM]);
    fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
    expect(await screen.findByRole("heading", { name: "Choose a recording from your Library" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "A teal fish" })).toBeNull();
    fireEvent.click(await screen.findByRole("button", { name: "A short spoken note" }));
    expect(await screen.findByText("A short spoken note", { selector: "[data-slot=picked-summary] span" })).toBeVisible();
    expect(library.listed.every((url) => url.includes("kind=audio"))).toBe(true);
  });

  it("an input whose kind the Library cannot hold has no Library button", async () => {
    await chooseKind([textRow, smellRow], "Smell");
    fileInput(/Choose a chemical/);
    expect(screen.queryByRole("button", { name: "Choose from Library" })).toBeNull();
  });

  it("the attach button of a capability that takes words and a file offers file or Library", async () => {
    await chooseKind([textRow, editRow], "Edit");
    withLibrary([PICTURE_ITEM]);
    const attach = screen.getByRole("button", { name: "Attach a picture" });
    Object.defineProperty(HTMLElement.prototype, "hasPointerCapture", { configurable: true, value: () => false });
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: () => {} });
    attach.focus();
    fireEvent.keyDown(attach, { key: "Enter" });
    expect(await screen.findByRole("menuitem", { name: "Choose a picture" })).toBeVisible();
    fireEvent.click(await screen.findByRole("menuitem", { name: "Choose from Library" }));
    fireEvent.click(await screen.findByRole("button", { name: "A teal fish" }));
    // The chip names the picture by what the Library calls it; the file keeps its own name for the request.
    await waitFor(() => expect(document.querySelector('[data-slot="home-picked-file"]')).toHaveTextContent("A teal fish"));
    expect(document.querySelector('[data-slot="home-picked-file"]')).not.toHaveTextContent("library-picture.png");
    expect(screen.getByRole("button", { name: "Remove A teal fish" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Change picture" })).toBeVisible();
  });

  it("Make waits for a Library pick that replaces a file already chosen", async () => {
    await chooseKind([textRow, editRow], "Edit");
    fireEvent.change(await screen.findByRole("textbox", { name: "Describe what you want" }), { target: { value: "make it night" } });
    fireEvent.change(fileInput(/Choose a picture/), { target: { files: [picture()] } });
    await screen.findByText("fox.png");
    expect(makeButton().getAttribute("aria-disabled")).toBe("false");
    let release!: () => void;
    const held = new Promise<void>((resolve) => (release = resolve));
    const base = stubbedLibrary([PICTURE_ITEM]).impl;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).includes("/download")) await held;
        return base(input, init);
      }),
    );
    const change = screen.getByRole("button", { name: "Change picture" });
    Object.defineProperty(HTMLElement.prototype, "hasPointerCapture", { configurable: true, value: () => false });
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: () => {} });
    change.focus();
    fireEvent.keyDown(change, { key: "Enter" });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Choose from Library" }));
    fireEvent.click(await screen.findByRole("button", { name: "A teal fish" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Adding picture…");
    expect(makeButton().getAttribute("aria-disabled")).toBe("true");
    release();
    await waitFor(() => expect(document.querySelector('[data-slot="home-picked-file"]')).toHaveTextContent("A teal fish"));
    await waitFor(() => expect(makeButton().getAttribute("aria-disabled")).toBe("false"));
  });

  /** A Library whose download is held back until `release()`. */
  function heldLibrary() {
    let release!: () => void;
    const held = new Promise<void>((resolve) => (release = resolve));
    const base = stubbedLibrary([PICTURE_ITEM]).impl;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).includes("/download")) await held;
        return base(input, init);
      }),
    );
    return { release };
  }
  const letLateAnswerArrive = () => new Promise((resolve) => setTimeout(resolve, 30));

  it("a file of any size is taken: 30 MB of audio on Home, and 11 MB of a picture", async () => {
    await chooseKind([textRow, audioRow], "Transcription");
    const big = new File([new Uint8Array(8)], "talk.mp3", { type: "audio/mpeg" });
    Object.defineProperty(big, "size", { value: 30 * 1024 * 1024 });
    fireEvent.change(fileInput(/Choose a recording/), { target: { files: [big] } });
    expect(await screen.findByText("talk.mp3")).toBeVisible();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("button", { name: "Transcribe" }).getAttribute("aria-disabled")).toBe("false");
    cleanup();
    await chooseKind([textRow, meshRow], "3D models");
    const picture11 = picture();
    Object.defineProperty(picture11, "size", { value: 11 * 1024 * 1024 });
    fireEvent.change(fileInput(/Choose a picture/), { target: { files: [picture11] } });
    expect(await screen.findByText("fox.png")).toBeVisible();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a pasted picture ends a Library pick still arriving: the late pick does not replace it", async () => {
    await chooseKind([textRow, meshRow], "3D models");
    const library = heldLibrary();
    fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
    fireEvent.click(await screen.findByRole("button", { name: "A teal fish" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Adding picture…");
    const composer = document.querySelector('[data-slot="composer"]') as HTMLElement;
    fireEvent.paste(composer, { clipboardData: { files: [picture()] } });
    expect(await screen.findByText("fox.png")).toBeVisible();
    expect(screen.queryByRole("status")).toBeNull();
    library.release();
    await letLateAnswerArrive();
    expect(screen.getByText("fox.png")).toBeVisible();
    expect(screen.queryByText("A teal fish", { selector: "[data-slot=picked-summary] span" })).toBeNull();
    expect(screen.queryByText(/From your Library/)).toBeNull();
  });

  it("a pasted file of the wrong type is not taken", async () => {
    await chooseKind([textRow, meshRow], "3D models");
    const composer = document.querySelector('[data-slot="composer"]') as HTMLElement;
    fireEvent.paste(composer, { clipboardData: { files: [new File(["x"], "notes.txt", { type: "text/plain" })] } });
    // Not a picture: nothing is taken and the zone is unchanged.
    expect(screen.queryByText("notes.txt")).toBeNull();
    expect(document.querySelector('[data-slot="picked-summary"]')).toBeNull();
  });

  it("removing the chip ends a Library pick that is replacing it: the late pick does not come back", async () => {
    await chooseKind([textRow, editRow], "Edit");
    fireEvent.change(await screen.findByRole("textbox", { name: "Describe what you want" }), { target: { value: "make it night" } });
    fireEvent.change(fileInput(/Choose a picture/), { target: { files: [picture()] } });
    await screen.findByText("fox.png");
    const library = heldLibrary();
    const change = screen.getByRole("button", { name: "Change picture" });
    Object.defineProperty(HTMLElement.prototype, "hasPointerCapture", { configurable: true, value: () => false });
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: () => {} });
    change.focus();
    fireEvent.keyDown(change, { key: "Enter" });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Choose from Library" }));
    fireEvent.click(await screen.findByRole("button", { name: "A teal fish" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Adding picture…");
    fireEvent.click(screen.getByRole("button", { name: "Remove fox.png" }));
    expect(screen.queryByRole("status")).toBeNull();
    library.release();
    await letLateAnswerArrive();
    expect(document.querySelector('[data-slot="home-picked-file"]')).toBeNull();
    expect(makeButton().getAttribute("aria-disabled")).toBe("true");
  });

  it("focus stays with the person: Replace after a pick, the way to choose after Remove, never on arrival", async () => {
    await chooseKind([textRow, meshRow], "3D models");
    // Nothing takes focus because the page drew.
    expect(document.body).toBe(document.activeElement);
    withLibrary([PICTURE_ITEM]);
    fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
    fireEvent.click(await screen.findByRole("button", { name: "A teal fish" }));
    const replace = await screen.findByRole("button", { name: "Replace A teal fish" });
    await waitFor(() => expect(document.activeElement).toBe(replace));
    fireEvent.click(screen.getByRole("button", { name: "Remove A teal fish" }));
    const choose = await screen.findByRole("button", { name: "Choose a picture" });
    await waitFor(() => expect(document.activeElement).toBe(choose));
  });

  it("removing the chip puts focus on the attach button", async () => {
    await chooseKind([textRow, editRow], "Edit");
    fireEvent.change(fileInput(/Choose a picture/), { target: { files: [picture()] } });
    await screen.findByText("fox.png");
    fireEvent.click(screen.getByRole("button", { name: "Remove fox.png" }));
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Attach a picture" }));
  });

  it("removing a chip that came from the Library puts focus on the attach button, and the Library opens clean again", async () => {
    await chooseKind([textRow, editRow], "Edit");
    withLibrary([PICTURE_ITEM]);
    const attach = screen.getByRole("button", { name: "Attach a picture" });
    Object.defineProperty(HTMLElement.prototype, "hasPointerCapture", { configurable: true, value: () => false });
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: () => {} });
    attach.focus();
    fireEvent.keyDown(attach, { key: "Enter" });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Choose from Library" }));
    fireEvent.click(await screen.findByRole("button", { name: "A teal fish" }));
    await waitFor(() => expect(document.querySelector('[data-slot="home-picked-file"]')).toHaveTextContent("A teal fish"));
    fireEvent.click(screen.getByRole("button", { name: "Remove A teal fish" }));
    expect(document.querySelector('[data-slot="home-picked-file"]')).toBeNull();
    // The attach button is the way to choose again, and it holds focus (it is not left on the removed chip's place).
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Attach a picture" }));
    // Nothing of the removed pick is marked when the Library opens again.
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Enter" });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Choose from Library" }));
    await screen.findByRole("button", { name: "A teal fish" });
    expect(document.querySelectorAll('[aria-current="true"]')).toHaveLength(0);
  });

  it("the chip's Remove is a 40 px target (44 on a phone)", async () => {
    await chooseKind([textRow, editRow], "Edit");
    fireEvent.change(fileInput(/Choose a picture/), { target: { files: [picture()] } });
    await screen.findByText("fox.png");
    const remove = screen.getByRole("button", { name: "Remove fox.png" });
    expect(remove.className).toContain("size-10");
    expect(remove.className).toContain("max-sm:size-11");
    expect(remove.className).toContain("focus-visible:ring-2");
  });

  it("audio: a recording input, and a hand-off to record in Try that carries no file", async () => {
    await chooseKind([textRow, audioRow], "Transcription");
    expect(fileInput(/Choose a recording/).getAttribute("accept")).toBe("audio/*");
    expect(screen.queryByRole("textbox")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Or record in Try" }));
    expect(push).toHaveBeenLastCalledWith("/try/?capability=transcribe&model=ear-model");
    expect(takeHomeHandoff("transcribe")?.files).toBeUndefined();
  });

  it("the button's word comes from the capability's data", async () => {
    await chooseKind([textRow, audioRow], "Transcription");
    expect(screen.getByRole("button", { name: "Transcribe" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Make" })).toBeNull();
  });

  it("offers recording in Try only when the input declares it can be recorded", async () => {
    await chooseKind([textRow, sampleRow], "Analyse");
    expect(fileInput(/Choose a recording/).getAttribute("accept")).toBe("audio/*");
    expect(screen.queryByRole("button", { name: "Or record in Try" })).toBeNull();
  });

  it("a picture beside required words is a chip above the words with an attach button, not a large drop zone", async () => {
    await chooseKind([textRow, editRow], "Edit");
    expect(document.querySelector('[data-slot="home-media-input"]')).toBeNull();
    expect(document.querySelector('[data-slot="home-picked-file"]')).toBeNull();
    fireEvent.change(fileInput(/Choose a picture/), { target: { files: [picture()] } });
    const chip = await waitFor(() => {
      const found = document.querySelector('[data-slot="home-picked-file"]') as HTMLElement;
      expect(found).not.toBeNull();
      return found;
    });
    expect(chip).toHaveTextContent("fox.png");
    const box = screen.getByRole("textbox", { name: "Describe what you want" });
    // The chip sits above the words.
    expect(chip.compareDocumentPosition(box) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole("button", { name: "Change picture" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Remove fox.png" }));
    expect(document.querySelector('[data-slot="home-picked-file"]')).toBeNull();
  });

  it("refuses a file of the wrong kind from the attach button", async () => {
    await chooseKind([textRow, editRow], "Edit");
    fireEvent.change(fileInput(/Choose a picture/), { target: { files: [new File(["x"], "notes.txt", { type: "text/plain" })] } });
    expect(await screen.findByRole("alert")).toHaveTextContent("That file is not a picture");
  });

  it("a kind no code knows still gets a file input from its declared modality alone", async () => {
    await chooseKind([textRow, smellRow], "Smell");
    const input = fileInput(/Choose a chemical/);
    expect(input.getAttribute("accept")).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.getByText("Add a chemical to start")).toBeVisible();
  });

  it("offers Make a picture of this first only when the capability needs a picture and words are typed", async () => {
    await chooseKind([textRow, editRow], "Edit");
    expect(screen.queryByRole("button", { name: "Make a picture of this first" })).toBeNull();
    fireEvent.change(await screen.findByRole("textbox", { name: "Describe what you want" }), { target: { value: "a cute octopus eating ice cream" } });
    fireEvent.click(screen.getByRole("button", { name: "Make a picture of this first" }));
    expect(push).toHaveBeenLastCalledWith("/try/?capability=image&model=krea-2-turbo");
    const stored = JSON.parse(window.sessionStorage.getItem("azizam.home.handoff") ?? "{}");
    expect(stored).toMatchObject({ capability: "image", model: "krea-2-turbo", prompt: "a cute octopus eating ice cream" });
  });

  it("does not offer it once a picture is chosen", async () => {
    await chooseKind([textRow, editRow], "Edit");
    fireEvent.change(await screen.findByRole("textbox", { name: "Describe what you want" }), { target: { value: "night" } });
    fireEvent.change(fileInput(/Choose a picture/), { target: { files: [picture()] } });
    await screen.findByText("fox.png");
    expect(screen.queryByRole("button", { name: "Make a picture of this first" })).toBeNull();
  });
  const keyframeInputs = [
    { name: "image", label: "a starting picture", modality: "image", accepts: ["image/png", "image/jpeg"] },
    { name: "last_frame", label: "an ending picture", modality: "image", accepts: ["image/*"] },
  ];
  const mediaRow = (required_media_inputs = keyframeInputs) => capability("video", {
    input_modalities: ["image", "video"], required_input_modalities: ["image"],
    input_words: { image: "picture", video: "clip" }, required_media_inputs,
  });
  const composer = () => document.querySelector('[data-slot="composer"]') as HTMLElement;

  it("keyframes labels two compact slots, waits for each, and hands files to Try in slot order", async () => {
    await chooseKind([textRow, mediaRow()], "Video");
    const start = fileInput(/^Choose a starting picture$/);
    const end = fileInput(/^Choose an ending picture$/);
    expect(start.accept).toBe("image/png,image/jpeg");
    expect(end.accept).toBe("image/*");
    expect(start.compareDocumentPosition(end) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(document.querySelector('[data-slot="home-media-input"]')).toBeNull();
    expect(screen.getByRole("button", { name: "Attach a starting picture" })).toHaveTextContent("Attach a starting picture");
    expect(screen.getByRole("button", { name: "Attach an ending picture" })).toHaveTextContent("Attach an ending picture");
    expect(screen.getByText("Add a starting picture and an ending picture to start")).toBeVisible();
    expect(makeButton()).toHaveAttribute("title", "Add a starting picture and an ending picture to start");
    const ending = new File(["e"], "end.png", { type: "image/png" });
    fireEvent.change(end, { target: { files: [ending] } });
    await screen.findByText("end.png");
    expect(screen.getByText("Add a starting picture to start")).toBeVisible();
    const starting = picture();
    fireEvent.change(start, { target: { files: [starting] } });
    await screen.findByText("fox.png");
    expect(makeButton()).toHaveAttribute("aria-disabled", "false");
    fireEvent.click(makeButton());
    expect(takeHomeHandoff("video")).toMatchObject({
      files: [starting, ending], file_inputs: ["image", "last_frame"],
    });
  });

  it("drops into the first empty compatible slot, then pastes into the next without replacing the first", async () => {
    await chooseKind([textRow, mediaRow()], "Video");
    const starting = picture();
    const ending = new File(["e"], "end.png", { type: "image/png" });
    fireEvent.drop(composer(), { dataTransfer: { files: [starting] } });
    await screen.findByText("fox.png");
    expect(screen.getByText("Add an ending picture to start")).toBeVisible();
    fireEvent.paste(composer(), { clipboardData: { files: [ending] } });
    await screen.findByText("end.png");
    expect(screen.getByText("fox.png")).toBeVisible();
    fireEvent.click(makeButton());
    expect(takeHomeHandoff("video")).toMatchObject({ files: [starting, ending], file_inputs: ["image", "last_frame"] });
  });

  it("routes by each accepts filter and shows the existing wrong-type alert for unmatched drop or paste", async () => {
    await chooseKind([textRow, mediaRow()], "Video");
    const gif = new File(["g"], "end.gif", { type: "image/gif" });
    fireEvent.drop(composer(), { dataTransfer: { files: [gif] } });
    await screen.findByText("end.gif");
    expect(screen.getByText("Add a starting picture to start")).toBeVisible();
    const wrong = new File(["x"], "notes.txt", { type: "text/plain" });
    fireEvent.paste(composer(), { clipboardData: { files: [wrong] } });
    expect(await screen.findByRole("alert")).toHaveTextContent("Choose a starting picture");
    expect(screen.queryByText("notes.txt")).toBeNull();
    fireEvent.drop(composer(), { dataTransfer: { files: [wrong] } });
    expect(await screen.findByRole("alert")).toHaveTextContent("Choose a starting picture");
  });

  it("mid offers three labelled compact slots, including the third file in the handoff", async () => {
    const inputs = [...keyframeInputs, { name: "mid_frame", label: "a middle picture", modality: "image", accepts: ["image/png"] }];
    await chooseKind([textRow, mediaRow(inputs)], "Video");
    expect(Array.from(composer().querySelectorAll('input[type="file"]')).map((input) => input.getAttribute("aria-label")))
      .toEqual(["Choose a starting picture", "Choose an ending picture", "Choose a middle picture"]);
    expect(document.querySelector('[data-slot="home-media-input"]')).toBeNull();
    const files = inputs.map((input) => new File(["p"], input.name + ".png", { type: "image/png" }));
    for (const [i, input] of inputs.entries()) {
      fireEvent.change(fileInput(new RegExp("^Choose " + input.label + "$")), { target: { files: [files[i]] } });
      await screen.findByText(files[i].name);
    }
    fireEvent.click(makeButton());
    expect(takeHomeHandoff("video")).toMatchObject({ files, file_inputs: ["image", "last_frame", "mid_frame"] });
  });

  it("scail orders picture then video even when the video is dropped first", async () => {
    const inputs = [
      { name: "reference_image", label: "a new character", modality: "image", accepts: ["image/png"] },
      { name: "source_video", label: "a video to change", modality: "video", accepts: ["video/mp4"] },
    ];
    await chooseKind([textRow, mediaRow(inputs)], "Video");
    expect(fileInput(/^Choose a new character$/).accept).toBe("image/png");
    expect(fileInput(/^Choose a video to change$/).accept).toBe("video/mp4");
    const video = new File(["v"], "source.mp4", { type: "video/mp4" });
    fireEvent.drop(composer(), { dataTransfer: { files: [video] } });
    await screen.findByText("source.mp4");
    const image = picture();
    fireEvent.paste(composer(), { clipboardData: { files: [image] } });
    await screen.findByText("fox.png");
    fireEvent.click(makeButton());
    expect(takeHomeHandoff("video")).toMatchObject({ files: [image, video], file_inputs: ["reference_image", "source_video"] });
  });

  it("each duplicate picture slot keeps its own Library pick", async () => {
    await chooseKind([textRow, mediaRow()], "Video");
    const library = withLibrary([PICTURE_ITEM]);
    Object.defineProperty(HTMLElement.prototype, "hasPointerCapture", { configurable: true, value: () => false });
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: () => {} });
    const ending = screen.getByRole("button", { name: "Attach an ending picture" });
    ending.focus();
    fireEvent.keyDown(ending, { key: "Enter" });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Choose from Library" }));
    fireEvent.click(await screen.findByRole("button", { name: "A teal fish" }));
    await screen.findByRole("button", { name: "Remove an ending picture" });
    expect(screen.getByText("Add a starting picture to start")).toBeVisible();
    expect(library.listed.every((url) => url.includes("kind=image"))).toBe(true);
    const starting = picture();
    fireEvent.change(fileInput(/^Choose a starting picture$/), { target: { files: [starting] } });
    await screen.findByText("fox.png");
    fireEvent.click(makeButton());
    const handoff = takeHomeHandoff("video");
    expect(handoff?.files?.map((file) => file.name)).toEqual(["fox.png", "library-picture.png"]);
    expect(handoff?.file_inputs).toEqual(["image", "last_frame"]);
  });

  it("a filled slot is one chip: Change and Remove carry the slot's label, and no Change pill remains", async () => {
    await chooseKind([textRow, mediaRow()], "Video");
    fireEvent.change(fileInput(/^Choose an ending picture$/), { target: { files: [new File(["e"], "end.png", { type: "image/png" })] } });
    await screen.findByText("end.png");
    // Filled: its Attach pill is gone and the chip carries the change and remove controls.
    expect(screen.queryByRole("button", { name: "Attach an ending picture" })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Change a/ })).not.toBeNull();
    expect(screen.getAllByRole("button", { name: /^Change / })).toHaveLength(1);
    const change = screen.getByRole("button", { name: "Change an ending picture" });
    expect(change).toHaveTextContent("an ending picture:");
    expect(change).toHaveTextContent("end.png");
    expect(screen.getByRole("button", { name: "Remove an ending picture" })).toBeVisible();
    // The unfilled slot keeps its pill.
    expect(screen.getByRole("button", { name: "Attach a starting picture" })).toBeVisible();
    // The chip changes the file: it opens the slot's picker, a choice of the file dialog or the Library.
    withLibrary([PICTURE_ITEM]);
    Object.defineProperty(HTMLElement.prototype, "hasPointerCapture", { configurable: true, value: () => false });
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: () => {} });
    change.focus();
    fireEvent.keyDown(change, { key: "Enter" });
    expect(await screen.findByRole("menuitem", { name: "Choose from Library" })).toBeVisible();
    expect(screen.getByRole("menuitem", { name: "Choose an ending picture" })).toBeVisible();
    fireEvent.keyDown(document.activeElement ?? change, { key: "Escape" });
    // Remove brings the pill back, with focus on it.
    fireEvent.click(screen.getByRole("button", { name: "Remove an ending picture" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Attach an ending picture" })).toHaveFocus());
    expect(screen.queryByRole("button", { name: /^Change / })).toBeNull();
  });

  it("chips keep DOM and Tab order after reverse filling and removing and re-adding Starting", async () => {
    const user = userEvent.setup();
    await chooseKind([textRow, mediaRow()], "Video");
    const ending = new File(["e"], "end.png", { type: "image/png" });
    fireEvent.change(fileInput(/^Choose an ending picture$/), { target: { files: [ending] } });
    await screen.findByText("end.png");
    fireEvent.change(fileInput(/^Choose a starting picture$/), { target: { files: [picture()] } });
    await screen.findByText("fox.png");

    // Start outside the chips, rather than focusing the expected first chip and masking DOM order.
    const beforeChips = document.createElement("button");
    beforeChips.textContent = "Before media slots";
    composer().before(beforeChips);
    const expectSlotOrder = async () => {
      const chips = Array.from(composer().querySelectorAll<HTMLElement>('[data-slot="home-picked-file"]'));
      expect(chips.map((chip) => chip.textContent)).toEqual([
        expect.stringContaining("a starting picture"), expect.stringContaining("an ending picture"),
      ]);
      const names = [
        "Change a starting picture", "Remove a starting picture",
        "Change an ending picture", "Remove an ending picture",
      ];
      beforeChips.focus();
      expect(beforeChips).toHaveFocus();
      for (const name of names) {
        await user.tab();
        const button = screen.getByRole("button", { name });
        expect(button).toHaveFocus();
        expect(button.tabIndex).toBe(0);
      }
    };
    try {
      await expectSlotOrder();
      await user.click(screen.getByRole("button", { name: "Remove a starting picture" }));
      await waitFor(() => expect(screen.getByRole("button", { name: "Attach a starting picture" })).toHaveFocus());
      expect(screen.getByRole("button", { name: "Change an ending picture" })).toHaveTextContent("end.png");
      const replacement = new File(["s"], "start-again.png", { type: "image/png" });
      fireEvent.change(fileInput(/^Choose a starting picture$/), { target: { files: [replacement] } });
      await screen.findByText("start-again.png");
      await expectSlotOrder();
      expect(fileInput(/^Choose a starting picture$/).accept).toBe("image/png,image/jpeg");
      expect(fileInput(/^Choose an ending picture$/).accept).toBe("image/*");
      fireEvent.click(makeButton());
      const handoff = takeHomeHandoff("video");
      expect(handoff?.files?.map((file) => file.name)).toEqual(["start-again.png", "end.png"]);
      expect(handoff?.file_inputs).toEqual(["image", "last_frame"]);
    } finally {
      beforeChips.remove();
    }
  });

  it("an unfilled slot's pill draws its modality's icon, not a paperclip; a single slot keeps the paperclip", async () => {
    await chooseKind([textRow, mediaRow([
      { name: "reference_image", label: "a new character", modality: "image", accepts: ["image/png"] },
      { name: "source_video", label: "a video to change", modality: "video", accepts: ["video/mp4"] },
    ])], "Video");
    const icon = (name: string) => screen.getByRole("button", { name }).querySelector("svg")?.getAttribute("class") ?? "";
    const video = icon("Attach a video to change");
    const image = icon("Attach a new character");
    expect(video).not.toContain("paperclip");
    expect(image).not.toContain("paperclip");
    expect(video).not.toEqual(image);
    expect(video).toContain("video");
    cleanup();
    await chooseKind([textRow, editRow], "Edit");
    expect(screen.getByRole("button", { name: "Attach a picture" }).querySelector("svg")?.getAttribute("class")).toContain("paperclip");
  });

  it("scail's video slot opens a video-filtered Library", async () => {
    await chooseKind([textRow, mediaRow([
      { name: "reference_image", label: "a new character", modality: "image", accepts: ["image/png"] },
      { name: "source_video", label: "a video to change", modality: "video", accepts: ["video/mp4"] },
    ])], "Video");
    const library = withLibrary([PICTURE_ITEM, RECORDING_ITEM]);
    Object.defineProperty(HTMLElement.prototype, "hasPointerCapture", { configurable: true, value: () => false });
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: () => {} });
    const video = screen.getByRole("button", { name: "Attach a video to change" });
    video.focus();
    fireEvent.keyDown(video, { key: "Enter" });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Choose from Library" }));
    await waitFor(() => expect(library.listed.length).toBeGreaterThan(0));
    expect(library.listed.every((url) => url.includes("kind=video"))).toBe(true);
    expect(screen.queryByRole("button", { name: "A teal fish" })).toBeNull();
  });

  it("shows only three slots for the guard case and leaves additional inputs to Try", async () => {
    const inputs = Array.from({ length: 4 }, (_, n) => ({ name: "frame_" + n, label: "picture " + n, modality: "image", accepts: ["image/*"] }));
    await chooseKind([textRow, mediaRow(inputs)], "Video");
    expect(composer().querySelectorAll('input[type="file"]')).toHaveLength(3);
    expect(screen.queryByLabelText("Choose picture 3")).toBeNull();
    const files = inputs.slice(0, 3).map((input) => new File(["p"], input.name + ".png", { type: "image/png" }));
    for (let i = 0; i < files.length; i++) {
      fireEvent.change(fileInput(new RegExp("^Choose picture " + i + "$")), { target: { files: [files[i]] } });
      await screen.findByText(files[i].name);
    }
    fireEvent.click(makeButton());
    expect(takeHomeHandoff("video")).toMatchObject({ files, file_inputs: ["frame_0", "frame_1", "frame_2"] });
  });

  it("a present single input and an older row keep the existing single-picture layout and copy", async () => {
    for (const extra of [{ required_media_inputs: [{ name: "image", label: "a picture", modality: "image", accepts: ["image/*"] }] }, {}]) {
      await chooseKind([textRow, { ...meshRow, ...extra }], "3D models");
      expect(document.querySelectorAll('[data-slot="home-media-input"]')).toHaveLength(1);
      expect(screen.getByRole("button", { name: "Choose a picture" })).toBeVisible();
      expect(screen.getByText("Add a picture to start")).toBeVisible();
      expect(screen.queryByRole("button", { name: "Attach a picture" })).toBeNull();
      cleanup();
    }
  });

  it("a named slot's Library filter follows its modality even without a file filter", async () => {
    await chooseKind([textRow, mediaRow([
      { name: "reference_image", label: "a new character", modality: "image", accepts: ["image/png"] },
      { name: "source_video", label: "a video to change", modality: "video", accepts: [] },
    ])], "Video");
    expect(fileInput(/^Choose a video to change$/).getAttribute("accept")).toBeNull();
    const library = withLibrary([PICTURE_ITEM, RECORDING_ITEM]);
    Object.defineProperty(HTMLElement.prototype, "hasPointerCapture", { configurable: true, value: () => false });
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: () => {} });
    const video = screen.getByRole("button", { name: "Attach a video to change" });
    video.focus();
    fireEvent.keyDown(video, { key: "Enter" });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Choose from Library" }));
    await waitFor(() => expect(library.listed.length).toBeGreaterThan(0));
    expect(library.listed.every((url) => url.includes("kind=video"))).toBe(true);
  });

  it("keeps a multi-slot file control visibly labelled when the Library does not support its modality", async () => {
    await chooseKind([textRow, mediaRow([
      { name: "first", label: "a first sample", modality: "chemical", accepts: [] },
      { name: "second", label: "a second sample", modality: "chemical", accepts: [] },
    ])], "Video");
    expect(screen.getByRole("button", { name: "Attach a first sample" })).toHaveTextContent("Attach a first sample");
    expect(screen.getByRole("button", { name: "Attach a second sample" })).toHaveTextContent("Attach a second sample");
  });

});
