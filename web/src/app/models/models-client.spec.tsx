import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
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
        capability: "chat",
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
  return client;
}

beforeEach(() => {
  vi.mocked(productApi.models).mockReset();
});

afterEach(() => {
  cleanup();
});

describe("models catalog refresh", () => {
  it.each([true, false])(
    "refreshes the open detail by id (alias: %s)",
    async (alias) => {
      const original = row({
        id: "refresh-model",
        alias,
        object: alias ? "model_catalog.alias" : "model_catalog.model",
        title: "Original title",
        description: "Original purpose.",
        locality: "remote",
        cost: { kind: "paid", price: "$1" },
      });
      const snapshot = (entry: CatalogRow) =>
        alias ? catalog([entry]) : catalog([], [entry]);
      const client = renderModels(snapshot(original));
      if (!alias) {
        fireEvent.click(await screen.findByRole("button", { name: "All models" }));
      }
      const copy = await screen.findByRole("button", {
        name: "Copy model name refresh-model",
      });
      fireEvent.click(
        within(copy.closest("tr")!).getByRole("button", {
          name: "Details",
        }),
      );
      expect(within(screen.getByRole("dialog")).getByText("Original purpose.")).toBeVisible();
      vi.mocked(productApi.models).mockResolvedValue(snapshot({
        ...original,
        title: "Updated title",
        description: "Updated purpose.",
        engine_label: "Updated engine",
        resolves_to: { chat: "updated-target" },
        cost: { kind: "paid", price: "$2" },
        availability: {
          ...original.availability,
          available: false,
          status: "unavailable",
        },
      }));
      await act(async () => {
        await client.refetchQueries({ queryKey: ["models", "catalog"] });
      });
      await waitFor(() => {
        const detail = within(screen.getByRole("dialog"));
        expect(detail.getByRole("heading", { name: "Updated title" })).toBeVisible();
        expect(detail.getByText("Updated purpose.")).toBeVisible();
        expect(detail.getByText("Updated engine")).toBeVisible();
        expect(detail.getByText("chat: updated-target")).toBeVisible();
        expect(detail.getByText("Paid · $2")).toBeVisible();
        expect(detail.getByText("Unavailable")).toBeVisible();
        expect(detail.queryByText("Original purpose.")).toBeNull();
      });
    },
  );

  it.each([true, false])(
    "closes a removed model with a note and does not reopen it (alias: %s)",
    async (alias) => {
      const original = row({
        id: "removed-model",
        alias,
        object: alias ? "model_catalog.alias" : "model_catalog.model",
      });
      const snapshot = alias ? catalog([original]) : catalog([], [original]);
      const client = renderModels(snapshot);
      if (!alias) {
        fireEvent.click(await screen.findByRole("button", { name: "All models" }));
      }
      const copy = await screen.findByRole("button", {
        name: "Copy model name removed-model",
      });
      fireEvent.click(
        within(copy.closest("tr")!).getByRole("button", {
          name: "Details",
        }),
      );
      expect(screen.getByRole("dialog")).toBeVisible();
      vi.mocked(productApi.models).mockResolvedValue(catalog([]));
      await act(async () => {
        await client.refetchQueries({ queryKey: ["models", "catalog"] });
      });
      await waitFor(() => {
        expect(screen.queryByRole("dialog")).toBeNull();
        expect(screen.getByRole("status")).toHaveTextContent(
          "This model is no longer in the catalog.",
        );
      });
      vi.mocked(productApi.models).mockResolvedValue(snapshot);
      await act(async () => {
        await client.refetchQueries({ queryKey: ["models", "catalog"] });
      });
      const restoredCopy = await screen.findByRole("button", {
        name: "Copy model name removed-model",
      });
      expect(screen.queryByRole("dialog")).toBeNull();
      fireEvent.click(
        within(restoredCopy.closest("tr")!).getByRole("button", {
          name: "Details",
        }),
      );
      expect(screen.getByRole("dialog")).toBeVisible();
      expect(screen.queryByText("This model is no longer in the catalog.")).toBeNull();
    },
  );
});

describe("models visible-text search", () => {
  it.each([
    ["title", "LANTERN"],
    ["purpose", "gentle explanations"],
    ["capability", "CONVERSATION"],
    ["id", "opaque-17"],
  ])("matches visible %s text and retains id search", async (_field, query) => {
    renderModels(catalog([
      row({ id: "default" }),
      row({
        id: "opaque-17",
        title: "Lantern",
        description: "Offers gentle explanations.",
        capability_title: "Conversation",
      }),
    ]));
    await screen.findByRole("button", { name: "Copy model name default" });
    fireEvent.click(screen.getByRole("button", { name: "1 more Chat models" }));
    expect(screen.getByRole("heading", { name: "Lantern" })).toBeVisible();
    expect(screen.getByText("Offers gentle explanations.")).toBeVisible();
    fireEvent.change(screen.getByRole("textbox", { name: "Search models" }), {
      target: { value: query },
    });
    expect(screen.getByRole("heading", { name: "Lantern" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Chat", level: 2 })).toBeVisible();
    expect(screen.getByRole("button", { name: "Copy model name opaque-17" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Copy model name default" })).toBeNull();
  });

  it("finds an exact model by its visible title", async () => {
    renderModels(catalog([], [
      row({
        id: "opaque-exact",
        alias: false,
        object: "model_catalog.model",
        title: "Firefly",
      }),
    ]));
    fireEvent.click(await screen.findByRole("button", { name: "All models" }));
    expect(within(screen.getByRole("table", { name: "All models" })).getByText("Firefly")).toBeVisible();
    fireEvent.change(screen.getByRole("textbox", { name: "Search models" }), {
      target: { value: "FIREFLY" },
    });
    expect(within(screen.getByRole("table", { name: "All models" })).getByRole("button", {
      name: "Copy model name opaque-exact",
    })).toBeVisible();
  });
});

describe("models catalog contract distinctions", () => {
  it("uses the catalog recommendation even when coding and roleplay come first", async () => {
    renderModels(
      catalog([
        row({ id: "coder", title: "Coder", recommended: false }),
        row({
          id: "rp",
          title: "Roleplay",
          secondary_profile: true,
          recommended: false,
        }),
        row({ id: "default", title: "Default", recommended: true }),
      ]),
    );
    expect(
      await screen.findByRole("button", { name: "Copy model name default" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Copy model name coder" }),
    ).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "2 more Chat models" }));
    for (const id of ["coder", "rp"]) {
      expect(
        screen.getByRole("button", { name: `Copy model name ${id}` }),
      ).toBeVisible();
    }
    expect(
      screen.queryByRole("button", { name: /More chat profiles/ }),
    ).toBeNull();
  });

  it("offers capability chips only for visible list sections", async () => {
    renderModels(
      catalog(
        [row()],
        [
          row({
            id: "generic-audio",
            alias: false,
            object: "model_catalog.model",
            capabilities: [
              {
                ...row().capabilities[0],
                capability: "audio",
                intent: "audio.generate",
                label: "Audio",
              },
            ],
          }),
        ],
      ),
    );
    await screen.findByRole("button", { name: "Copy model name chat" });
    expect(
      screen.getByRole("button", { name: /^Chat$/ }),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: /^Audio$/ }),
    ).toBeNull();
  });

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
    fireEvent.click(screen.getByRole("button", { name: "2 more Chat models" }));
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
      within(copy.closest("tr")!).getByRole("button", { name: "Details" }),
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
      await screen.findByRole("heading", { name: "Make music or sounds", level: 2 }),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "Speech" })).toBeNull();
  });

  it("keeps free remote placement honest and price filters quiet in the capability row", async () => {
    renderModels(catalog([row({ locality: "remote" })]));
    await screen.findByRole("button", { name: "Copy model name chat" });
    expect(screen.queryByText("Remote", { exact: true })).toBeNull();
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

  it("names embeddings by what they do, never 'Embeddings' or 'embedding'", async () => {
    renderModels(
      catalog([
        row({ id: "chat", capability: "chat", capability_title: "Chat" }),
        row({
          id: "embed",
          title: "Embed",
          display_name: "Embed",
          capability: "embed",
          capability_title: "Embeddings",
          architecture: { input_modalities: ["text"], output_modalities: ["embedding"] },
          capabilities: [{ ...row().capabilities[0], capability: "embed", label: "Embeddings" }],
        }),
      ]),
    );
    await screen.findByRole("button", { name: "Copy model name embed" });
    expect(
      screen.getByRole("heading", { name: "Turn text into numbers", level: 2 }),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Turn text into numbers" })).toBeVisible();
    const facts = screen.getAllByRole("list", { name: "Model facts" });
    expect(facts.map((list) => list.textContent).join(" ")).toMatch(/text.*numbers/);
    expect(document.body.textContent).not.toMatch(/Embeddings|\bembedding\b/i);
  });

  it("groups rows by the server classification, not by intent", async () => {
    const classified = (id: string, capability: string, label: string) =>
      row({
        id,
        capability,
        capability_title: label,
        // The row's capability comes from its /v1/models type. It decides
        // even where the first serving capability is a chat endpoint.
        capabilities: [{ ...row().capabilities[0], label }],
      });
    renderModels(
      catalog([
        classified("chat", "chat", "Chat"),
        classified("fast", "chat", "Chat"),
        classified("image", "image", "Images"),
        classified("decide", "decide", "Decisions"),
        classified("tts", "speech", "Speech"),
      ]),
    );
    await screen.findByRole("button", { name: "Copy model name chat" });
    const chatGroup = screen
      .getByRole("heading", { name: "Chat", level: 2 })
      .closest("tbody")!;
    expect(
      within(chatGroup).getByRole("button", { name: "1 more Chat models" }),
    ).toBeInTheDocument();
    expect(within(chatGroup).queryByText("image", { exact: true })).toBeNull();
    // Decisions read as the task they do, the name Try uses.
    for (const label of ["Make a picture", "Choose an option", "Read text aloud"]) {
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

  it("shows input and output modalities and capability tags on each row", async () => {
    renderModels(
      catalog(
        [
          row({
            type: "language",
            architecture: {
              input_modalities: ["text", "image"],
              output_modalities: ["text"],
            },
            tags: ["tool-use", "reasoning", "vision"],
          }),
        ],
        [
          row({
            id: "plain-chat",
            alias: false,
            object: "model_catalog.model",
            type: "language",
            architecture: {
              input_modalities: ["text"],
              output_modalities: ["text"],
            },
          }),
        ],
      ),
    );
    const copy = await screen.findByRole("button", {
      name: "Copy model name chat",
    });
    const facts = within(
      within(copy.closest("tr")!).getByRole("list", {
        name: "Model facts",
      }),
    );
    expect(facts.getByText("Reasoning")).toBeVisible();
    expect(facts.getByText("Tool use")).toBeVisible();
    expect(facts.getByText("Vision")).toBeVisible();
    expect(facts.getAllByRole("listitem")[0]).toHaveTextContent(
      "text, image→totext",
    );

    fireEvent.click(screen.getByRole("button", { name: "All models" }));
    const tableRow = screen
      .getByRole("button", { name: "Copy model name plain-chat" })
      .closest("tr")!;
    const tableFacts = within(tableRow).getByRole("list", {
      name: "Model facts",
    });
    expect(within(tableFacts).getAllByRole("listitem")).toHaveLength(1);
    expect(tableFacts).toHaveTextContent("text→totext");
  });

  it("shows no facts for a row /v1/models does not type", async () => {
    renderModels(catalog([row()]));
    const copy = await screen.findByRole("button", {
      name: "Copy model name chat",
    });
    expect(
      within(copy.closest("tr")!).queryByRole("list", {
        name: "Model facts",
      }),
    ).toBeNull();
  });

  it("keeps ready status quiet and puts description in Details", async () => {
    renderModels(catalog([row()]));

    const copy = await screen.findByRole("button", {
      name: "Copy model name chat",
    });
    const card = within(copy.closest("tr")!);

    expect(copy).toBeVisible();
    fireEvent.click(card.getByRole("button", { name: "Details" }));
    expect(productApi.models).toHaveBeenCalledOnce();
    expect(
      within(screen.getByRole("dialog")).getByText(
        "Chat through your configured model.",
      ),
    ).toBeVisible();
    expect(card.queryByText("Ready")).toBeNull();
    expect(card.queryByText("Recent request succeeded.")).toBeNull();
    expect(card.queryByText("Allowed by your access")).toBeNull();
    expect(card.queryByText("Observed:")).toBeNull();
    expect(card.queryByText("Not measured yet")).toBeNull();
    expect(card.queryByRole("button", { name: "Use" })).toBeNull();
    expect(copy).toBeVisible();
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
    const card = within(copy.closest("tr")!);

    fireEvent.click(card.getByRole("button", { name: "Details" }));
    expect(
      within(screen.getByRole("dialog")).getByText(
        "Use this when you want short replies.",
      ),
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
    const card = within(copy.closest("tr")!);

    fireEvent.click(card.getByRole("button", { name: "Details" }));
    expect(
      within(screen.getByRole("dialog")).getByText(
        "Thinks through questions before answering.",
      ),
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
    const card = within(copy.closest("tr")!);

    fireEvent.click(card.getByRole("button", { name: "Details" }));
    expect(
      within(screen.getByRole("dialog")).getByText(
        "Local server tuned for short replies.",
      ),
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
    const card = within(copy.closest("tr")!);
    expect(
      card.queryByText("Answers quickly for everyday prompts."),
    ).toBeNull();
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
    const card = within(copy.closest("tr")!);
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
    const card = within(copy.closest("tr")!);

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

    expect(await screen.findByRole("heading", { name: "Make music" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Read text aloud" })).toBeVisible();
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

    expect(await screen.findByRole("heading", { name: "Make music or sounds" })).toBeVisible();
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
    const table = within(screen.getByRole("table", { name: "All models" }));

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

    await screen.findByRole("button", { name: "Copy model name chat" });
    expect(screen.queryByText("Local · no provider charge")).toBeNull();
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

  it("keeps local unknown-cost rows outside the remote unknown category", async () => {
    renderModels(catalog([
      row({
        id: "local-unknown",
        locality: "local",
        cost: { kind: "unknown", price: null },
      }),
      row({
        id: "remote-unknown",
        locality: "remote",
        cost: { kind: "unknown", price: null },
      }),
    ]));
    const localCopy = await screen.findByRole("button", {
      name: "Copy model name local-unknown",
    });
    expect(within(localCopy.closest("tr")!).queryByText("Cost not recorded")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "1 more Chat models" }));
    fireEvent.click(screen.getByRole("button", { name: "Cost not recorded" }));
    expect(localCopy).toBeVisible();
    expect(screen.queryByRole("button", { name: "Copy model name remote-unknown" })).toBeNull();
  });

  it(
    "labels unknown remote costs in primary rows, alternatives, details, and tables",
    async () => {
      const unknown = row({
        locality: "remote",
        cost: { kind: "unknown", price: null },
      });
      renderModels(catalog(
        [unknown, { ...unknown, id: "unknown-alternative" }],
        [{
          ...unknown,
          id: "unknown-exact",
          alias: false,
          object: "model_catalog.model",
        }],
      ));
      const copy = await screen.findByRole("button", {
        name: "Copy model name chat",
      });
      const card = within(copy.closest("tr")!);
      expect(card.getByText("Cost not recorded")).toBeVisible();
      fireEvent.click(screen.getByRole("button", { name: "1 more Chat models" }));
      const alternative = screen.getByRole("button", {
        name: "Copy model name unknown-alternative",
      }).closest("tr")!;
      expect(within(alternative).getByText("Cost not recorded")).toBeVisible();
      fireEvent.click(card.getByRole("button", { name: "Details" }));
      expect(within(screen.getByRole("dialog")).getByText("Cost not recorded")).toBeVisible();
      fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", {
        name: "Close",
      }));
      fireEvent.click(screen.getByRole("button", { name: "All models" }));
      const tableRow = screen.getByRole("button", {
        name: "Copy model name unknown-exact",
      }).closest("tr")!;
      expect(within(tableRow).getByText("Cost not recorded")).toBeVisible();
    },
  );

  it(
    "filters unknown remote costs independently and restores them with Clear filters",
    async () => {
      const unknown = row({
        id: "unknown-cost",
        locality: "remote",
        cost: { kind: "unknown", price: null },
      });
      renderModels(catalog(
        [
          unknown,
          row({ id: "free", locality: "remote" }),
          row({ id: "paid", locality: "remote", cost: { kind: "paid", price: "$1" } }),
        ],
        [{
          ...unknown,
          id: "unknown-exact",
          alias: false,
          object: "model_catalog.model",
        }],
      ));
      await screen.findByRole("button", { name: "Copy model name unknown-cost" });
      fireEvent.click(screen.getByRole("button", { name: "All models" }));
      const priceFilters = within(screen.getByRole("group", {
        name: "Price filters (choose any)",
      }));
      const unknownFilter = priceFilters.getByRole("button", {
        name: "Cost not recorded",
      });
      expect(unknownFilter).toHaveAttribute("aria-pressed", "true");
      fireEvent.click(priceFilters.getByRole("button", { name: "No provider charge" }));
      fireEvent.click(priceFilters.getByRole("button", { name: "Paid" }));
      for (const id of ["unknown-cost", "unknown-exact"]) {
        expect(screen.getByRole("button", { name: `Copy model name ${id}` })).toBeVisible();
      }
      fireEvent.click(unknownFilter);
      expect(unknownFilter).toHaveAttribute("aria-pressed", "false");
      for (const id of ["unknown-cost", "unknown-exact"]) {
        expect(screen.queryByRole("button", { name: `Copy model name ${id}` })).toBeNull();
      }
      expect(screen.getByText("No models match these filters")).toBeVisible();
      fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
      expect(unknownFilter).toHaveAttribute("aria-pressed", "true");
      fireEvent.click(screen.getByRole("button", { name: "2 more Chat models" }));
      for (const id of ["unknown-cost", "unknown-exact", "free", "paid"]) {
        expect(screen.getByRole("button", { name: `Copy model name ${id}` })).toBeVisible();
      }
    },
  );

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
    expect(screen.queryByText("Local · no provider charge")).toBeNull();
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

  it("merges primary and secondary profiles into one Chat alternative list", async () => {
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
    const more = screen.getByRole("button", { name: "4 more Chat models" });
    expect(more).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("heading", { name: "Coder Q5" })).toBeNull();
    fireEvent.click(more);
    expect(screen.getByText("fast")).toBeVisible();
    expect(screen.getByText("coder")).toBeVisible();
    expect(screen.getByRole("heading", { name: "Coder Q5" })).toBeVisible();
    expect(screen.getByText("Coding profile with Q5 weights.")).toBeVisible();
    expect(screen.getByText("Chat profile for roleplay.")).toBeVisible();
    expect(
      screen.queryByText("Chat model visible to your access plan."),
    ).toBeNull();
  });

  it("shows direct secondary profiles in Chat and keeps them in All models", async () => {
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
    await screen.findByRole("button", { name: "Copy model name coder-q8" });
    expect(
      screen.getByRole("heading", { name: "Chat", level: 2 }),
    ).toBeVisible();
    expect(screen.getByRole("heading", { name: "Coder Q8" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "All models" }));
    expect(
      within(screen.getByRole("table", { name: "All models" })).getByText("Coder Q8"),
    ).toBeVisible();
    fireEvent.click(
      within(screen.getByRole("table", { name: "All models" })).getByRole("button", {
        name: "Details",
      }),
    );
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
    await screen.findByRole("button", { name: "Copy model name coder-q8" });
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
      ).closest("tr")!,
    );
    const sfx = within(
      screen
        .getByRole("button", { name: "Copy model name sfx" })
        .closest("tr")!,
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
      within(copy.closest("tr")!).getByRole("button", { name: "Details" }),
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
              capability: "image",
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
    const busyCard = within(busyCopy.closest("tr")!);
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
      within(remoteCopy.closest("tr")!).getByRole("button", {
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
      within(copy.closest("tr")!).getByRole("button", { name: "Details" }),
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
    const table = within(screen.getByRole("table", { name: "All models" }));

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

describe("model cost and clipboard recovery", () => {
  it.each([
    { price: null, label: "Paid · price unavailable" },
    { price: 0, label: "Paid · 0" },
  ])(
    "preserves paid classification for price $price across cards, details, and tables",
    async ({ price, label }) => {
      renderModels(
        catalog(
          [row({ locality: "remote", cost: { kind: "paid", price } })],
          [
            row({
              id: "paid-exact",
              alias: false,
              object: "model_catalog.model",
              locality: "remote",
              cost: { kind: "paid", price },
            }),
          ],
        ),
      );
      const copy = await screen.findByRole("button", {
        name: "Copy model name chat",
      });
      const card = within(copy.closest("tr")!);
      expect(card.getByText(label)).toBeVisible();
      fireEvent.click(card.getByRole("button", { name: "Details" }));
      expect(within(screen.getByRole("dialog")).getByText(label)).toBeVisible();
      fireEvent.click(
        within(screen.getByRole("dialog")).getByRole("button", {
          name: "Close",
        }),
      );
      fireEvent.click(screen.getByRole("button", { name: "All models" }));
      expect(within(screen.getByRole("table", { name: "All models" })).getByText(label)).toBeVisible();
    },
  );

  it("announces rejected clipboard access and exposes the full name for manual copy", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("Permission denied"));
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const id = "provider/a-long-model-name-that-needs-manual-copy";
    renderModels(catalog([row({ id })]));
    fireEvent.click(
      await screen.findByRole("button", { name: `Copy model name ${id}` }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Copy failed. Select the model name and copy it manually.",
    );
    expect(
      screen.getByRole("textbox", { name: "Model name for manual copy" }),
    ).toHaveValue(id);
    expect(writeText).toHaveBeenCalledWith(id);
  });
});

describe("models query-string deep links", () => {
  let previousUrl: string;

  beforeEach(() => {
    previousUrl = window.location.href;
  });

  afterEach(() => {
    window.history.replaceState(null, "", previousUrl);
  });

  it("applies a capability on load and falls back for an unknown capability", async () => {
    const snapshot = catalog([
      row(),
      row({
        id: "image",
        capabilities: [
          { ...row().capabilities[0], capability: "image", label: "Images" },
        ],
      }),
    ]);
    window.history.replaceState(null, "", "?capability=image&model=unknown");
    renderModels(snapshot);
    expect(
      await screen.findByRole("button", { name: "Images", pressed: true }),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Copy model name image" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Copy model name chat" }),
    ).toBeNull();
    cleanup();
    window.history.replaceState(null, "", "?capability=unknown");
    renderModels(snapshot);
    expect(
      await screen.findByRole("button", { name: "All", pressed: true }),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Copy model name chat" }),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Copy model name image" }),
    ).toBeVisible();
  });

  it("applies an alias model on load and falls back for an unknown model", async () => {
    const snapshot = catalog([row(), row({ id: "special" })]);
    window.history.replaceState(null, "", "?model=special&capability=unknown");
    renderModels(snapshot);
    expect(
      await screen.findByRole("button", { name: "Copy model name special" }),
    ).toBeVisible();
    expect(screen.getByRole("textbox", { name: "Search models" })).toHaveValue("special");
    expect(
      screen.queryByRole("button", { name: "Copy model name chat" }),
    ).toBeNull();
    fireEvent.change(screen.getByRole("textbox", { name: "Search models" }), {
      target: { value: "" },
    });
    expect(
      screen.getByRole("button", { name: "Copy model name chat" }),
    ).toBeVisible();
    cleanup();
    window.history.replaceState(null, "", "?model=unknown");
    renderModels(snapshot);
    expect(
      await screen.findByRole("button", { name: "Copy model name chat" }),
    ).toBeVisible();
    expect(screen.getByRole("textbox", { name: "Search models" })).toHaveValue("");
    expect(screen.getByRole("button", { name: "All models" })).toHaveAttribute(
      "aria-expanded", "false",
    );
  });

  it("decodes an exact model deep link and opens its filtered table", async () => {
    const id = "provider/model+v2";
    window.history.replaceState(null, "", `?model=${encodeURIComponent(id)}`);
    renderModels(catalog([row()], [
      row({ id, alias: false, object: "model_catalog.model" }),
      row({ id: "other-model", alias: false, object: "model_catalog.model" }),
    ]));
    const table = within(await screen.findByRole("table", { name: "All models" }));
    expect(
      table.getByRole("button", { name: `Copy model name ${id}` }),
    ).toBeVisible();
    expect(
      table.queryByRole("button", { name: "Copy model name other-model" }),
    ).toBeNull();
    expect(screen.getByRole("textbox", { name: "Search models" })).toHaveValue(id);
    expect(screen.getByRole("button", { name: "All models" })).toHaveAttribute(
      "aria-expanded", "true",
    );
  });
});


describe("task-first catalog rows", () => {
  it("hides wholly empty cost and status columns and explains local cost once", async () => {
    renderModels(catalog([row(), row({ id: "fast" })]));
    const table = within(await screen.findByRole("table", { name: "Available model aliases" }));
    expect(table.queryByRole("columnheader", { name: "Cost" })).toBeNull();
    expect(table.queryByRole("columnheader", { name: "Status" })).toBeNull();
    expect(table.queryByRole("columnheader", { name: "Use" })).toBeNull();
    expect(table.getByRole("columnheader", { name: "Actions" })).toBeInTheDocument();
    expect(screen.getAllByText("Local models without a listed cost: Free (runs here)")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "1 more Chat models" }));
    expect(screen.getAllByText("Local models without a listed cost: Free (runs here)")).toHaveLength(1);
    expect(table.getAllByRole("button", { name: "Details" })).toHaveLength(2);
  });

  it("keeps mixed cost and status columns aligned and removes them after filtering", async () => {
    renderModels(catalog([
      row(),
      row({
        id: "remote-paid",
        locality: "remote",
        cost: { kind: "paid", price: "$1" },
        availability: { ...row().availability, status: "busy" },
      }),
    ]));
    const table = within(await screen.findByRole("table", { name: "Available model aliases" }));
    expect(table.getByRole("columnheader", { name: "Cost" })).toBeInTheDocument();
    expect(table.getByRole("columnheader", { name: "Status" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "1 more Chat models" }));
    const rows = table.getAllByRole("row");
    expect(within(rows[1]).getAllByRole("cell")).toHaveLength(6);
    expect(within(rows[2]).getAllByRole("cell")).toHaveLength(6);
    expect(within(rows[2]).getByText("Paid · $1")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Paid" }));
    expect(table.queryByRole("columnheader", { name: "Cost" })).toBeNull();
    expect(table.queryByRole("columnheader", { name: "Status" })).toBeNull();
  });

  it.each(["unknown", "not_measured"] as const)(
    "hides empty %s summary status in aliases and exact models without claiming local cost",
    async (status) => {
      const remote = row({
        locality: "remote",
        availability: { ...row().availability, measured: false, status: status as CatalogRow["availability"]["status"] },
      });
      renderModels(catalog([remote], [{ ...remote, id: "exact", alias: false, object: "model_catalog.model" }]));
      await screen.findByRole("button", { name: "Copy model name chat" });
      fireEvent.click(screen.getByRole("button", { name: "All models" }));
      for (const name of ["Available model aliases", "All models"]) {
        const table = within(screen.getByRole("table", { name }));
        expect(table.queryByRole("columnheader", { name: "Cost" })).toBeNull();
        expect(table.queryByRole("columnheader", { name: "Status" })).toBeNull();
      }
      expect(screen.queryByText("Local models without a listed cost: Free (runs here)")).toBeNull();
    },
  );

  it("leads primary, alternative and exact rows with shared task names above the alias", async () => {
    const speech = row({
      id: "voice",
      title: "Voice",
      capability: "speech",
      capability_title: "Speech",
    });
    renderModels(catalog([speech, { ...speech, id: "voice-fast" }], [
      { ...speech, id: "voice-exact", alias: false, object: "model_catalog.model" },
    ]));
    await screen.findByRole("button", { name: "Copy model name voice" });
    fireEvent.click(screen.getByRole("button", { name: "1 more Speech models" }));
    fireEvent.click(screen.getByRole("button", { name: "All models" }));
    for (const id of ["voice", "voice-fast", "voice-exact"]) {
      const copy = screen.getByRole("button", { name: `Copy model name ${id}` });
      const firstCell = copy.closest("tr")!.querySelector("td")!;
      expect(firstCell).toContainElement(copy);
      expect(firstCell).toHaveTextContent("Read text aloud");
      expect(firstCell).toHaveTextContent(id);
    }
    fireEvent.change(screen.getByRole("textbox", { name: "Search models" }), {
      target: { value: "Read text aloud" },
    });
    expect(screen.getByRole("button", { name: "Copy model name voice" })).toBeVisible();
  });

  it("uses two phone lines with inline cost and status in every catalog row", async () => {
    const paid = row({
      locality: "remote",
      cost: { kind: "paid", price: "$1" },
      availability: { ...row().availability, status: "busy" },
    });
    renderModels(catalog([paid, { ...paid, id: "alternative" }], [
      { ...paid, id: "exact", alias: false, object: "model_catalog.model" },
    ]));
    await screen.findByRole("button", { name: "Copy model name chat" });
    fireEvent.click(screen.getByRole("button", { name: "1 more Chat models" }));
    fireEvent.click(screen.getByRole("button", { name: "All models" }));
    for (const id of ["chat", "alternative", "exact"]) {
      const copy = screen.getByRole("button", { name: `Copy model name ${id}` });
      const modelRow = copy.closest("tr")!;
      expect(modelRow).toHaveClass("max-sm:grid", "max-sm:grid-rows-2", "max-sm:h-(--row-phone-2)");
      expect(copy.closest("td")).toHaveClass("max-sm:contents");
      expect(copy.parentElement).toHaveClass("max-sm:row-start-2");
      expect(within(modelRow).getByText("Paid · $1")).toHaveClass("max-sm:row-start-2");
      const cells = within(modelRow).getAllByRole("cell");
      expect(cells[cells.length - 2]).toHaveClass("max-sm:row-start-2");
      expect(within(modelRow).getByRole("button", { name: "Details" })).toBeInTheDocument();
    }
  });
});
