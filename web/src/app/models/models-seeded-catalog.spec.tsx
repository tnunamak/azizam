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
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ModelsClient from "./models-client";
import { ViewerContext, type Viewer } from "@/components/shell/session";
import { accountApi } from "@/lib/account-api";
import { productApi, type ModelsCatalog } from "@/lib/product-api";

vi.mock("@/lib/product-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/product-api")>();
  return {
    ...actual,
    productApi: { ...actual.productApi, models: vi.fn() },
  };
});

vi.mock("@/lib/account-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/account-api")>();
  return {
    ...actual,
    accountApi: { ...actual.accountApi, discovery: vi.fn() },
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
      provider: "OpenRouter",
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
      provider: "OpenRouter",
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

function renderModels(snapshot: unknown, viewer: Viewer = member) {
  vi.mocked(productApi.models).mockResolvedValue(snapshot as ModelsCatalog);
  render(
    <ViewerContext.Provider value={viewer}>
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
  vi.mocked(accountApi.discovery).mockReset();
  vi.mocked(accountApi.discovery).mockResolvedValue({ recipes: {} });
  Element.prototype.hasPointerCapture ??= () => false;
  Element.prototype.releasePointerCapture ??= () => {};
  Element.prototype.scrollIntoView ??= () => {};
});

afterEach(() => {
  cleanup();
});

const rowOf = (id: string) =>
  screen.getByRole("button", { name: id }).closest("li") as HTMLElement;
/** True when the row shows the text anywhere (a phone line repeats facts). */
const rowHas = (id: string, text: string) =>
  within(rowOf(id)).queryAllByText(text).length > 0;
const sectionOf = (label: string) =>
  screen.getByRole("region", { name: new RegExp(`^${label}\\s*\\d+$`) });
const idsIn = (label: string) =>
  [...sectionOf(label).querySelectorAll("li")].map((li) => li.dataset.model);
/** One Details fact: the value beside its label. */
function fact(label: string): string | undefined {
  return [...screen.getByRole("dialog").querySelectorAll("dt")]
    .find((dt) => dt.textContent === label)
    ?.nextElementSibling?.textContent ?? undefined;
}
function openDetails(id: string) {
  fireEvent.click(screen.getByRole("button", { name: id }));
  return within(screen.getByRole("dialog"));
}

describe("seeded catalog", () => {
  it("lists every name under its task with honest price and quiet status", async () => {
    renderModels(seededCatalog);
    await screen.findByRole("button", { name: "chat" });
    expect(
      screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent),
    ).toEqual(["Chat2", "Images2", "Speech1", "Choose an option1"]);
    expect(idsIn("Chat")).toEqual(["chat", "demo-chat"]);
    expect(idsIn("Images")).toEqual(["image", "provider/image-fast"]);
    // Row facts: tags in words, a local price, a remote price, quiet status.
    expect(within(rowOf("chat")).getByTitle(/Tool use, Reasoning, Vision/)).toBeInTheDocument();
    expect(rowHas("chat", "Local · no provider charge")).toBe(true);
    expect(rowHas("image", "$0.04 per image")).toBe(true);
    expect(rowHas("image", "Busy")).toBe(true);
    expect(rowHas("decide", "Ready to start")).toBe(true);
    expect(rowHas("tts", "Couldn't check")).toBe(true);
    expect(rowHas("chat", "Not checked yet")).toBe(true);
    expect(screen.queryByText("Unknown", { exact: true })).toBeNull();
    expect(screen.queryByText("Ready to start when you make a request.")).toBeNull();
  });

  it("explains a start-on-request model in Details and never prints Unknown", async () => {
    renderModels(seededCatalog);
    await screen.findByRole("button", { name: "decide" });
    expect(openDetails("decide").getByText("Ready to start when you make a request.")).toBeVisible();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(openDetails("tts").queryByText("Unknown", { exact: true })).toBeNull();
  });
});

describe("search, price and Details", () => {
  it("narrows by search and price, then opens an example request", async () => {
    const user = userEvent.setup();
    renderModels(seededCatalog);
    await screen.findByRole("button", { name: "chat" });
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "image" } });
    expect(screen.getByRole("button", { name: "image" })).toBeVisible();
    expect(screen.getByRole("button", { name: "provider/image-fast" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "chat" })).toBeNull();
    await user.click(screen.getByRole("combobox", { name: "Price" }));
    await user.click(screen.getByRole("option", { name: "No provider charge" }));
    expect(screen.getByText("No models match")).toBeVisible();
    await user.click(screen.getByRole("combobox", { name: "Price" }));
    await user.click(screen.getByRole("option", { name: "Paid" }));
    const sheet = openDetails("image");
    expect(sheet.getByText("Example request")).toBeVisible();
    expect(sheet.getByText(/\/v1\/images\/generations/)).toBeVisible();
    expect(sheet.getByText("Recent receipts")).toBeVisible();
  });

  it("omits time per request for a model with no timing data", async () => {
    renderModels(seededCatalog);
    await screen.findByRole("button", { name: "provider/image-fast" });
    openDetails("provider/image-fast");
    expect(fact("Time per request")).toBeUndefined();
    expect(fact("Runs on")).toBe("OpenRouter");
    expect(screen.getByRole("dialog")).not.toHaveTextContent("Not measured yet");
  });
});

describe("truthful metadata", () => {
  it("keeps the engine, service id and timing out of rows and shows them in Details", async () => {
    renderModels(aliasesOnly([measured, typical]), owner);
    await screen.findByRole("button", { name: "chat" });
    const list = screen.getByRole("region", { name: "Model names by task" });
    // A remote row names its provider; a local row names nothing.
    expect(within(rowOf("chat")).queryByText(/Household chat|via /)).toBeNull();
    expect(rowHas("image", "Example image provider")).toBe(false);
    expect(within(rowOf("image")).getByTitle(/via Example image provider/)).toBeInTheDocument();
    expect(within(list).queryByText(/capture-engine|provider-opaque-id/i)).toBeNull();
    expect(within(list).queryByText(/About \d+/)).toBeNull();
    expect(within(list).queryByText("Not measured yet", { exact: true })).toBeNull();

    openDetails("chat");
    expect(fact("Time per request")).toBe("About 151 ms, measured");
    expect(fact("Runs on")).toBe("This gateway's engines");
    expect(fact("Engine or provider")).toBe("Household chat");
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    // A configured speed is "typical", not "measured".
    openDetails("image");
    expect(fact("Time per request")).toBe("About 8.0 s, typical");
    expect(fact("Runs on")).toBe("Example image provider");
    expect(fact("Engine or provider")).toBe("Example image provider");
    expect(screen.getByRole("dialog")).not.toHaveTextContent(/provider-opaque-id/);
  });

  it("stays honest when the engine label and timing are absent, and never shows a service id", async () => {
    renderModels(
      aliasesOnly([{ ...measured, engine_label: null, timing: { typical_seconds: null, source: null } }]),
      owner,
    );
    await screen.findByRole("button", { name: "chat" });
    openDetails("chat");
    expect(fact("Engine or provider")).toBe("Engine or provider not specified");
    expect(fact("Time per request")).toBeUndefined();
    expect(document.body.textContent).not.toMatch(/capture-engine|Not measured yet|Location not measured yet/);
  });
});

describe("each task lists only its own names", () => {
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

  it("keeps peers within their server-classified task", async () => {
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
    await screen.findByRole("button", { name: "default" });
    expect(idsIn("Chat")).toEqual(["default", "chat-peer"]);
    expect(idsIn("Images")).toEqual(["image", "image-peer"]);
    expect(idsIn("Choose an option")).toEqual(["decide"]);
    expect(idsIn("Speech")).toEqual(["tts"]);
  });
});
