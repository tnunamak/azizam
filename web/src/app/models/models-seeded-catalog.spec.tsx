// Display states of the Models page that used to run in the browser
// (e2e/models.spec.ts, e2e/models-polish2.spec.ts). The catalog payloads are
// copied from those specs. Real geometry (phone rows, overflow) and axe stay
// in the browser specs.
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
import { productApi, type ModelsCatalog } from "@/lib/product-api";

vi.mock("@/lib/product-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/product-api")>();
  return {
    ...actual,
    productApi: { ...actual.productApi, models: vi.fn() },
  };
});

const member: Viewer = {
  authenticated: true,
  name: "Member",
  email: "member@example.test",
  role: "member",
  admin: false,
};

const seededCatalog = {
  schema_version: 1,
  object: "models.catalog",
  aliases: [
    {
      id: "chat",
      object: "model_catalog.alias",
      display_name: "chat",
      alias: true,
      operations: ["chat"],
      resolves_to: { chat: "demo-chat" },
      description: "Best default for short conversations and app setup tests.",
      type: "language",
      architecture: {
        input_modalities: ["text", "image"],
        output_modalities: ["text"],
      },
      tags: ["tool-use", "reasoning", "vision"],
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
      provider: "Local test engine",
      locality: "local",
      cost: { kind: "free", price: null },
      timing: { typical_seconds: 0.8, source: "receipts", sample_count: 2 },
      availability: {
        status: "configured",
        available: true,
        reason_code: "configured",
        measured: false,
      },
      io: { input: ["text"], output: ["text"] },
      examples: [
        {
          kind: "chat",
          body: {
            model: "chat",
            messages: [{ role: "user", content: "Hello" }],
          },
        },
      ],
      recent_receipts: [
        {
          request_id: "req_1",
          href: "/usage/?receipt=req_1",
          status: "succeeded",
          latency_ms: 820,
        },
      ],
      use: {
        model: "chat",
        curl: 'curl $AZIZAM_BASE/v1/chat/completions -H "Authorization: Bearer $AZIZAM_API_KEY" -H "Content-Type: application/json" -d \'{"model":"chat","messages":[{"role":"user","content":"Hello"}]}\'',
        python: { path: "/v1/chat/completions", model: "chat" },
        javascript: { path: "/v1/chat/completions", model: "chat" },
      },
    },
    {
      id: "image",
      object: "model_catalog.alias",
      display_name: "image",
      alias: true,
      operations: ["image"],
      resolves_to: { image: "provider/image-fast" },
      description: "Image generation through the configured remote provider.",
      type: "image",
      architecture: {
        input_modalities: ["text"],
        output_modalities: ["image"],
      },
      capabilities: [
        {
          capability: "image",
          intent: "image.generate",
          label: "Images",
          input_modalities: ["text"],
          output_modalities: ["image"],
          available: true,
        },
      ],
      provider: "provider-a",
      locality: "remote",
      cost: { kind: "paid", price: "$0.04 per image" },
      timing: { typical_seconds: null, source: null },
      availability: {
        available: false,
        reason: "Two requests are ahead of yours.",
        measured: true,
        status: "busy",
      },
      io: { input: ["text"], output: ["image"] },
      examples: [
        {
          kind: "image",
          body: { model: "image", prompt: "A small red cabin" },
        },
      ],
      recent_receipts: [],
      use: {
        model: "image",
        curl: 'curl $AZIZAM_BASE/v1/images/generations -H "Authorization: Bearer $AZIZAM_API_KEY" -H "Content-Type: application/json" -d \'{"model":"image","prompt":"A small red cabin"}\'',
        python: { path: "/v1/images/generations", model: "image" },
        javascript: { path: "/v1/images/generations", model: "image" },
      },
    },
    {
      id: "decide",
      object: "model_catalog.alias",
      display_name: "decide",
      alias: true,
      operations: ["decide"],
      resolves_to: { decide: "local/decider" },
      description: "On-demand decision model for routing choices.",
      capabilities: [
        {
          capability: "decide",
          intent: "decision.systemone",
          label: "Decide",
          input_modalities: ["text"],
          output_modalities: ["json"],
          available: true,
        },
      ],
      provider: "Local test engine",
      locality: "local",
      cost: { kind: "free", price: null },
      timing: { typical_seconds: null, source: null },
      availability: {
        available: true,
        reason_code: "ready_on_request",
        measured: false,
        status: "idle_startable",
      },
      io: { input: ["text"], output: ["json"] },
      examples: [],
      recent_receipts: [],
      use: {
        model: "decide",
        python: { path: "/v1/systemone", model: "decide" },
        javascript: { path: "/v1/systemone", model: "decide" },
      },
    },
    {
      id: "tts",
      object: "model_catalog.alias",
      display_name: "tts",
      alias: true,
      operations: ["speech"],
      resolves_to: { speech: "local/speech" },
      description: "Speech synthesis when a backend is configured.",
      capabilities: [
        {
          capability: "speech",
          intent: "speech.synthesis",
          label: "Speech",
          input_modalities: ["text"],
          output_modalities: ["audio"],
          available: true,
        },
      ],
      provider: "Local test engine",
      locality: "local",
      cost: { kind: "free", price: null },
      timing: { typical_seconds: null, source: null },
      availability: {
        available: true,
        reason_code: "unknown",
        measured: true,
        status: "unknown",
      },
      io: { input: ["text"], output: ["audio"] },
      examples: [],
      recent_receipts: [],
      use: {
        model: "tts",
        python: { path: "/v1/audio/speech", model: "tts" },
        javascript: { path: "/v1/audio/speech", model: "tts" },
      },
    },
  ],
  models: [
    {
      id: "demo-chat",
      object: "model_catalog.model",
      display_name: "demo-chat",
      alias: false,
      operations: [],
      resolves_to: {},
      description: "Low latency chat model",
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
      provider: "Local test engine",
      locality: "local",
      cost: { kind: "free", price: null },
      timing: { typical_seconds: 0.8, source: "receipts", sample_count: 2 },
      availability: {
        status: "configured",
        available: true,
        reason_code: "configured",
        measured: false,
      },
      io: { input: ["text"], output: ["text"] },
      examples: [],
      recent_receipts: [],
      use: {
        model: "demo-chat",
        curl: 'curl $AZIZAM_BASE/v1/chat/completions -H "Authorization: Bearer $AZIZAM_API_KEY"',
        python: { path: "/v1/chat/completions", model: "demo-chat" },
        javascript: { path: "/v1/chat/completions", model: "demo-chat" },
      },
    },
    {
      id: "provider/image-fast",
      object: "model_catalog.model",
      display_name: "provider/image-fast",
      alias: false,
      operations: [],
      resolves_to: {},
      description: null,
      capabilities: [
        {
          capability: "image",
          intent: "image.generate",
          label: "Images",
          input_modalities: ["text"],
          output_modalities: ["image"],
          available: true,
        },
      ],
      provider: "provider-a",
      locality: "remote",
      cost: { kind: "paid", price: "$0.04 per image" },
      timing: { typical_seconds: null, source: null },
      availability: {
        available: false,
        reason: "Two requests are ahead of yours.",
        measured: true,
        status: "busy",
      },
      io: { input: ["text"], output: ["image"] },
      examples: [],
      recent_receipts: [],
      use: {
        model: "provider/image-fast",
        curl: 'curl $AZIZAM_BASE/v1/images/generations -H "Authorization: Bearer $AZIZAM_API_KEY"',
        python: {
          path: "/v1/images/generations",
          model: "provider/image-fast",
        },
        javascript: {
          path: "/v1/images/generations",
          model: "provider/image-fast",
        },
      },
    },
  ],
  counts: { aliases: 4, models: 2, capabilities: 4 },
};

const emptyCatalog = {
  schema_version: 1,
  object: "models.catalog",
  aliases: [],
  models: [],
  counts: { aliases: 0, models: 0, capabilities: 0 },
};

const measured = {
  id: "chat",
  object: "model_catalog.alias",
  display_name: "Default",
  title: "Default",
  capability_title: "Chat",
  icon_key: "chat",
  use_names: ["chat", "internal-chat-model"],
  secondary_profile: false,
  alias: true,
  operations: ["chat"],
  resolves_to: { chat: "internal-chat-model" },
  description: "Everyday chat on your hardware.",
  capabilities: [
    {
      intent: "chat.completions",
      capability: "chat",
      icon_key: "chat",
      label: "Chat",
    },
  ],
  provider: "capture-engine",
  engine_label: "Household chat",
  locality: "local",
  cost: { kind: "free", price: null },
  timing: { typical_seconds: 0.151, source: "receipts", sample_count: 3 },
  availability: {
    available: true,
    measured: false,
    reason: "Live availability is not measured yet",
  },
  io: { input: ["text"], output: ["text"] },
  examples: [],
  recent_receipts: [],
  use: { model: "chat" },
};

const typical = {
  ...measured,
  id: "image",
  display_name: "Images",
  title: "Images",
  capability_title: "Images",
  icon_key: "image",
  use_names: ["image", "internal-image-workflow"],
  operations: ["image"],
  resolves_to: { image: "internal-image-workflow" },
  description: "Images through your chosen provider.",
  capabilities: [
    {
      intent: "image.generate",
      capability: "image",
      icon_key: "image",
      label: "Images",
    },
  ],
  provider: "provider-opaque-id",
  engine_label: "Example image provider",
  locality: "remote",
  cost: { kind: "paid", price: "$0.04 per image" },
  timing: { typical_seconds: 8, source: "configured", sample_count: 0 },
  availability: {
    available: true,
    measured: true,
    status: "ready",
    reason: "Ready for your next request.",
  },
  io: { input: ["text"], output: ["image"] },
  use: { model: "image" },
};

function aliasesOnly(aliases: unknown[]) {
  return { schema_version: 1, object: "models.catalog", aliases, models: [] };
}

function renderModels(snapshot: unknown) {
  vi.mocked(productApi.models).mockResolvedValue(snapshot as ModelsCatalog);
  render(
    <ViewerContext.Provider value={member}>
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
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

function openDetails(rowgroup: HTMLElement) {
  fireEvent.click(within(rowgroup).getAllByRole("button", { name: "Details" })[0]);
  return screen.getByRole("dialog");
}

describe("seeded catalog", () => {
  it("shows task headings, row facts, honest cost and quiet status", async () => {
    renderModels(seededCatalog);
    await screen.findByRole("button", { name: "Copy model name chat" });
    for (const name of ["Make a picture", "Choose an option", "Read text aloud"]) {
      expect(screen.getByRole("heading", { name })).toBeVisible();
    }
    const chat = screen.getByRole("rowgroup", { name: "Chat models" });
    const facts = within(chat).getByRole("list", { name: "Model facts" });
    expect(within(facts).getByText("Reasoning")).toBeVisible();
    expect(within(facts).getByText("Vision")).toBeVisible();
    expect(within(facts).getAllByRole("listitem")[0]).toHaveTextContent(
      "text, image→totext",
    );
    expect(
      screen.getByRole("button", { name: "Copy model name image" }),
    ).toBeVisible();
    // Rows make no local-cost claim; the page note below the table may.
    expect(
      within(screen.getByLabelText("Available model names", { selector: "div" })).queryByText(
        "Local · no provider charge",
        { exact: true },
      ),
    ).toBeNull();
    expect(
      screen.getByRole("button", { name: "No provider charge" }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByText("Ready", { exact: true })).toBeNull();
    expect(screen.getByText("Ready to start", { exact: true })).toBeVisible();
    expect(screen.queryByText("Unknown", { exact: true })).toBeNull();
    expect(
      screen.queryByText("Ready to start when you make a request."),
    ).toBeNull();
    expect(screen.getByText("Paid · $0.04 per image", { exact: true })).toBeVisible();
  });

  it("explains a start-on-request model in Details and never prints Unknown", async () => {
    renderModels(seededCatalog);
    await screen.findByRole("button", { name: "Copy model name chat" });
    const decision = screen.getByRole("rowgroup", { name: "Choose an option models" });
    expect(
      within(openDetails(decision)).getByText(
        "Ready to start when you make a request.",
      ),
    ).toBeVisible();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    const speech = screen.getByRole("rowgroup", { name: "Speech models" });
    expect(
      within(openDetails(speech)).queryByText("Unknown", { exact: true }),
    ).toBeNull();
  });
});

describe("empty and error catalog", () => {
  it("says no models are available and links back to setup", async () => {
    renderModels(emptyCatalog);
    expect(await screen.findByText("No models are available yet")).toBeVisible();
    expect(screen.getByRole("link", { name: "Review setup" })).toHaveAttribute(
      "href",
      "/",
    );
  });

  it("says the catalog could not be loaded", async () => {
    vi.mocked(productApi.models).mockRejectedValue(
      new Error("Unavailable for this test"),
    );
    render(
      <ViewerContext.Provider value={member}>
        <QueryClientProvider
          client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
        >
          <ModelsClient />
        </QueryClientProvider>
      </ViewerContext.Provider>,
    );
    expect(
      await screen.findByText("Models catalog could not be loaded. Try again."),
    ).toBeVisible();
  });
});

describe("filters, snippets, table and detail sheet", () => {
  it("narrows by search, keeps filters honest, and opens details and the exact-model table", async () => {
    renderModels(seededCatalog);
    await screen.findByRole("button", { name: "Copy model name chat" });
    fireEvent.change(screen.getByPlaceholderText("Search model names or providers"), {
      target: { value: "image" },
    });
    expect(
      screen.getByRole("button", { name: "Copy model name image" }),
    ).toBeVisible();
    expect(screen.queryByText("Best default for short conversations")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Paid" }));
    expect(
      screen.getByRole("button", { name: "No provider charge" }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText(/No models match these filters/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Paid" }));
    const imageDetails = within(
      (fireEvent.click(screen.getAllByRole("button", { name: "Details" })[0]), screen.getByRole("dialog")),
    );
    expect(imageDetails.getByText("Example request")).toBeVisible();
    expect(imageDetails.getByText(/\/v1\/images\/generations/)).toBeVisible();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    fireEvent.click(screen.getByRole("button", { name: /All models/ }));
    expect(screen.getByRole("button", { name: "All models" })).toBeVisible();
    const exact = within(screen.getByRole("table", { name: "All models" }))
      .getAllByText("provider/image-fast")
      .filter((node) => node.tagName === "CODE");
    expect(exact).toHaveLength(1);
    expect(exact[0]).toBeVisible();
  });

  it("omits typical time for a model with no timing data", async () => {
    renderModels(seededCatalog);
    fireEvent.click(await screen.findByRole("button", { name: /All models/ }));
    const table = screen.getByRole("table", { name: "All models" });
    const sheet = openDetails(table);
    expect(sheet).not.toHaveTextContent("Typical time");
    expect(sheet).not.toHaveTextContent("Not measured yet");
  });
});

describe("truthful metadata", () => {
  it("keeps locality, engine, timing and ids out of rows and shows them in Details", async () => {
    renderModels(aliasesOnly([measured, typical]));
    await screen.findByRole("button", { name: "Copy model name chat" });
    const table = screen.getByRole("table", { name: "Available model names" });
    const cards = within(table).getAllByRole("row").filter((r) => r.closest("tbody"));
    expect(cards).toHaveLength(2);
    const [first, second] = cards.map((card) => within(card));
    const aliasGroups = within(screen.getByLabelText("Available model names", { selector: "div" }));
    expect(aliasGroups.queryByText("Local · no provider charge", { exact: true })).toBeNull();
    expect(first.queryByText("Local", { exact: true })).toBeNull();
    expect(first.queryByText("Household chat")).toBeNull();
    expect(first.queryByText("~151 ms · Measured")).toBeNull();
    expect(first.queryByText("Unknown", { exact: true })).toBeNull();
    expect(first.queryByText("Live availability is not measured yet")).toBeNull();
    expect(second.queryByText("Remote", { exact: true })).toBeNull();
    expect(second.queryByText("Example image provider")).toBeNull();
    expect(second.queryByText("~8.0 s · Typical")).toBeNull();
    expect(within(table).queryByText("Not measured yet", { exact: true })).toBeNull();
    expect(within(table).queryByText(/capture-engine|provider-opaque-id/i)).toBeNull();

    const chatDialog = within(openDetails(cards[0]));
    expect(chatDialog.getByText("Household chat")).toBeVisible();
    expect(chatDialog.getByText("Measured time")).toBeVisible();
    expect(chatDialog.getByText("~151 ms · Measured")).toBeVisible();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
  });

  it("shows a typical time for an alias whose timing is configured, not measured", async () => {
    renderModels(aliasesOnly([measured, typical]));
    await screen.findByRole("button", { name: "Copy model name image" });
    const row = screen.getByRole("button", { name: "Copy model name image" }).closest("tr")!;
    const dialog = within(openDetails(row));
    expect(dialog.getByText("Example image provider")).toBeVisible();
    expect(dialog.getByText("~8.0 s · Typical")).toBeVisible();
  });

  it("stays honest when engine label and timing are absent, and never shows a service id", async () => {
    renderModels(
      aliasesOnly([
        { ...measured, engine_label: null, timing: { typical_seconds: null, source: null } },
      ]),
    );
    await screen.findByRole("button", { name: "Copy model name chat" });
    expect(screen.queryByText("Configured engine")).toBeNull();
    expect(
      within(screen.getByLabelText("Available model names", { selector: "div" })).queryByText("Not measured yet", { exact: true }),
    ).toBeNull();
    const dialog = within(openDetails(screen.getByRole("table", { name: "Available model names" })));
    expect(dialog.queryByText("Not measured yet", { exact: true })).toBeNull();
    expect(dialog.queryByText("Typical time", { exact: true })).toBeNull();
    expect(screen.queryByText(/capture-engine|Location not measured yet/)).toBeNull();
  });

  it("offers Clear filters after a search that matches nothing", async () => {
    renderModels(aliasesOnly([measured]));
    await screen.findByRole("button", { name: "Copy model name chat" });
    fireEvent.change(screen.getByRole("textbox", { name: "Search models" }), {
      target: { value: "unmatched" },
    });
    expect(screen.queryByRole("button", { name: "Copy model name chat" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(screen.getByRole("button", { name: "Copy model name chat" })).toBeVisible();
  });
});

describe("aliases keep alternatives within their server-classified capability", () => {
  const peer = (id: string, intent: string, capability: string, label: string) => ({
    ...measured,
    id,
    display_name: id,
    title: id,
    capability,
    capability_title: label,
    icon_key: capability,
    use_names: [id],
    operations: [capability],
    capabilities: [{ intent, label, capability, icon_key: capability }],
  });

  it("opens each capability's own alternatives and no other's", async () => {
    renderModels(
      aliasesOnly([
        peer("default", "chat.completions", "chat", "Chat"),
        peer("image", "image.generate", "image", "Images"),
        peer("decide", "decision.systemone", "decide", "Decisions"),
        peer("tts", "speech.synthesis", "speech", "Speech"),
        peer("chat-peer", "chat.completions", "chat", "Chat"),
        peer("image-peer", "image.generate", "image", "Images"),
      ]),
    );
    await screen.findByRole("button", { name: "Copy model name default" });
    const groupOf = (label: string) =>
      screen.getByRole("rowgroup", { name: `${label} models` });
    const alternativesOf = (key: string) =>
      document.getElementById(`model-alternatives-${key}`)!;
    for (const [label, key, primary, alternative] of [
      ["Chat", "chat", "default", "chat-peer"],
      ["Images", "image", "image", "image-peer"],
      ["Choose an option", "decide", "decide", null],
      ["Speech", "speech", "tts", null],
    ] as const) {
      const group = groupOf(label);
      expect(within(group).getByRole("heading", { name: primary })).toBeVisible();
      if (alternative) {
        const more = within(group).getByText("1 more", { exact: true });
        expect(document.getElementById(`model-alternatives-${key}`)).toBeNull();
        expect(
          screen.queryByRole("button", { name: `Copy model name ${alternative}` }),
        ).toBeNull();
        fireEvent.click(more);
        expect(
          within(alternativesOf(key)).getByRole("button", { name: `Copy model name ${alternative}` }),
        ).toBeVisible();
      } else {
        expect(within(group).queryByText(/\d+ more/)).toBeNull();
      }
    }
    expect(
      within(groupOf("Chat")).queryByRole("button", { name: "Copy model name image-peer" }),
    ).toBeNull();
    expect(
      within(alternativesOf("image")).queryByRole("button", { name: "Copy model name chat-peer" }),
    ).toBeNull();
  });
});
