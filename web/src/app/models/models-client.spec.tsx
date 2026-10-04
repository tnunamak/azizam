import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ModelsClient from "./models-client";
import { ViewerContext, type Viewer } from "@/components/shell/session";
import {
  productApi,
  type CatalogRow,
  type ModelsCatalog,
} from "@/lib/product-api";

vi.mock("@/lib/product-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/product-api")>();
  return {
    ...actual,
    productApi: {
      ...actual.productApi,
      models: vi.fn(),
    },
  };
});

const owner: Viewer = {
  authenticated: true,
  name: "Owner",
  email: "owner@example.test",
  role: "owner",
  admin: true,
};

const member: Viewer = {
  authenticated: true,
  name: "Member",
  email: "member@example.test",
  role: "member",
  admin: false,
};

function row(overrides: Partial<CatalogRow> = {}): CatalogRow {
  return {
    id: "chat",
    object: "model_catalog.alias",
    display_name: "chat",
    name: "chat",
    alias: true,
    operations: ["chat"],
    resolves_to: { chat: "demo-chat" },
    description: "Chat through your configured model.",
    capabilities: [
      {
        intent: "chat.completions",
        label: "Chat",
        input_modalities: ["text"],
        output_modalities: ["text"],
        streaming: true,
        available: true,
      },
    ],
    provider: null,
    engine_label: "Household chat",
    locality: "local",
    cost: { kind: "free", price: null },
    timing: { typical_seconds: null, source: null },
    availability: {
      available: true,
      status: "ready",
      reason: "Recent request succeeded.",
      measured: true,
      enabled: true,
      configured: true,
      up: true,
      idle_startable: false,
      evidence: { source: "request", observed_at_s: 1800000000 },
    },
    io: { input: ["text"], output: ["text"] },
    examples: [],
    recent_receipts: [],
    use: {
      model: "chat",
      path: "/v1/chat/completions",
      curl: null,
      python: { path: "/v1/chat/completions", model: "chat" },
      javascript: { path: "/v1/chat/completions", model: "chat" },
    },
    ...overrides,
  };
}

function catalog(
  aliases: CatalogRow[],
  models: CatalogRow[] = [],
): ModelsCatalog {
  return {
    schema_version: 1,
    object: "models.catalog",
    aliases,
    models,
    counts: {
      aliases: aliases.length,
      models: models.length,
      capabilities: [...aliases, ...models].flatMap(
        (entry) => entry.capabilities,
      ).length,
    },
  };
}

function renderModels(snapshot: ModelsCatalog, viewer: Viewer = member) {
  vi.mocked(productApi.models).mockResolvedValue(snapshot);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <ViewerContext.Provider value={viewer}>
      <QueryClientProvider client={client}>
        <ModelsClient />
      </QueryClientProvider>
    </ViewerContext.Provider>,
  );
}

beforeEach(() => {
  vi.mocked(productApi.models).mockReset();
});

afterEach(() => {
  cleanup();
});

describe("models catalog contract distinctions", () => {
  it("copies unavailable names from recommendations, alternatives, profiles and exact models", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const unavailableRow = (id: string, overrides: Partial<CatalogRow> = {}) =>
      row({
        id,
        availability: {
          ...row().availability,
          available: false,
          status: "unavailable",
        },
        ...overrides,
      });
    renderModels(
      catalog(
        [
          unavailableRow("default", {
            examples: [
              {
                kind: "chat.completions",
                body: { model: "default", messages: [] },
              },
            ],
          }),
          unavailableRow("fast"),
          unavailableRow("profile", { secondary_profile: true }),
        ],
        [
          unavailableRow("exact", {
            alias: false,
            object: "model_catalog.model",
          }),
        ],
      ),
    );
    const copy = await screen.findByRole("button", {
      name: "Copy model name default",
    });
    fireEvent.click(screen.getByText("Other options · 1"));
    fireEvent.click(screen.getByRole("button", { name: /More chat profiles/ }));
    fireEvent.click(screen.getByRole("button", { name: "All models" }));
    for (const id of ["default", "fast", "profile", "exact"]) {
      fireEvent.click(
        screen.getByRole("button", { name: `Copy model name ${id}` }),
      );
      expect(writeText).toHaveBeenCalledWith(id);
    }
    expect(screen.queryByRole("link", { name: "Check status" })).toBeNull();
    expect(screen.queryByText(/\d+ shown/)).toBeNull();
    expect(document.querySelectorAll('[data-variant="default"]')).toHaveLength(
      0,
    );
    fireEvent.click(
      within(copy.closest("article")!).getByRole("button", { name: "Details" }),
    );
    const dialog = within(screen.getByRole("dialog"));
    expect(dialog.getByText("Example request")).toBeVisible();
    expect(dialog.getByRole("link", { name: "Check status" })).toBeVisible();
    expect(dialog.getByText(/curl 'http/)).toBeInTheDocument();
  });

  it("keeps explicit server capability authoritative over a legacy intent", async () => {
    renderModels(
      catalog([
        row({
          id: "operator-audio",
          capabilities: [
            {
              ...row().capabilities[0],
              intent: "speech.synthesis",
              capability: "audio",
              label: "Audio",
            },
          ],
        }),
      ]),
    );
    expect(
      await screen.findByRole("heading", { name: "Audio", level: 2 }),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "Speech" })).toBeNull();
  });

  it("keeps free remote placement honest and price filters quiet in the capability row", async () => {
    renderModels(catalog([row({ locality: "remote" })]));
    await screen.findByRole("button", { name: "Copy model name chat" });
    expect(screen.getByText("Remote", { exact: true })).toBeVisible();
    expect(screen.queryByText(/Local/)).toBeNull();
    const price = screen.getByRole("group", {
      name: "Price filters (choose any)",
    });
    expect(price.parentElement).toContainElement(
      screen.getByRole("button", { name: /^All$/ }),
    );
    const free = within(price).getByRole("button", {
      name: "No provider charge",
    });
    expect(free).toHaveAttribute("aria-pressed", "true");
    expect(free).not.toHaveAttribute("data-slot", "button");
    fireEvent.click(free);
    expect(free).toHaveAttribute("aria-pressed", "false");
    expect(
      screen.queryByRole("button", { name: "Copy model name chat" }),
    ).toBeNull();
  });

  it("groups aliases without presentation metadata by their contract intent", async () => {
    const intentRow = (id: string, intent: string, label: string) =>
      row({
        id,
        capabilities: [{ ...row().capabilities[0], intent, label }],
      });
    renderModels(
      catalog([
        intentRow("chat", "chat.completions", "Chat"),
        intentRow("fast", "text.completions", "Chat"),
        intentRow("image", "image.generate", "Images"),
        intentRow("decide", "decision.systemone", "Decisions"),
        intentRow("tts", "speech.synthesis", "Speech"),
      ]),
    );
    await screen.findByRole("button", { name: "Copy model name chat" });
    const chatGroup = screen
      .getByRole("heading", { name: "Chat", level: 2 })
      .closest("section")!;
    expect(
      within(chatGroup).getByText("Other options · 1"),
    ).toBeInTheDocument();
    expect(within(chatGroup).queryByText("image", { exact: true })).toBeNull();
    for (const label of ["Images", "Decisions", "Speech"]) {
      expect(
        screen.getByRole("heading", { name: label, level: 2 }),
      ).toBeVisible();
    }
    fireEvent.click(screen.getByRole("button", { name: "Images" }));
    expect(
      screen.getByRole("button", { name: "Copy model name image" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Copy model name chat" }),
    ).toBeNull();
  });

  it("uses product alias fallback and one availability line for allowed configured observed rows", async () => {
    renderModels(catalog([row()]));

    const copy = await screen.findByRole("button", {
      name: "Copy model name chat",
    });
    const card = within(copy.closest("article")!);

    expect(productApi.models).toHaveBeenCalledOnce();
    expect(card.getByText("Chat through your configured model.")).toBeVisible();
    expect(card.getByText("Ready")).toBeVisible();
    expect(card.queryByText("Recent request succeeded.")).toBeNull();
    expect(card.queryByText("Allowed by your access")).toBeNull();
    expect(card.queryByText("Observed:")).toBeNull();
    expect(card.queryByText("Not measured yet")).toBeNull();
    expect(card.queryByRole("button", { name: "Use" })).toBeNull();
    expect(
      card.getByRole("button", { name: "Copy model name chat" }),
    ).toBeVisible();
  });

  it("uses authored row descriptions before alias fallback", async () => {
    renderModels(
      catalog([
        row({
          id: "fast",
          description: "Use this when you want short replies.",
        }),
      ]),
    );

    const copy = await screen.findByRole("button", {
      name: "Copy model name fast",
    });
    const card = within(copy.closest("article")!);

    expect(
      card.getByText("Use this when you want short replies."),
    ).toBeVisible();
    expect(
      card.queryByText("Fast chat alias for lower-latency replies."),
    ).toBeNull();
  });

  it("renders the description from the presentation contract", async () => {
    renderModels(
      catalog([
        row({
          id: "chat",
          description: "Thinks through questions before answering.",
        }),
      ]),
    );

    const copy = await screen.findByRole("button", {
      name: "Copy model name chat",
    });
    const card = within(copy.closest("article")!);

    expect(
      card.getByText("Thinks through questions before answering."),
    ).toBeVisible();
  });

  it("uses authored row descriptions even when they contain backend words", async () => {
    renderModels(
      catalog([
        row({
          id: "fast",
          description: "Local server tuned for short replies.",
        }),
      ]),
    );

    const copy = await screen.findByRole("button", {
      name: "Copy model name fast",
    });
    const card = within(copy.closest("article")!);

    expect(
      card.getByText("Local server tuned for short replies."),
    ).toBeVisible();
    expect(
      card.queryByText("Fast chat alias for lower-latency replies."),
    ).toBeNull();
  });

  it("keeps technical descriptions out of cards and shows them in details", async () => {
    renderModels(
      catalog([
        row({
          id: "fast",
          title: "Fast",
          description: "Answers quickly for everyday prompts.",
          technical_description: "Served via local llama.cpp on :5051.",
        }),
      ]),
    );

    const copy = await screen.findByRole("button", {
      name: "Copy model name fast",
    });
    const card = within(copy.closest("article")!);
    expect(
      card.getByText("Answers quickly for everyday prompts."),
    ).toBeVisible();
    expect(
      card.queryByText("Served via local llama.cpp on :5051."),
    ).not.toBeInTheDocument();
    fireEvent.click(card.getByRole("button", { name: "Details" }));
    const dialog = within(screen.getByRole("dialog"));
    expect(dialog.getByText("Technical description")).toBeVisible();
    expect(
      dialog.getByText("Served via local llama.cpp on :5051."),
    ).toBeVisible();
  });

  it("renders the human engine label independently of profile description", async () => {
    renderModels(
      catalog([
        row({
          title: "Default",
          description: "Thinks through questions before answering.",
          engine_label: "llama.cpp (chat)",
        }),
      ]),
    );
    const copy = await screen.findByRole("button", {
      name: "Copy model name chat",
    });
    const card = within(copy.closest("article")!);
    expect(card.getByRole("heading", { name: "Default" })).toBeVisible();
    expect(card.queryByText("llama.cpp (chat)")).toBeNull();
    fireEvent.click(card.getByRole("button", { name: /Details/ }));
    expect(screen.getByText("llama.cpp (chat)")).toBeVisible();
    expect(card.queryByText(/rollback|official/i)).toBeNull();
  });

  it("keeps provider provenance for remote summaries", async () => {
    renderModels(
      catalog([
        row({
          locality: "remote",
          provider: "provider-internal-id",
          engine_label: "OpenRouter",
        }),
      ]),
    );

    const copy = await screen.findByRole("button", {
      name: "Copy model name chat",
    });
    const card = within(copy.closest("article")!);

    expect(card.queryByText("OpenRouter")).toBeNull();
    fireEvent.click(card.getByRole("button", { name: /Details/ }));
    expect(screen.getByText("OpenRouter")).toBeVisible();
  });

  it("groups explicit music and speech capabilities separately with server labels", async () => {
    renderModels(
      catalog([
        row({
          id: "music",
          display_name: "ACE-Step music",
          name: "ace-step-xl",
          description: "Generate music from a prompt.",
          engine_label: "Music (ACE-Step XL Turbo)",
          capabilities: [
            {
              capability: "music",
              intent: "audio.generate",
              label: "Music",
              input_modalities: ["text"],
              output_modalities: ["audio"],
              streaming: false,
              available: true,
            } as CatalogRow["capabilities"][number],
          ],
        }),
        row({
          id: "speech",
          display_name: "Chatterbox speech",
          name: "chatterbox",
          description: "Read text aloud.",
          engine_label: "Chatterbox (speech)",
          capabilities: [
            {
              capability: "speech",
              intent: "speech.synthesis",
              label: "Speech",
              input_modalities: ["text"],
              output_modalities: ["audio"],
              streaming: false,
              available: true,
            } as CatalogRow["capabilities"][number],
          ],
        }),
      ]),
    );

    expect(await screen.findByRole("heading", { name: "Music" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Speech" })).toBeVisible();
    expect(screen.queryByText("Music (ACE-Step XL Turbo)")).toBeNull();
    expect(screen.queryByText("Chatterbox (speech)")).toBeNull();
  });

  it("uses the server audio taxonomy instead of the operation intent", async () => {
    renderModels(
      catalog([
        row({
          id: "legacy-audio",
          icon_key: "sfx",
          capability_title: "Audio",
          display_name: "Legacy audio",
          name: "legacy-audio",
          capabilities: [
            {
              intent: "audio.generate",
              capability: "audio",
              label: "Audio",
              input_modalities: ["text"],
              output_modalities: ["audio"],
              streaming: false,
              available: true,
            },
          ],
        }),
      ]),
    );

    expect(await screen.findByRole("heading", { name: "Audio" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Speech" })).toBeNull();
  });

  it("renders the server rerank capability label", async () => {
    renderModels(
      catalog(
        [],
        [
          row({
            id: "rerank-model",
            alias: false,
            object: "model_catalog.model",
            display_name: "Reranker",
            name: "reranker",
            capabilities: [
              {
                capability: "rerank",
                intent: "text.rerank",
                label: "Rerank",
                input_modalities: ["text"],
                output_modalities: ["text"],
                streaming: false,
                available: true,
              } as CatalogRow["capabilities"][number],
            ],
          }),
        ],
      ),
    );

    fireEvent.click(await screen.findByRole("button", { name: /All models/ }));
    const table = within(screen.getByRole("table"));

    expect(table.getByText("Rerank")).toBeVisible();
  });

  it("shows local no-provider-charge cost even when backend cost is unknown", async () => {
    renderModels(
      catalog([
        row({
          cost: { kind: "unknown", price: null },
          locality: "local",
        }),
      ]),
    );

    expect(await screen.findByText("Local · no provider charge")).toBeVisible();
    expect(screen.queryByText("Price not provided")).toBeNull();
  });

  it("filters local unknown-cost rows with the no-provider-charge toggle", async () => {
    renderModels(
      catalog([
        row({
          id: "local-unknown-cost",
          cost: { kind: "unknown", price: null },
          locality: "local",
        }),
      ]),
    );

    expect(
      await screen.findByRole("button", {
        name: "Copy model name local-unknown-cost",
      }),
    ).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "No provider charge" }));

    expect(
      screen.queryByRole("button", {
        name: "Copy model name local-unknown-cost",
      }),
    ).toBeNull();
    expect(screen.getByText("No models match these filters")).toBeVisible();
  });

  it("keeps local rows when paid filter is off even if backend cost is inconsistent", async () => {
    renderModels(
      catalog([
        row({
          id: "local-paid-cost",
          cost: { kind: "paid", price: "$9" },
          locality: "local",
        }),
      ]),
    );

    expect(
      await screen.findByRole("button", {
        name: "Copy model name local-paid-cost",
      }),
    ).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Paid" }));

    expect(
      screen.getByRole("button", { name: "Copy model name local-paid-cost" }),
    ).toBeVisible();
    expect(screen.getByText("Local · no provider charge")).toBeVisible();
  });

  it("uses the friendly alias title from the server", async () => {
    renderModels(
      catalog([
        row({
          id: "coder",
          title: "Coder",
          display_name: "Chat",
          name: "coder",
          description: "Chat model visible to your access plan.",
        }),
      ]),
    );

    expect(await screen.findByText("Coder")).toBeVisible();
  });

  it("keeps primary profile cards distinct and secondary profiles collapsed until opened", async () => {
    renderModels(
      catalog([
        row({
          id: "default",
          title: "Default",
          description: "Reasoning profile for considered answers.",
        }),
        row({
          id: "fast",
          title: "Fast",
          description: "Direct answers with thinking disabled.",
        }),
        row({
          id: "coder",
          title: "Coder",
          description: "Coding profile for technical work.",
        }),
        row({
          id: "coder-q5",
          title: "Coder Q5",
          secondary_profile: true,
          description: "Coding profile with Q5 weights.",
        }),
        row({
          id: "rp",
          title: "Roleplay",
          secondary_profile: true,
          description: "Chat profile for roleplay.",
        }),
      ]),
    );
    expect(
      await screen.findByRole("heading", { name: "Default" }),
    ).toBeVisible();
    const otherOptions = screen.getByText("Other options · 2");
    fireEvent.click(otherOptions);
    expect(screen.getByText("fast")).toBeVisible();
    expect(screen.getByText("coder")).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Coder Q5" })).toBeNull();
    const more = screen.getByRole("button", { name: /More chat profiles/ });
    expect(more).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(more);
    expect(screen.getByRole("heading", { name: "Coder Q5" })).toBeVisible();
    expect(screen.getByText("Coding profile with Q5 weights.")).toBeVisible();
    expect(screen.getByText("Chat profile for roleplay.")).toBeVisible();
    expect(
      screen.queryByText("Chat model visible to your access plan."),
    ).toBeNull();
  });

  it("puts direct secondary model profiles under More chat profiles instead of All models", async () => {
    renderModels(
      catalog(
        [],
        [
          row({
            id: "coder-q8",
            alias: false,
            object: "model_catalog.model",
            title: "Coder Q8",
            secondary_profile: true,
            description: "Coding profile with Q8 weights.",
          }),
        ],
      ),
    );
    const more = await screen.findByRole("button", {
      name: /More chat profiles/,
    });
    expect(
      screen.queryByText("No stable aliases match these filters"),
    ).toBeNull();
    expect(screen.queryByRole("heading", { name: "Coder Q8" })).toBeNull();
    expect(screen.queryByText(/\d+ shown/)).toBeNull();
    fireEvent.click(more);
    expect(screen.getByRole("heading", { name: "Coder Q8" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Details" }));
    expect(
      within(screen.getByRole("dialog")).getByRole("heading", {
        name: "Coder Q8",
      }),
    ).toBeVisible();
  });

  it("finds collapsed profiles and alternative use names through search", async () => {
    renderModels(
      catalog([
        row({
          id: "coder-q8",
          title: "Coder Q8",
          secondary_profile: true,
          description: "Coding profile with Q8 weights.",
          use_names: ["coder-q8", "coding-heavy"],
        }),
      ]),
    );
    await screen.findByRole("button", { name: /More chat profiles/ });
    fireEvent.change(screen.getByRole("textbox", { name: "Search models" }), {
      target: { value: "coding-heavy" },
    });
    expect(screen.getByRole("heading", { name: "Coder Q8" })).toBeVisible();
    expect(screen.queryByText("No models match these filters")).toBeNull();
  });

  it("uses music and sound-effect icons from server metadata and lists usable names in details", async () => {
    renderModels(
      catalog([
        row({
          id: "music",
          title: "Music",
          icon_key: "music",
          capability_title: "Music",
          use_names: ["music", "song", "ace-step"],
          capabilities: [
            {
              capability: "music",
              intent: "audio.generate",
              label: "Music",
              input_modalities: ["text"],
              output_modalities: ["audio"],
              streaming: false,
              available: true,
            },
          ],
        }),
        row({
          id: "sfx",
          title: "Sound effects",
          icon_key: "sfx",
          capability_title: "Sound effects",
          capabilities: [
            {
              capability: "sfx",
              intent: "audio.generate",
              label: "Sound effects",
              input_modalities: ["text"],
              output_modalities: ["audio"],
              streaming: false,
              available: true,
            },
          ],
        }),
      ]),
    );
    const music = within(
      (
        await screen.findByRole("button", { name: "Copy model name music" })
      ).closest("article")!,
    );
    const sfx = within(
      screen
        .getByRole("button", { name: "Copy model name sfx" })
        .closest("article")!,
    );
    expect(
      music
        .getByRole("img", { name: "Music" })
        .querySelector(".lucide-music-2"),
    ).not.toBeNull();
    expect(
      sfx
        .getByRole("img", { name: "Sound effects" })
        .querySelector(".lucide-waves"),
    ).not.toBeNull();
    fireEvent.click(music.getByRole("button", { name: "Details" }));
    const dialog = within(screen.getByRole("dialog"));
    expect(dialog.getByText("Names you can use")).toBeVisible();
    expect(dialog.getByText("song")).toBeVisible();
    expect(dialog.getByText("ace-step")).toBeVisible();
  });

  it("treats only explicit configured false as catalog-only and gives owners an engine action", async () => {
    renderModels(
      catalog([
        row({
          id: "catalog-chat",
          display_name: "Catalog chat",
          name: "Catalog chat",
          availability: {
            available: false,
            status: "unavailable",
            reason: "The service is not configured.",
            measured: true,
            enabled: true,
            configured: false,
            up: false,
            idle_startable: false,
            evidence: { source: "registry", kind: "unconfigured" },
          },
        }),
      ]),
      owner,
    );

    const copy = await screen.findByRole("button", {
      name: "Copy model name catalog-chat",
    });
    expect(
      screen.queryByRole("link", { name: "Connect an engine" }),
    ).toBeNull();
    expect(screen.getByText("Unavailable")).toBeVisible();
    expect(screen.queryByText("The service is not configured.")).toBeNull();
    expect(screen.queryByText("Catalog only")).toBeNull();
    fireEvent.click(
      within(copy.closest("article")!).getByRole("button", { name: "Details" }),
    );
    const action = screen.getByRole("link", { name: "Connect an engine" });
    expect(action).toHaveAttribute("href", "/ui/engines/");
    expect(
      within(screen.getByRole("dialog")).getByText("Catalog only"),
    ).toBeVisible();
  });

  it("keeps copying available for busy rows and sends remote catalog-only owners to providers from Details", async () => {
    renderModels(
      catalog([
        row({
          id: "busy-image",
          display_name: "Busy image",
          name: "Busy image",
          capabilities: [
            {
              intent: "image.generate",
              label: "Images",
              input_modalities: ["text"],
              output_modalities: ["image"],
              streaming: false,
              available: false,
            },
          ],
          availability: {
            available: false,
            status: "busy",
            reason: "A request is running.",
            measured: true,
            enabled: true,
            configured: true,
            up: true,
            idle_startable: false,
            evidence: { source: "health", observed_at_s: 1700000000 },
          },
        }),
        row({
          id: "remote-catalog",
          display_name: "Remote catalog",
          name: "Remote catalog",
          locality: "remote",
          provider: "Example provider",
          engine_label: "Example provider",
          availability: {
            available: false,
            status: "unavailable",
            reason: "The provider is not configured.",
            measured: true,
            enabled: true,
            configured: false,
            up: false,
            idle_startable: false,
            evidence: { source: "registry", kind: "unconfigured" },
          },
        }),
      ]),
      owner,
    );

    const busyCopy = await screen.findByRole("button", {
      name: "Copy model name busy-image",
    });
    const busyCard = within(busyCopy.closest("article")!);
    expect(
      busyCard.getByRole("button", { name: "Copy model name busy-image" }),
    ).toBeVisible();
    expect(busyCard.queryByRole("link", { name: "Check status" })).toBeNull();

    expect(
      screen.queryByRole("link", { name: "Connect a provider" }),
    ).toBeNull();
    const remoteCopy = screen.getByRole("button", {
      name: "Copy model name remote-catalog",
    });
    fireEvent.click(
      within(remoteCopy.closest("article")!).getByRole("button", {
        name: "Details",
      }),
    );
    const providerAction = screen.getByRole("link", {
      name: "Connect a provider",
    });
    expect(providerAction).toHaveAttribute("href", "/ui/providers/");
  });

  it("keeps omitted configuration fields as not reported and gives members a status action", async () => {
    const availability = {
      available: false,
      status: "unknown" as const,
      reason: "Not measured yet.",
      measured: false,
      evidence: {},
    };
    renderModels(catalog([row({ availability })]), member);

    const copy = await screen.findByRole("button", {
      name: "Copy model name chat",
    });
    expect(screen.queryByRole("link", { name: "Check status" })).toBeNull();
    expect(screen.queryByText("Unknown")).toBeNull();
    expect(screen.queryByText("Configuration not reported")).toBeNull();
    expect(screen.queryByText("Observation time not reported")).toBeNull();
    expect(screen.queryByText("Catalog only")).toBeNull();
    fireEvent.click(
      within(copy.closest("article")!).getByRole("button", { name: "Details" }),
    );
    expect(screen.getByRole("link", { name: "Check status" })).toHaveAttribute(
      "href",
      "/ui/status/",
    );
  });

  it("shows distinctions and one role action for unavailable direct model rows", async () => {
    renderModels(
      catalog(
        [],
        [
          row({
            id: "remote-model",
            object: "model_catalog.model",
            display_name: "Remote model",
            name: "Remote model",
            alias: false,
            locality: "remote",
            provider: "Example provider",
            engine_label: "Example provider",
            cost: { kind: "paid", price: "$0.10 per 1M tokens" },
            availability: {
              available: false,
              status: "unavailable",
              reason: "The provider is disabled.",
              measured: true,
              enabled: false,
              configured: true,
              up: false,
              idle_startable: false,
              evidence: { source: "registry", observed_at_s: 1700000000 },
            },
          }),
        ],
      ),
      owner,
    );

    fireEvent.click(await screen.findByRole("button", { name: /All models/ }));
    const table = within(screen.getByRole("table"));

    expect(table.getByText("Unavailable")).toBeVisible();
    expect(table.queryByText("From configuration")).toBeNull();
    expect(table.queryByText("Disabled")).toBeNull();
    expect(table.queryByText("Observed:")).toBeNull();
    expect(table.queryByRole("link", { name: "Review provider" })).toBeNull();
    expect(
      table.getByRole("button", { name: "Copy model name remote-model" }),
    ).toBeVisible();
    fireEvent.click(table.getByRole("button", { name: "Details" }));
    expect(
      screen.getByRole("link", { name: "Review provider" }),
    ).toHaveAttribute("href", "/ui/providers/");
  });
});
