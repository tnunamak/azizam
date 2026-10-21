import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import TryPage from "@/app/try/page";

const account = vi.hoisted(() => ({
  session: vi.fn(),
  tryKey: vi.fn(),
  revokeKey: vi.fn(),
  params: new URLSearchParams(),
  viewer: { authenticated: true, serviceAdmin: true, admin: false },
}));
vi.mock("next/navigation", () => ({ useSearchParams: () => account.params }));
vi.mock("@/lib/account-api", () => ({ accountApi: account }));
vi.mock("@/components/shell/session", () => ({ useViewer: () => account.viewer }));

// Rows shaped like gateway/models_catalog.py `_base_row`: capability intents
// and input modalities under `capabilities`, `alias` and `use_names` on
// aliases, and a manifest `task` for models that make another kind of picture.
const row = (
  id: string,
  name: string,
  intent: string,
  input: string[],
  extra: Record<string, unknown> = {},
) => ({
  id,
  name,
  display_name: name,
  capability: "image",
  alias: false,
  use_names: [id],
  locality: "local",
  capabilities: [
    {
      intent,
      capability: "image",
      input_modalities: input,
      output_modalities: ["image"],
    },
  ],
  ...extra,
});
const catalog = {
  aliases: [
    row("image", "Krea 2 Turbo", "image.generate", ["text"], {
      alias: true,
      recommended: true,
      use_names: ["image", "krea2-turbo"],
    }),
    row("image-edit", "Qwen-Image 2.1 Edit", "image.edit", ["text", "image"], {
      alias: true,
      use_names: ["image-edit", "qwen-image-2.1-edit"],
    }),
  ],
  models: [
    row("krea2-turbo", "Krea 2 Turbo", "image.generate", ["text"]),
    row("anima-turbo", "Anima Turbo (anime)", "image.generate", ["text"]),
    row("flux2-klein-4b", "Flux2 Klein 4B", "image.generate", ["text"]),
    row("qwen-image-2.1-edit", "Qwen-Image 2.1 Edit", "image.edit", ["text", "image"]),
    row("flux2-klein-4b-edit", "Flux2 Klein 4B Edit", "image.edit", ["text", "image"]),
    row("flux2-klein-4b-inpaint", "Flux2 Klein 4B Inpaint", "image.edit", ["text", "image"]),
    row("qwen-image-2.1-inpaint", "Qwen-Image 2.1 Inpaint (LanPaint)", "image.edit", ["text", "image"]),
    row("qwen-image-2.1-edit-multiref", "Qwen-Image 2.1 Multi-Reference Edit", "image.edit", ["text", "image"]),
    row("seedvr2-upscale", "SeedVR2 7B Sharp Upscale", "image.edit", ["image"]),
    row("face-likeness", "Face Likeness Check", "image.analyze", ["image"]),
    row("sdxl-seamless-tile", "SDXL Seamless Tile", "image.generate", ["text"], {
      task: "texture-tile",
    }),
    row("flux2-klein-4b-seamless-tile", "Flux2 Klein 4B Seamless Tile", "image.generate", ["text"], {
      task: "texture-tile",
    }),
    {
      id: "chat-a",
      name: "Chat model",
      capability: "chat",
      capabilities: [{ intent: "chat.completions", input_modalities: ["text"] }],
      locality: "local",
    },
  ],
};
const json = (body: unknown) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });

beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, "hasPointerCapture", {
    configurable: true,
    value: () => false,
  });
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
    configurable: true,
    value: () => {},
  });
  account.params = new URLSearchParams("capability=image");
  account.session.mockResolvedValue({ authenticated: true, apps: [] });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      json(url.includes("workflows") ? { workflows: [] } : catalog),
    ),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function optionNames() {
  const user = userEvent.setup();
  render(<TryPage />);
  await waitFor(() =>
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Krea 2 Turbo"),
  );
  await user.click(screen.getByRole("combobox", { name: "Model" }));
  return screen.getAllByRole("option").map((option) => option.textContent);
}

describe("Make a picture lists only models that make a picture from a prompt", () => {
  it("leaves out edit, inpaint, upscale, tile and check models", async () => {
    const names = await optionNames();
    expect(names).toEqual(["Krea 2 Turbo", "Anima Turbo (anime)", "Flux2 Klein 4B"]);
    for (const hidden of [
      "Qwen-Image 2.1 Edit",
      "Flux2 Klein 4B Edit",
      "Flux2 Klein 4B Inpaint",
      "Qwen-Image 2.1 Inpaint (LanPaint)",
      "Qwen-Image 2.1 Multi-Reference Edit",
      "SeedVR2 7B Sharp Upscale",
      "Face Likeness Check",
      "SDXL Seamless Tile",
      "Flux2 Klein 4B Seamless Tile",
    ])
      expect(names.join("|")).not.toContain(hidden);
  });

  it("lists Krea 2 Turbo once: the alias and its model are one choice", async () => {
    const names = await optionNames();
    expect(names.filter((name) => name?.includes("Krea 2 Turbo"))).toHaveLength(1);
    // Not disambiguated with ids either: there is nothing to tell apart.
    expect(names.join("|")).not.toContain("krea2-turbo");
  });

  it("still opens a deep link to the model the alias stands for", async () => {
    account.params = new URLSearchParams("capability=image&model=krea2-turbo");
    render(<TryPage />);
    await waitFor(() =>
      expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Krea 2 Turbo"),
    );
    expect(screen.getByRole("combobox", { name: "Model" })).not.toHaveTextContent("Unverified");
  });

  it("points to Workflow for tools that start from a picture", async () => {
    const user = userEvent.setup();
    render(<TryPage />);
    await waitFor(() =>
      expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Krea 2 Turbo"),
    );
    await user.click(screen.getByRole("button", { name: "Workflow" }));
    expect(screen.getByRole("tab", { name: "Workflow" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("keeps a row that carries no intent detail", async () => {
    // An older gateway: `capability` only. Nothing is hidden by guesswork.
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        json(
          url.includes("workflows")
            ? { workflows: [] }
            : {
                aliases: [],
                models: [
                  { id: "old-image", name: "Old picture model", capability: "image" },
                ],
              },
        ),
      ),
    );
    render(<TryPage />);
    await waitFor(() =>
      expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Old picture model"),
    );
  });
});

it("keeps the model picker disabled and busy until the catalog arrives", async () => {
  let finish!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn((url: string) => url.includes("workflows") ? Promise.resolve(json({ workflows: [] })) : new Promise<Response>((resolve) => { finish = resolve; })));
  render(<TryPage />);
  expect(screen.getByRole("combobox", { name: "Model" })).toBeDisabled();
  expect(screen.getByRole("status", { name: "Loading models" })).toHaveAttribute("aria-busy", "true");
  expect(screen.queryByText(/Unverified model/)).toBeNull();
  finish(json(catalog));
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toBeEnabled());
  expect(screen.queryByRole("status", { name: "Loading models" })).toBeNull();
});

it("places a selected model's outage and Status link beside its picker", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => json(url.includes("workflows") ? { workflows: [] } : {
    aliases: [], models: [row("picture", "Picture model", "image.generate", ["text"], {
      engine_label: "Picture studio", availability: { status: "unavailable" },
    })],
  })));
  render(<TryPage />);
  const link = await screen.findByRole("link", { name: "See Status" });
  expect(link.closest("form")).toBe(screen.getByRole("combobox", { name: "Model" }).closest("form"));
  expect(link.parentElement).toHaveTextContent("Picture studio can’t take requests right now. See Status");
});

describe("fallback capabilities and catalog envelopes", () => {
  const unavailable = () => new Response("", { status: 503 });
  function withTryKey() {
    account.params = new URLSearchParams("capability=chat");
    account.session.mockResolvedValue({
      authenticated: true, apps: [{ id: "try-app", name: "Try" }],
    });
    account.tryKey.mockResolvedValue({ id: "test-key", api_key: "fixture-only", reused: false, app_name: "Try", expires_at: 0 });
  }

  it.each([
    ["audio", false, false],
    ["workflow", false, true],
    ["music", true, false],
    ["sfx", true, true],
  ])("shows unsupported %s fallback rows (mixed=%s, workflow failure=%s)",
    async (capability, mixed, workflowFailure) => {
      withTryKey();
      const rows = [
        { id: "novel-work", capability },
        ...(mixed ? [{ id: "novel-chat", name: "Discovered chat", capability: "chat" }] : []),
      ];
      vi.stubGlobal("fetch", vi.fn(async (url: string) => {
        if (url.includes("workflows"))
          return workflowFailure ? unavailable() : json({ workflows: [] });
        if (url.includes("/models/catalog")) return unavailable();
        if (url.endsWith("/models")) return json({ data: rows });
        return json({ choices: [{ message: { content: "Fixture reply" } }] });
      }));
      const user = userEvent.setup();
      render(<TryPage />);
      await screen.findByRole("button", { name: "Retry" });
      // Run waits for a list; Retry prepares the key and reads the fallback.
      expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
      await user.click(screen.getByRole("button", { name: "Retry" }));
      await screen.findByText(/1 model has no supported Try capability/);
      const picker = screen.getByRole("combobox", { name: "Model" });
      if (mixed) {
        expect(picker).toHaveTextContent("Discovered chat");
        await user.click(picker);
        expect(screen.getAllByRole("option")).toHaveLength(1);
        await user.keyboard("{Escape}");
      } else {
        expect(picker).toBeDisabled();
      }
      await user.click(screen.getByRole("tab", { name: "Workflow" }));
      expect(screen.getByRole("combobox", { name: "Model" })).toBeDisabled();
      expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
      expect(screen.getByText(/1 model has no supported Try capability/)).toBeVisible();
      if (workflowFailure)
        expect(screen.getByRole("button", { name: "Retry" })).toBeVisible();
    });

  it.each([{}, { aliases: [], models: {} }, null])(
    "treats malformed rich HTTP 200 as retryable on first load: %j", async (body) => {
      let payload: unknown = body;
      vi.stubGlobal("fetch", vi.fn(async (url: string) =>
        json(url.includes("workflows") ? { workflows: [] } : payload)));
      const user = userEvent.setup();
      render(<TryPage />);
      await screen.findByRole("button", { name: "Retry" });
      expect(screen.queryByText(/models are available/)).toBeNull();
      expect(screen.getByRole("combobox", { name: "Model" })).toBeDisabled();
      expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
      payload = catalog;
      await user.click(screen.getByRole("button", { name: "Retry" }));
      await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Krea 2 Turbo"));
    });

  it("preserves a chosen model after a malformed rich refresh and allows retry", async () => {
    let payload: unknown = catalog;
    vi.stubGlobal("fetch", vi.fn(async (url: string) =>
      json(url.includes("workflows") ? { workflows: [] } : payload)));
    const user = userEvent.setup();
    const view = render(<TryPage />);
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Krea 2 Turbo"));
    await user.click(screen.getByRole("combobox", { name: "Model" }));
    await user.click(screen.getByRole("option", { name: "Anima Turbo (anime)" }));
    payload = {};
    account.params = new URLSearchParams("capability=images");
    view.rerender(<TryPage />);
    await screen.findByRole("button", { name: "Retry" });
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Anima Turbo (anime)");
    expect(screen.queryByText(/models are available/)).toBeNull();
    payload = catalog;
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Retry" })).toBeNull());
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Anima Turbo (anime)");
  });

  it.each([{}, { data: {} }, null])(
    "treats malformed fallback HTTP 200 as retryable on first load: %j", async (body) => {
      withTryKey();
      let payload: unknown = body;
      vi.stubGlobal("fetch", vi.fn(async (url: string) => {
        if (url.includes("workflows")) return json({ workflows: [] });
        if (url.includes("/models/catalog")) return unavailable();
        return json(payload);
      }));
      const user = userEvent.setup();
      render(<TryPage />);
      await screen.findByRole("button", { name: "Retry" });
      await user.click(screen.getByRole("button", { name: "Retry" }));
      await waitFor(() => expect(vi.mocked(fetch)).toHaveBeenCalledWith(
        "/openai/v1/models", expect.anything()));
      await screen.findByRole("button", { name: "Retry" });
      expect(screen.queryByText(/models are available/)).toBeNull();
      expect(screen.getByRole("combobox", { name: "Model" })).toBeDisabled();
      payload = { data: [{ id: "new-chat", name: "Recovered chat", capability: "chat" }] };
      await user.click(screen.getByRole("button", { name: "Retry" }));
      await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Recovered chat"));
    });

  it("preserves a selected model on malformed fallback refresh, then accepts valid empty", async () => {
    withTryKey();
    let first = true;
    let payload: unknown = { data: {} };
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.includes("workflows")) return json({ workflows: [] });
      if (url.includes("/models/catalog")) {
        if (first) { first = false; return json(catalog); }
        return unavailable();
      }
      if (url.endsWith("/models")) return json(payload);
      return json({ choices: [{ message: { content: "Fixture reply" } }] });
    }));
    const user = userEvent.setup();
    render(<TryPage />);
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Chat model"));
    await user.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByRole("button", { name: "Retry" });
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Chat model");
    expect(screen.queryByText(/models are available/)).toBeNull();
    payload = { data: [] };
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText(/No chat models are available/);
    expect(screen.getByRole("combobox", { name: "Model" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
  });

  it("reserves the empty state for valid rich arrays", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) =>
      json(url.includes("workflows") ? { workflows: [] } : { aliases: [], models: [] })));
    render(<TryPage />);
    await screen.findByText(/No picture models are available/);
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
  });
});

describe("Try keys stay in page memory", () => {
  const storageKey = "azizam.try.key";
  const credential = (id: string, secret: string) => ({
    id, api_key: secret, reused: false, app_name: "Try", expires_at: 0,
  });

  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    account.viewer = { authenticated: true, serviceAdmin: true, admin: false };
    account.params = new URLSearchParams("capability=chat");
    account.tryKey.mockReset();
    account.revokeKey.mockReset();
    account.tryKey.mockResolvedValue(credential("fixture-key-a", "fixture-secret-a"));
    account.revokeKey.mockResolvedValue({});
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.includes("workflows")) return json({ workflows: [] });
      if (url.includes("/models/catalog")) return json(catalog);
      return json({ choices: [{ message: { content: "Fixture reply" } }] });
    }));
  });

  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
    sessionStorage.clear();
  });

  async function runPage(container: HTMLElement) {
    const page = within(container);
    await waitFor(() => expect(page.getByRole("combobox", { name: "Model" })).toHaveTextContent("Chat model"));
    await userEvent.setup().click(page.getByRole("button", { name: "Run" }));
    await page.findByText("Fixture reply");
    await waitFor(() => expect(page.getByRole("button", { name: "Run" })).toBeEnabled());
  }

  it("uses the in-memory key repeatedly without persisting its secret", async () => {
    const writes = vi.spyOn(Storage.prototype, "setItem");
    const view = render(<TryPage />);
    await runPage(view.container);
    await runPage(view.container);
    expect(account.tryKey).toHaveBeenCalledTimes(1);
    expect(account.tryKey).toHaveBeenCalledWith(null);
    expect(vi.mocked(fetch)).toHaveBeenCalledWith(
      "/openai/v1/chat/completions",
      expect.objectContaining({ headers: expect.objectContaining({ authorization: "Bearer fixture-secret-a" }) }),
    );
    expect(writes.mock.calls.some(([name, value]) => name === storageKey || value.includes("fixture-secret-a"))).toBe(false);
    expect(localStorage.getItem(storageKey)).toBeNull();
    expect(sessionStorage.getItem(storageKey)).toBeNull();
  });

  it("removes a legacy secret on load without reading or reusing it", async () => {
    localStorage.setItem(storageKey, JSON.stringify({
      id: "legacy-fixture", secret: "legacy-fixture-secret", appName: "Try",
    }));
    const reads = vi.spyOn(Storage.prototype, "getItem");
    const view = render(<TryPage />);
    expect(localStorage.getItem(storageKey)).toBeNull();
    // The assertion above is the only read of this key.
    expect(reads.mock.calls.filter(([name]) => name === storageKey)).toHaveLength(1);
    expect(account.tryKey).not.toHaveBeenCalled();
    await runPage(view.container);
    expect(account.tryKey).toHaveBeenCalledWith(null);
  });

  it("re-acquires a key after a page reload", async () => {
    const first = render(<TryPage />);
    await runPage(first.container);
    first.unmount();
    account.tryKey.mockResolvedValue(credential("fixture-key-b", "fixture-secret-b"));
    const reloaded = render(<TryPage />);
    await runPage(reloaded.container);
    expect(account.tryKey.mock.calls).toEqual([[null], [null]]);
    expect(vi.mocked(fetch)).toHaveBeenCalledWith(
      "/openai/v1/chat/completions",
      expect.objectContaining({ headers: expect.objectContaining({ authorization: "Bearer fixture-secret-b" }) }),
    );
    expect(account.revokeKey).not.toHaveBeenCalled();
  });

  it("keeps independent tab instances usable when one tab revokes its key", async () => {
    account.tryKey
      .mockResolvedValueOnce(credential("fixture-key-a", "fixture-secret-a"))
      .mockResolvedValueOnce(credential("fixture-key-b", "fixture-secret-b"));
    const first = render(<TryPage />);
    const second = render(<TryPage />);
    await runPage(first.container);
    await runPage(second.container);
    expect(account.tryKey.mock.calls).toEqual([[null], [null]]);
    const user = userEvent.setup();
    await user.click(within(first.container).getByText("Key details"));
    await user.click(within(first.container).getByRole("button", { name: "Remove this key" }));
    await waitFor(() => expect(within(first.container).queryByText("Key details")).toBeNull());
    expect(account.revokeKey).toHaveBeenCalledWith("fixture-key-a");
    await runPage(second.container);
    expect(account.tryKey).toHaveBeenCalledTimes(2);
    expect(vi.mocked(fetch)).toHaveBeenLastCalledWith(
      "/openai/v1/chat/completions",
      expect.objectContaining({ headers: expect.objectContaining({ authorization: "Bearer fixture-secret-b" }) }),
    );
  });

  it("clears the key on sign-out and re-acquires on the next sign-in", async () => {
    const view = render(<TryPage />);
    await runPage(view.container);
    account.viewer = { ...account.viewer, authenticated: false };
    view.rerender(<TryPage />);
    expect(screen.queryByText("Key details")).toBeNull();
    account.viewer = { ...account.viewer, authenticated: true };
    account.tryKey.mockResolvedValue(credential("fixture-key-b", "fixture-secret-b"));
    view.rerender(<TryPage />);
    await runPage(view.container);
    expect(account.tryKey.mock.calls).toEqual([[null], [null]]);
    expect(localStorage.getItem(storageKey)).toBeNull();
    expect(sessionStorage.getItem(storageKey)).toBeNull();
  });

  it("discards a key response that arrives after sign-out", async () => {
    let finish!: (value: ReturnType<typeof credential>) => void;
    account.tryKey.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    const view = render(<TryPage />);
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Chat model"));
    await userEvent.setup().click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() => expect(account.tryKey).toHaveBeenCalledTimes(1));
    account.viewer = { ...account.viewer, authenticated: false };
    view.rerender(<TryPage />);
    await act(async () => { finish(credential("fixture-key-a", "fixture-secret-a")); });
    expect(screen.queryByText("Key details")).toBeNull();
    expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("/chat/completions"))).toBe(false);
    expect(localStorage.getItem(storageKey)).toBeNull();
  });
});


it("announces access refresh completion and preserves Refresh focus while loading", async () => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  account.viewer = { authenticated: true, serviceAdmin: false, admin: false };
  Object.assign(account, { promptMemory: vi.fn().mockResolvedValue({ prompt_memory: null }) });
  let finish: (() => void) | undefined;
  let hold = false;
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (hold && url.includes("models/catalog")) await new Promise<void>((resolve) => { finish = resolve; });
    return json(url.includes("workflows") ? { workflows: [] } : { ...catalog, access_plan: { id: "family", name: "Family" } });
  }));
  try {
    render(<TryPage />);
    const status = await screen.findByText("Your access plan: Family.");
    expect(status).toHaveAttribute("role", "status");
    const user = userEvent.setup();
    const refresh = screen.getByRole("button", { name: "Refresh access" });
    hold = true;
    await user.click(refresh);
    expect(refresh).toHaveFocus();
    expect(refresh).not.toBeDisabled();
    expect(refresh).toHaveAttribute("aria-disabled", "true");
    expect(refresh).toHaveAttribute("aria-busy", "true");
    expect(status).toHaveTextContent("Checking your access plan…");
    await user.click(refresh);
    expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes("models/catalog"))).toHaveLength(2);
    await act(async () => { finish!(); });
    await waitFor(() => expect(refresh).toHaveAttribute("aria-busy", "false"));
    expect(refresh).toHaveFocus();
    expect(status).toHaveTextContent("Your access plan: Family.");
    expect(status).toHaveTextContent("Access refresh complete.");
  } finally {
    account.viewer.serviceAdmin = true;
  }
});
