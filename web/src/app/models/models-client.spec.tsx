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
  it("uses product alias fallback and one availability line for allowed configured observed rows", async () => {
    renderModels(catalog([row()]));

    const copy = await screen.findByRole("button", { name: "Copy alias chat" });
    const card = within(copy.closest("article")!);

    expect(productApi.models).toHaveBeenCalledOnce();
    expect(card.getByText("Chat through your configured model.")).toBeVisible();
    expect(card.getByText("Ready")).toBeVisible();
    expect(card.getByText("Recent request succeeded.")).toBeVisible();
    expect(card.queryByText("Allowed by your access")).toBeNull();
    expect(card.queryByText("Observed:")).toBeNull();
    expect(card.queryByText("Not measured yet")).toBeNull();
    expect(card.getByRole("button", { name: "Use" })).toBeVisible();
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

    const copy = await screen.findByRole("button", { name: "Copy alias fast" });
    const card = within(copy.closest("article")!);

    expect(
      card.getByText("Use this when you want short replies."),
    ).toBeVisible();
    expect(
      card.queryByText("Fast chat alias for lower-latency replies."),
    ).toBeNull();
  });

  it("uses alias fallback when backend copy is generic", async () => {
    renderModels(
      catalog([
        row({
          id: "chat",
          description: "Chat model visible to your access plan.",
        }),
      ]),
    );

    const copy = await screen.findByRole("button", { name: "Copy alias chat" });
    const card = within(copy.closest("article")!);

    expect(
      card.getByText(
        "General chat alias for longer answers and reasoning-capable models.",
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

    const copy = await screen.findByRole("button", { name: "Copy alias fast" });
    const card = within(copy.closest("article")!);

    expect(
      card.getByText("Local server tuned for short replies."),
    ).toBeVisible();
    expect(
      card.queryByText("Fast chat alias for lower-latency replies."),
    ).toBeNull();
  });

  it("filters descriptions that duplicate the engine label and hides local engine labels from summaries", async () => {
    renderModels(
      catalog([
        row({
          description: "Local llama.cpp server (official; Bee rollback alias)",
          engine_label: "Local llama.cpp server (official; Bee rollback alias)",
        }),
      ]),
    );

    const copy = await screen.findByRole("button", { name: "Copy alias chat" });
    const card = within(copy.closest("article")!);

    expect(card.getByText("Local engine")).toBeVisible();
    expect(
      card.getByText(
        "General chat alias for longer answers and reasoning-capable models.",
      ),
    ).toBeVisible();
    expect(card.queryByText(/llama\.cpp/i)).toBeNull();
  });

  it("keeps provider provenance for remote summaries", async () => {
    renderModels(
      catalog([
        row({
          locality: "remote",
          provider: "OpenRouter",
          engine_label: "Remote provider",
        }),
      ]),
    );

    const copy = await screen.findByRole("button", { name: "Copy alias chat" });
    const card = within(copy.closest("article")!);

    expect(card.getByText("OpenRouter")).toBeVisible();
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
          engine_label: "Speech (Chatterbox)",
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
    expect(screen.getByText("Music (ACE-Step XL Turbo)")).toBeVisible();
    expect(screen.getByText("Speech (Chatterbox)")).toBeVisible();
  });

  it("falls back audio.generate legacy intent to audio instead of speech", async () => {
    renderModels(
      catalog([
        row({
          id: "legacy-audio",
          display_name: "Legacy audio",
          name: "legacy-audio",
          capabilities: [
            {
              intent: "audio.generate",
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

  it("maps rerank capability into the embed icon family and table label", async () => {
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
        name: "Copy alias local-unknown-cost",
      }),
    ).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "No provider charge" }));

    expect(
      screen.queryByRole("button", { name: "Copy alias local-unknown-cost" }),
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
      await screen.findByRole("button", { name: "Copy alias local-paid-cost" }),
    ).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Paid" }));

    expect(
      screen.getByRole("button", { name: "Copy alias local-paid-cost" }),
    ).toBeVisible();
    expect(screen.getByText("Local · no provider charge")).toBeVisible();
  });

  it("uses a coding title for coder aliases without a friendly name", async () => {
    renderModels(
      catalog([
        row({
          id: "coder",
          display_name: "coder",
          name: "coder",
          description: "Chat model visible to your access plan.",
        }),
      ]),
    );

    expect(await screen.findByText("Coding chat")).toBeVisible();
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

    const action = await screen.findByRole("link", {
      name: "Connect an engine",
    });
    expect(action).toHaveAttribute("href", "/ui/engines/");
    expect(screen.getByText("Unavailable")).toBeVisible();
    expect(screen.getByText("The service is not configured.")).toBeVisible();
    expect(screen.queryByText("Catalog only")).toBeNull();
    expect(screen.queryByRole("button", { name: "Use" })).toBeNull();
  });

  it("keeps Use for busy rows and sends remote catalog-only owners to providers", async () => {
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
      name: "Copy alias busy-image",
    });
    const busyCard = within(busyCopy.closest("article")!);
    expect(busyCard.getByRole("button", { name: "Use" })).toBeVisible();
    expect(busyCard.queryByRole("link", { name: "Check status" })).toBeNull();

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

    const action = await screen.findByRole("link", { name: "Check status" });
    expect(action).toHaveAttribute("href", "/ui/status/");
    expect(screen.getByText("Unknown")).toBeVisible();
    expect(screen.queryByText("Configuration not reported")).toBeNull();
    expect(screen.queryByText("Observation time not reported")).toBeNull();
    expect(screen.queryByText("Catalog only")).toBeNull();
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
    expect(
      table.getByRole("link", { name: "Review provider" }),
    ).toHaveAttribute("href", "/ui/providers/");
    expect(table.queryByRole("link", { name: "Review engine" })).toBeNull();
  });
});
