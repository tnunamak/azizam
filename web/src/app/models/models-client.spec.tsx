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
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ModelsClient, { snippetsFor } from "./models-client";
import { ViewerContext, type Viewer } from "@/components/shell/session";
import { accountApi } from "@/lib/account-api";
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

vi.mock("@/lib/account-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/account-api")>();
  return {
    ...actual,
    accountApi: { ...actual.accountApi, discovery: vi.fn() },
  };
});

const GATEWAY = "https://gateway.example.test/openai/v1";

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

/** An exact model, not an alias. */
function exact(id: string, overrides: Partial<CatalogRow> = {}): CatalogRow {
  return row({ id, alias: false, object: "model_catalog.model", ...overrides });
}

/** A row of another task, classified the way the server does. */
function task(
  id: string,
  capability: string,
  overrides: Partial<CatalogRow> = {},
): CatalogRow {
  return row({
    id,
    capability,
    capabilities: [{ ...row().capabilities[0], capability }],
    ...overrides,
  });
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

/** The button that opens a model's Details; its name is the model id. */
const nameButton = (id: string) => screen.getByRole("button", { name: id });
const rowOf = (id: string) => nameButton(id).closest("li") as HTMLElement;
const copyButton = (id: string) =>
  screen.getByRole("button", { name: `Copy model name ${id}` });
/** The section for one task: its heading reads "<Label><count>". */
const sectionOf = (label: string) =>
  screen.getByRole("region", { name: new RegExp(`^${label}\\s*\\d+$`) });
const search = () => screen.getByRole("searchbox");
const dialog = () => within(screen.getByRole("dialog"));
/** A Details fact: the label and its value, as text. */
function fact(label: string): string | undefined {
  return [...screen.getByRole("dialog").querySelectorAll("dt")]
    .find((dt) => dt.textContent === label)
    ?.nextElementSibling?.textContent ?? undefined;
}
/** True when the row shows the text anywhere (a phone line repeats facts). */
const rowHas = (id: string, text: string) =>
  within(rowOf(id)).queryAllByText(text).length > 0;

beforeEach(() => {
  vi.mocked(productApi.models).mockReset();
  vi.mocked(accountApi.discovery).mockReset();
  vi.mocked(accountApi.discovery).mockResolvedValue({
    recipes: { openai: { canonical_base_url: GATEWAY } },
  });
  // jsdom lacks what Radix Select calls.
  Element.prototype.hasPointerCapture ??= () => false;
  Element.prototype.releasePointerCapture ??= () => {};
  Element.prototype.scrollIntoView ??= () => {};
});

afterEach(() => {
  cleanup();
});

describe("the list of model names", () => {
  it("shows every model name under its task, with counts, and nothing to expand", async () => {
    renderModels(
      catalog(
        [
          row({ id: "fast" }),
          row({ id: "coder" }),
          row({ id: "rp", secondary_profile: true }),
          row({ id: "default", recommended: true }),
          task("draw", "image"),
        ],
        // "default" is listed as an alias and as a model: one row.
        [exact("exact-chat"), exact("default")],
      ),
    );
    await screen.findByRole("button", { name: "fast" });
    expect(screen.getByRole("heading", { name: "Models", level: 1 })).toBeVisible();
    expect(screen.getByText("Names your apps put in the model field.")).toBeVisible();
    // The header band names the columns; each task is its own section.
    const list = screen.getByRole("region", { name: "Model names by task" });
    for (const label of ["Model name", "Price", "Status"]) {
      expect(within(list).getByText(label)).toBeInTheDocument();
    }
    expect(
      within(list).getAllByRole("heading", { level: 2 }).map((h) => h.textContent),
    ).toEqual(["Chat5", "Images1"]);
    expect(within(sectionOf("Chat")).getByText(/Answers, writing and code/)).toBeVisible();
    // Aliases first, recommended first, secondary profiles after, then exact.
    const order = (label: string) =>
      [...sectionOf(label).querySelectorAll("li")].map((li) => li.dataset.model);
    expect(order("Chat")).toEqual(["default", "fast", "coder", "rp", "exact-chat"]);
    expect(order("Images")).toEqual(["draw"]);
    expect(screen.getAllByRole("button", { name: "default" })).toHaveLength(1);
    expect(within(rowOf("default")).getByText("Recommended")).toBeVisible();
    // Nothing is hidden behind a control.
    expect(screen.queryByText(/\d+ more/)).toBeNull();
    expect(screen.queryByRole("button", { name: /All models|more/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^(Chat|Images)$/ })).toBeNull();
    expect(screen.getByText(/^6 model names/)).toBeVisible();
  });

  it("sorts rows into the server's task, not the one the legacy intent suggests", async () => {
    const audio = task("operator-audio", "audio");
    audio.capabilities = [
      { ...audio.capabilities[0], intent: "speech.synthesis", label: "Audio" },
    ];
    renderModels(
      catalog([
        row({ id: "chat" }),
        // The first serving capability is a chat endpoint; the type decides.
        task("draw", "image"),
        task("pick", "decide"),
        task("embed", "embed"),
        task("transcribe", "transcribe"),
        task("voice", "speech"),
        audio,
        task("future", "future-task"),
      ]),
    );
    await screen.findByRole("button", { name: "chat" });
    const headings = screen
      .getAllByRole("heading", { level: 2 })
      .map((h) => h.textContent);
    expect(headings).toEqual([
      // "future-task" has no section of its own, so it stays under Chat.
      "Chat2",
      "Images1",
      "Speech1",
      "Audio1",
      "Transcription1",
      "Choose an option1",
      "Turn text into numbers1",
    ]);
    expect(within(sectionOf("Chat")).getByRole("button", { name: "future" })).toBeVisible();
    expect(within(sectionOf("Audio")).getByRole("button", { name: "operator-audio" })).toBeVisible();
    expect(document.body.textContent).not.toMatch(/Make a picture/);
  });

  it("shows a description once per task, not on rows that share it", async () => {
    renderModels(
      catalog([
        row({ id: "alpha", description: "Shared by two rows." }),
        row({ id: "beta", description: "Shared by two rows." }),
        row({ id: "gamma", description: "Only gamma says this." }),
        task("draw", "image", { description: "The only image name." }),
      ]),
    );
    await screen.findByRole("button", { name: "alpha" });
    expect(screen.queryByText("Shared by two rows.")).toBeNull();
    // On one line of facts a sentence drops its full stop.
    expect(rowHas("gamma", "Only gamma says this")).toBe(true);
    // A task's only name needs no description beside the task's own sentence.
    expect(screen.queryByText("The only image name.")).toBeNull();
    // Details still carries the description.
    fireEvent.click(nameButton("alpha"));
    expect(dialog().getByText("Shared by two rows.")).toBeVisible();
  });

  it("adds a model's own name or provider to the second line only when it adds something", async () => {
    renderModels(
      catalog([
        row({ id: "default", description: "D1." }),
        // Repeats the id once punctuation is ignored.
        row({ id: "coder-q5", title: "Coder Q5", description: "D2." }),
        row({ id: "words", title: "Conversation", capability_title: "Conversation", description: "D3." }),
        row({ id: "opaque-4", title: "Lantern", description: "D4.", tags: ["tool-use", "reasoning", "vision"] }),
        row({ id: "recommended-only", recommended_model: "Qwen 3", description: "D5." }),
        row({
          id: "remote-one",
          title: "Firefly",
          locality: "remote",
          provider: "provider-internal-id",
          engine_label: "provider-a",
          description: "D6.",
        }),
        task("tts", "speech", { title: "Speech", description: "D7." }),
        task("tts-two", "speech", { title: "Supertonic", description: "D8." }),
      ]),
    );
    await screen.findByRole("button", { name: "default" });
    const second = (id: string) =>
      rowOf(id).querySelector<HTMLElement>("p[title]")?.title;
    expect(second("default")).toBe("D1");
    expect(second("coder-q5")).toBe("D2");
    expect(second("words")).toBe("D3");
    // The chat tags end the line, in words.
    expect(second("opaque-4")).toBe("Lantern · D4 · Uses tools, Thinks through questions, Understands images");
    expect(second("recommended-only")).toBe("Qwen 3 · D5");
    // A remote row names its provider (the label, not the internal id).
    expect(second("remote-one")).toBe("Firefly · via provider-a · D6");
    expect(second("tts")).toBe("D7");
    expect(second("tts-two")).toBe("Supertonic · D8");
    // A local row never shows its engine or service id.
    expect(rowOf("default")).not.toHaveTextContent(/Household chat|via /);
    expect(document.body.textContent).not.toContain("provider-internal-id");
  });

  it("folds a phone row's facts into one line", async () => {
    renderModels(
      catalog([
        row({ id: "home" }),
        row({
          id: "paid-busy",
          locality: "remote",
          engine_label: "Example provider",
          cost: { kind: "paid", price: "$1 per request" },
          availability: { ...row().availability, status: "busy" },
        }),
      ]),
    );
    await screen.findByRole("button", { name: "home" });
    const phone = (id: string) =>
      rowOf(id).querySelector<HTMLElement>(".sm\\:hidden")?.textContent;
    expect(phone("paid-busy")).toBe("Busy · via Example provider · $1 per request");
    // The usual local price and a ready state are left out of a phone line,
    // which falls back to the price when nothing else is left to say.
    expect(phone("home")).toBe("Local · no provider charge");
  });
});

describe("price and status", () => {
  const cases: [string, Partial<CatalogRow>, string][] = [
    ["a local row with an unknown cost", { cost: { kind: "unknown", price: null } }, "Local · no provider charge"],
    ["a local free row", {}, "Local · no provider charge"],
    ["a remote free row", { locality: "remote" }, "No provider charge"],
    ["a remote paid row", { locality: "remote", cost: { kind: "paid", price: "$0.04 per image" } }, "$0.04 per image"],
    ["a remote paid row with no price", { locality: "remote", cost: { kind: "paid", price: null } }, "Paid · price not recorded"],
    ["a remote row with an unknown cost", { locality: "remote", cost: { kind: "unknown", price: null } }, "Cost not recorded"],
  ];

  it.each(cases)("never leaves the price blank: %s", async (_name, overrides, label) => {
    renderModels(catalog([row({ id: "priced", ...overrides })]));
    await screen.findByRole("button", { name: "priced" });
    expect(rowHas("priced", label)).toBe(true);
    fireEvent.click(nameButton("priced"));
    expect(fact("Price")).toBe(label);
  });

  it("filters by the Price select, offering only the price kinds the catalog has", async () => {
    const user = userEvent.setup();
    renderModels(
      catalog([
        row({ id: "home" }),
        row({ id: "paid", locality: "remote", cost: { kind: "paid", price: "$1" } }),
        row({ id: "mystery", locality: "remote", cost: { kind: "unknown", price: null } }),
      ]),
    );
    await screen.findByRole("button", { name: "home" });
    await user.click(screen.getByRole("combobox", { name: "Price" }));
    expect(
      screen.getAllByRole("option").map((option) => option.textContent),
    ).toEqual(["Any price", "No provider charge", "Paid", "Cost not recorded"]);
    await user.click(screen.getByRole("option", { name: "Paid" }));
    expect(screen.getByRole("button", { name: "paid" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "home" })).toBeNull();
    expect(screen.queryByRole("button", { name: "mystery" })).toBeNull();
    expect(screen.getByText(/^1 of 3 model names/)).toBeVisible();
    await user.click(screen.getByRole("combobox", { name: "Price" }));
    await user.click(screen.getByRole("option", { name: "Cost not recorded" }));
    expect(screen.getByRole("button", { name: "mystery" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "paid" })).toBeNull();
    await user.click(screen.getByRole("combobox", { name: "Price" }));
    await user.click(screen.getByRole("option", { name: "Any price" }));
    expect(screen.getAllByRole("listitem", { hidden: false }).length).toBeGreaterThanOrEqual(3);
    expect(screen.getByText(/^3 model names/)).toBeVisible();
  });

  it("counts a local row as paid when its own cost says paid, and hides the Price select with one kind", async () => {
    const user = userEvent.setup();
    renderModels(
      catalog([
        row({ id: "local-paid", cost: { kind: "paid", price: "$9" } }),
        row({ id: "home" }),
      ]),
    );
    await screen.findByRole("button", { name: "home" });
    await user.click(screen.getByRole("combobox", { name: "Price" }));
    await user.click(screen.getByRole("option", { name: "Paid" }));
    expect(screen.getByRole("button", { name: "local-paid" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "home" })).toBeNull();
    cleanup();
    renderModels(catalog([row({ id: "one" }), row({ id: "two" })]));
    await screen.findByRole("button", { name: "one" });
    expect(screen.queryByRole("combobox", { name: "Price" })).toBeNull();
  });

  const states: [string, Partial<CatalogRow["availability"]>, string][] = [
    ["ready", { status: "ready" }, "Ready"],
    ["idle_startable", { status: "idle_startable" as never }, "Ready to start"],
    ["idle", { status: "idle" }, "Ready to start"],
    ["starting", { status: "starting", available: false }, "Starting"],
    ["busy", { status: "busy", available: false }, "Busy"],
    ["degraded", { status: "degraded" }, "Degraded"],
    ["unavailable", { status: "unavailable", available: false }, "Unavailable"],
    ["not checked", { status: "unknown", measured: false, available: false }, "Not checked yet"],
    ["failed check", { status: "unknown", measured: true }, "Couldn't check"],
  ];

  it.each(states)("shows the quiet status for %s", async (_name, availability, label) => {
    renderModels(catalog([row({ availability: { ...row().availability, ...availability } })]));
    await screen.findByRole("button", { name: "chat" });
    expect(rowHas("chat", label)).toBe(true);
    // The reason sentence stays out of the row.
    expect(screen.queryByText("Recent request succeeded.")).toBeNull();
    expect(screen.queryByText("Unknown", { exact: true })).toBeNull();
  });
});

describe("task filter", () => {
  it("narrows the list to one task, offering only the tasks the catalog has", async () => {
    const user = userEvent.setup();
    renderModels(
      catalog([row({ id: "alpha" }), task("draw", "image"), task("vec", "embed")]),
    );
    await screen.findByRole("button", { name: "alpha" });
    await user.click(screen.getByRole("combobox", { name: "Task" }));
    expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual([
      "All tasks",
      "Chat",
      "Images",
      "Turn text into numbers",
    ]);
    await user.click(screen.getByRole("option", { name: "Turn text into numbers" }));
    expect(screen.getByRole("button", { name: "vec" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "alpha" })).toBeNull();
    expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(1);
    expect(screen.getByText(/^1 of 3 model names/)).toBeVisible();
    cleanup();
    renderModels(catalog([row({ id: "one" }), row({ id: "two" })]));
    await screen.findByRole("button", { name: "one" });
    expect(screen.queryByRole("combobox", { name: "Task" })).toBeNull();
  });
});

describe("search", () => {
  it("finds a name by its id, title, description, use name, provider or target", async () => {
    const user = userEvent.setup();
    renderModels(
      catalog([
        row({ id: "default" }),
        row({
          id: "opaque-17",
          title: "Lantern",
          description: "Offers gentle explanations.",
          use_names: ["opaque-17", "glow-name"],
          locality: "remote",
          engine_label: "Acme Cloud",
          resolves_to: { chat: "hidden-target" },
        }),
      ]),
    );
    await screen.findByRole("button", { name: "default" });
    expect(search()).toHaveAttribute("placeholder", "Search models, tasks or providers");
    for (const query of ["LANTERN", "gentle explanations", "glow-name", "acme cloud", "opaque-17", "hidden-target"]) {
      await user.clear(search());
      await user.type(search(), query);
      expect(screen.getByRole("button", { name: "opaque-17" })).toBeVisible();
      expect(screen.queryByRole("button", { name: "default" })).toBeNull();
    }
    expect(screen.getByText(/^1 of 2 model names/)).toBeVisible();
  });

  it("matches the task name and never a local row's engine label", async () => {
    renderModels(catalog([row({ id: "alpha" }), task("draw", "image")]));
    await screen.findByRole("button", { name: "alpha" });
    fireEvent.change(search(), { target: { value: "images" } });
    expect(screen.getByRole("button", { name: "draw" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "alpha" })).toBeNull();
    fireEvent.change(search(), { target: { value: "household chat" } });
    expect(screen.getByText("No models match")).toBeVisible();
  });

  it("explains an empty result and clears the search and the price", async () => {
    const user = userEvent.setup();
    renderModels(
      catalog([
        row({ id: "home" }),
        row({ id: "paid", locality: "remote", cost: { kind: "paid", price: "$1" } }),
      ]),
    );
    await screen.findByRole("button", { name: "home" });
    await user.click(screen.getByRole("combobox", { name: "Price" }));
    await user.click(screen.getByRole("option", { name: "Paid" }));
    fireEvent.change(search(), { target: { value: "unmatched" } });
    expect(screen.getByText("No models match")).toBeVisible();
    expect(screen.queryByRole("region", { name: "Model names by task" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Clear search" }));
    expect(search()).toHaveValue("");
    expect(screen.getByRole("combobox", { name: "Price" })).toHaveTextContent("Any price");
    expect(screen.getByRole("button", { name: "home" })).toBeVisible();
    expect(screen.getByRole("button", { name: "paid" })).toBeVisible();
  });
});

describe("copying", () => {
  it("copies a model name, whatever its status", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    const down = { ...row().availability, available: false, status: "unavailable" as const };
    renderModels(
      catalog([row({ id: "up" }), row({ id: "down", availability: down })], [exact("exact", { availability: down })]),
    );
    await screen.findByRole("button", { name: "up" });
    for (const id of ["up", "down", "exact"]) {
      fireEvent.click(copyButton(id));
      await waitFor(() => expect(writeText).toHaveBeenLastCalledWith(id));
    }
    expect(await screen.findByText("exact copied")).toBeInTheDocument();
  });

  it("announces a refused clipboard and exposes the full name to copy by hand", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("Permission denied"));
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    Object.defineProperty(document, "execCommand", { configurable: true, value: () => false });
    const id = "provider/a-long-model-name-that-needs-manual-copy";
    renderModels(catalog([row({ id })]));
    fireEvent.click(await screen.findByRole("button", { name: `Copy model name ${id}` }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Copy failed. Select the model name and copy it yourself.",
    );
    expect(screen.getByRole("textbox", { name: "Model name for manual copy" })).toHaveValue(id);
    expect(writeText).toHaveBeenCalledWith(id);
  });

  it("shows the gateway address from discovery, or this origin, and copies it", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    renderModels(catalog([row()]));
    expect(await screen.findByText(GATEWAY)).toBeVisible();
    expect(screen.getByText("Gateway address")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Copy gateway address" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(GATEWAY));
    cleanup();
    vi.mocked(accountApi.discovery).mockRejectedValue(new Error("offline"));
    renderModels(catalog([row()]));
    expect(await screen.findByText(`${window.location.origin}/openai/v1`)).toBeVisible();
  });
});

describe("Details", () => {
  it("lists the facts a person needs, and omits the ones the catalog does not report", async () => {
    renderModels(
      catalog([
        row({
          id: "rich",
          title: "Lantern",
          description: "Offers gentle explanations.",
          use_names: ["rich", "also-rich"],
          locality: "remote",
          engine_label: "Acme Cloud",
          cost: { kind: "paid", price: "$2 per 1M tokens" },
          timing: { typical_seconds: 0.151, source: "receipts" },
          tags: ["tool-use", "vision"],
          io: { input: ["text", "image"], output: ["text"] },
          examples: [{ kind: "chat.completions", body: { model: "rich", messages: [] } }],
          recent_receipts: [{ request_id: "req 1", status: "succeeded", latency_ms: 820 }],
        }),
        row({ id: "bare", io: { input: [], output: [] } }),
      ]),
    );
    fireEvent.click(await screen.findByRole("button", { name: "rich" }));
    expect(dialog().getByRole("heading", { name: "rich" })).toBeVisible();
    expect(dialog().getByText("Lantern. Offers gentle explanations.")).toBeVisible();
    expect(fact("Task")).toBe("Chat");
    expect(fact("Also answers to")).toBe("also-rich");
    expect(fact("Runs on")).toBe("Acme Cloud");
    expect(fact("Price")).toBe("$2 per 1M tokens");
    expect(fact("Time per request")).toBe("About 151 ms, measured");
    expect(fact("Can also")).toBe("Uses tools, Understands images");
    expect(fact("What you send and receive")).toBe("text, image to text");
    expect(dialog().getByText("Example for app setup")).toBeVisible();
    expect(dialog().getByText(/curl 'http/)).toBeInTheDocument();
    expect(dialog().getByText("Recent activity")).toBeVisible();
    expect(dialog().getByRole("link", { name: "View request details" })).toHaveAttribute(
      "href",
      "/usage/?receipt=req%201",
    );
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    fireEvent.click(nameButton("bare"));
    expect(fact("Runs on")).toBe("On your equipment");
    for (const label of ["Also answers to", "Time per request", "Can also", "What you send and receive"]) {
      expect(fact(label)).toBeUndefined();
    }
    expect(dialog().getByText("No recent requests were reported. Use this model in an app, then check Usage for results.")).toBeVisible();
  });

  it.each([
    [{ typical_seconds: 8, source: "configured" }, "About 8.0 s, typical"],
    [{ typical_seconds: 12, source: null }, "About 12 s"],
    [{ typical_seconds: -1, source: "receipts" }, undefined],
    [{ typical_seconds: null, source: null }, undefined],
  ] as const)("reports time per request for %j", async (timing, expected) => {
    renderModels(catalog([row({ timing })]));
    fireEvent.click(await screen.findByRole("button", { name: "chat" }));
    expect(fact("Time per request")).toBe(expected);
    expect(screen.queryByText(/Not measured yet|-1000 ms/)).toBeNull();
  });

  it.each([member, owner])("keeps technical details for the owner only ($role)", async (viewer) => {
    renderModels(
      catalog([
        row({
          provider: "svc-internal-id",
          resolves_to: { "chat.completions": "private-target" },
          technical_description: "Private engine configuration.",
        }),
      ]),
      viewer,
    );
    await screen.findByRole("button", { name: "chat" });
    // Never on a row, for anyone.
    expect(screen.queryByText(/private-target|Private engine configuration|svc-internal-id/)).toBeNull();
    fireEvent.click(nameButton("chat"));
    const sheet = dialog();
    expect(sheet.getByText("Chat through your configured model.")).toBeVisible();
    for (const value of ["Technical details", "Engine or provider", "Sends requests to", "How this model runs", "chat.completions: private-target", "Private engine configuration."]) {
      if (viewer.admin) expect(sheet.getByText(value)).toBeVisible();
      else expect(sheet.queryByText(value)).toBeNull();
    }
    if (viewer.admin) expect(fact("Engine or provider")).toBe("Household chat");
    expect(screen.queryByText(/svc-internal-id/)).toBeNull();
  });

  it("says when the engine or provider is not specified", async () => {
    renderModels(
      catalog([row({ engine_label: null, provider: null, technical_description: "Notes." })]),
      owner,
    );
    fireEvent.click(await screen.findByRole("button", { name: "chat" }));
    expect(fact("Engine or provider")).toBe("Engine or provider not specified");
  });

  const unconfigured = { available: false, status: "unavailable" as const, configured: false };
  const disabled = { available: false, status: "unavailable" as const, enabled: false };
  const actions: [string, Viewer, "local" | "remote", Partial<CatalogRow["availability"]>, string | null, string][] = [
    ["owner, local engine not connected", owner, "local", unconfigured, "Connect an engine", "/engines/"],
    ["owner, provider not connected", owner, "remote", unconfigured, "Connect a provider", "/providers/"],
    ["owner, local engine disabled", owner, "local", disabled, "Review engine", "/engines/"],
    ["owner, provider disabled", owner, "remote", disabled, "Review provider", "/providers/"],
    ["owner, unavailable but configured", owner, "local", { available: false, status: "unavailable" }, "Check status", "/status/"],
    ["member, not connected", member, "local", unconfigured, "Check status", "/status/"],
    ["member, not checked yet", member, "local", { status: "unknown", measured: false, available: false }, "Check status", "/status/"],
    ["anyone, ready", owner, "local", {}, null, ""],
    ["anyone, busy", member, "local", { status: "busy", available: false }, null, ""],
  ];

  it.each(actions)("offers the right action: %s", async (_name, viewer, locality, availability, label, href) => {
    renderModels(
      catalog([row({ locality, availability: { ...row().availability, ...availability } })]),
      viewer,
    );
    await screen.findByRole("button", { name: "chat" });
    // The action is in Details, not on the row.
    expect(within(rowOf("chat")).queryByRole("link")).toBeNull();
    fireEvent.click(nameButton("chat"));
    const links = dialog().queryAllByRole("link").filter((link) => !/receipt/i.test(link.textContent ?? ""));
    if (label === null) {
      expect(links).toHaveLength(0);
    } else {
      expect(links).toHaveLength(1);
      expect(links[0]).toHaveTextContent(label);
      expect(links[0]).toHaveAttribute("href", href);
    }
  });

  it("refreshes the open Details by id when the catalog reloads", async () => {
    const original = row({
      id: "refresh-model",
      title: "Original title",
      description: "Original purpose.",
      locality: "remote",
      engine_label: "Original engine",
      cost: { kind: "paid", price: "$1" },
    });
    const client = renderModels(catalog([original]), owner);
    fireEvent.click(await screen.findByRole("button", { name: "refresh-model" }));
    expect(dialog().getByText("Original title. Original purpose.")).toBeVisible();
    vi.mocked(productApi.models).mockResolvedValue(
      catalog([
        {
          ...original,
          title: "Updated title",
          description: "Updated purpose.",
          engine_label: "Updated engine",
          resolves_to: { chat: "updated-target" },
          cost: { kind: "paid", price: "$2" },
          availability: { ...original.availability, available: false, status: "unavailable" },
        },
      ]),
    );
    await act(async () => {
      await client.refetchQueries({ queryKey: ["models", "catalog"] });
    });
    await waitFor(() => {
      expect(dialog().getByText("Updated title. Updated purpose.")).toBeVisible();
      expect(fact("Price")).toBe("$2");
      expect(fact("Engine or provider")).toBe("Updated engine");
      expect(fact("Sends requests to")).toBe("chat: updated-target");
      expect(dialog().getByText("Unavailable")).toBeVisible();
    });
    expect(dialog().queryByText(/Original/)).toBeNull();
  });

  it("closes a removed model with a note, lets the person dismiss it, and does not reopen it", async () => {
    const client = renderModels(catalog([row({ id: "removed-model" })]));
    fireEvent.click(await screen.findByRole("button", { name: "removed-model" }));
    expect(screen.getByRole("dialog")).toBeVisible();
    vi.mocked(productApi.models).mockResolvedValue(catalog([row({ id: "other" })]));
    await act(async () => {
      await client.refetchQueries({ queryKey: ["models", "catalog"] });
    });
    expect(await screen.findByText("This model is no longer listed. Choose another model, or review setup if the list is empty.")).toBeVisible();
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText("This model is no longer listed. Choose another model, or review setup if the list is empty.")).toBeNull();
    // The model comes back: the note goes and Details stays closed.
    fireEvent.click(nameButton("other"));
    expect(dialog().getByRole("heading", { name: "other" })).toBeVisible();
    vi.mocked(productApi.models).mockResolvedValue(catalog([]));
    await act(async () => {
      await client.refetchQueries({ queryKey: ["models", "catalog"] });
    });
    expect(await screen.findByText("This model is no longer listed. Choose another model, or review setup if the list is empty.")).toBeVisible();
    vi.mocked(productApi.models).mockResolvedValue(catalog([row({ id: "other" })]));
    await act(async () => {
      await client.refetchQueries({ queryKey: ["models", "catalog"] });
    });
    await screen.findByRole("button", { name: "other" });
    expect(screen.queryByText("This model is no longer listed. Choose another model, or review setup if the list is empty.")).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("deep links", () => {
  let previousUrl: string;
  beforeEach(() => {
    previousUrl = window.location.href;
  });
  afterEach(() => {
    window.history.replaceState(null, "", previousUrl);
  });

  it("opens Details for ?model= and filters to the task in ?capability=", async () => {
    const id = "provider/model+v2";
    window.history.replaceState(null, "", `?capability=image&model=${encodeURIComponent(id)}`);
    renderModels(catalog([row(), task("draw", "image")], [exact(id, { capability: "image" })]));
    expect(await screen.findByRole("heading", { name: id })).toBeVisible();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(screen.getByRole("combobox", { name: "Task" })).toHaveTextContent("Images");
    expect(screen.getByRole("button", { name: "draw" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "chat" })).toBeNull();
    expect(search()).toHaveValue("");
    expect(screen.getByText(/^2 of 3 model names/)).toBeVisible();
    // Clear search shows every task again.
    fireEvent.change(search(), { target: { value: "unmatched" } });
    fireEvent.click(screen.getByRole("button", { name: "Clear search" }));
    expect(screen.getByRole("combobox", { name: "Task" })).toHaveTextContent("All tasks");
    expect(screen.getByRole("button", { name: "chat" })).toBeVisible();
  });

  it.each(["?model=unknown&capability=unknown", "?capability=video"])(
    "ignores an unknown model or a task with no models (%s)",
    async (query) => {
      window.history.replaceState(null, "", query);
      renderModels(catalog([row(), task("draw", "image")]));
      await screen.findByRole("button", { name: "chat" });
      expect(screen.queryByRole("dialog")).toBeNull();
      expect(screen.queryByText("This model is no longer listed. Choose another model, or review setup if the list is empty.")).toBeNull();
      expect(screen.getByRole("combobox", { name: "Task" })).toHaveTextContent("All tasks");
      expect(screen.getByRole("button", { name: "draw" })).toBeVisible();
    },
  );
});

describe("loading, errors and an empty catalog", () => {
  const client = () => new QueryClient({ defaultOptions: { queries: { retry: false } } });

  it("announces loading, then shows the models", async () => {
    let release!: (value: ModelsCatalog) => void;
    vi.mocked(productApi.models).mockReturnValue(
      new Promise<ModelsCatalog>((resolve) => {
        release = resolve;
      }),
    );
    render(
      <ViewerContext.Provider value={member}>
        <QueryClientProvider client={client()}>
          <ModelsClient />
        </QueryClientProvider>
      </ViewerContext.Provider>,
    );
    expect(screen.getByRole("status", { name: "Loading models" })).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByText("No models are available yet")).toBeNull();
    await act(async () => release(catalog([row()])));
    expect(await screen.findByRole("button", { name: "chat" })).toBeVisible();
    expect(screen.queryByRole("status", { name: "Loading models" })).toBeNull();
  });

  it("retries a failed load from the error state", async () => {
    vi.mocked(productApi.models)
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValue(catalog([row()]));
    render(
      <ViewerContext.Provider value={member}>
        <QueryClientProvider client={client()}>
          <ModelsClient />
        </QueryClientProvider>
      </ViewerContext.Provider>,
    );
    expect(await screen.findByText("We couldn't load the models. Try again.")).toBeVisible();
    expect(screen.queryByRole("searchbox")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(await screen.findByRole("button", { name: "chat" })).toBeVisible();
    expect(screen.queryByText("We couldn't load the models. Try again.")).toBeNull();
  });

  it.each([
    [owner, "Connect an engine", "/engines", "Connect an engine to run models on your equipment, or connect an online provider."],
    [member, "Review setup", "/", "Ask the owner to connect an engine or online provider and give you access to a model."],
  ])("points an empty catalog at the next step for $role", async (viewer, label, href, description) => {
    renderModels(catalog([]), viewer);
    expect(await screen.findByText("No models are available yet")).toBeVisible();
    expect(screen.getByText(description)).toBeVisible();
    expect(screen.getByRole("link", { name: label })).toHaveAttribute("href", href);
    expect(screen.queryByRole("searchbox")).toBeNull();
  });
});

describe("multipart example parity", () => {
  it.each(["/v1/audio/transcriptions", "/v1/images/edits"])("keeps all declared body options in every %s snippet", (path) => {
    const example = row({ use: { ...row().use, path }, examples: [{ kind: "example", body: { model: "custom", language: "fr", temperature: 0, stream: false, options: { quality: "high" }, prompt: "@literal input" } }] });
    const snippets = snippetsFor(example, "https://example.test");
    expect(snippets).toHaveLength(3);
    for (const snippet of snippets) for (const name of ["model", "custom", "language", "fr", "temperature", "stream", "options", "quality", "prompt", "@literal input"]) expect(snippet.code).toContain(name);
    expect(snippets[0].code).toContain("--form-string 'temperature=0'");
    expect(snippets[0].code).toContain("--form-string 'stream=false'");
    expect(snippets[0].code).toContain("--form-string 'prompt=@literal input'");
    expect(snippets[0].code).toContain(path.endsWith("edits") ? "-F image=@sample.png" : "-F file=@sample.wav");
  });
});
