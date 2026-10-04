import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import ModelsClient from "@/app/models/models-client";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function row(overrides: Record<string, unknown> = {}) {
  return {
    id: "chat",
    object: "model_catalog.alias",
    display_name: "chat",
    alias: true,
    operations: ["chat"],
    resolves_to: { chat: "internal-model" },
    description: "Chat through your configured model.",
    capabilities: [
      { intent: "chat.completions", capability: "chat", label: "Chat" },
    ],
    provider: "capture-engine",
    engine_label: "Household chat",
    locality: "local",
    cost: { kind: "unknown", price: null },
    timing: { typical_seconds: 0.151, source: "receipts", sample_count: 3 },
    availability: {
      measured: false,
      available: true,
      reason: "Live availability is not measured yet",
    },
    io: { input: ["text"], output: ["text"] },
    examples: [],
    recent_receipts: [],
    use: { model: "chat" },
    ...overrides,
  };
}

function renderCatalog(
  aliases: ReturnType<typeof row>[],
  models: ReturnType<typeof row>[] = [],
) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      Response.json({
        schema_version: 1,
        object: "models.catalog",
        aliases,
        models,
      }),
    ),
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ModelsClient />
    </QueryClientProvider>,
  );
}

it("shows the supplied engine label and measured speed without an unknown speed claim", async () => {
  renderCatalog([row()]);
  const copy = await screen.findByRole("button", {
    name: "Copy model name chat",
  });
  const card = within(copy.closest("article")!);
  expect(card.getByText("Local", { exact: true })).toBeVisible();
  expect(card.queryByText("Unknown", { exact: true })).toBeNull();
  expect(card.queryByText("Live availability is not measured yet")).toBeNull();
  expect(card.queryByText("Not measured yet", { exact: true })).toBeNull();
  expect(card.queryByText(/capture-engine/)).toBeNull();
  expect(card.queryByText("Ready")).toBeNull();
  fireEvent.click(card.getByRole("button", { name: "Details" }));
  const detail = within(screen.getByRole("dialog"));
  expect(detail.getByText("Measured time")).toBeVisible();
  expect(detail.getByText("~151 ms · Measured")).toBeVisible();
  expect(detail.getByText("Household chat")).toBeVisible();
});

it("distinguishes configured typical speed from measured requests", async () => {
  renderCatalog([
    row({
      locality: "remote",
      engine_label: "Example provider",
      cost: { kind: "paid", price: "$0.02 per request" },
      timing: { typical_seconds: 1.2, source: "configured" },
    }),
  ]);
  await screen.findByRole("button", { name: "Copy model name chat" });
  expect(screen.getByText("Remote", { exact: true })).toBeVisible();
  expect(screen.getByText("Paid · $0.02 per request")).toBeVisible();
  expect(screen.queryByText("~1.2 s · Typical")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Details" }));
  expect(
    within(screen.getByRole("dialog")).getByText("~1.2 s · Typical"),
  ).toBeVisible();
  expect(screen.queryByText(/Measured/)).toBeNull();
});

it("keeps absent timing honest and does not expose service ids as engine names", async () => {
  renderCatalog([
    row({
      engine_label: undefined,
      timing: { typical_seconds: null, source: null },
    }),
  ]);
  await screen.findByRole("button", { name: "Copy model name chat" });
  expect(screen.queryByText("Not measured yet")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Details" }));
  expect(screen.getByText("Configured engine")).toBeVisible();
  expect(
    within(screen.getByRole("dialog")).getByText("Not measured yet"),
  ).toBeVisible();
  expect(screen.queryByText(/capture-engine/)).toBeNull();
  expect(screen.queryByText(/Location not measured yet/)).toBeNull();
});

it("does not display invalid negative duration as a measured speed", async () => {
  renderCatalog([row({ timing: { typical_seconds: -1, source: "receipts" } })]);
  await screen.findByRole("button", { name: "Copy model name chat" });
  expect(screen.queryByText("Not measured yet")).toBeNull();
  expect(
    screen.queryByTitle("Measured time from successful requests"),
  ).toBeNull();
  expect(screen.queryByText(/-1000 ms/)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Details" }));
  expect(
    within(screen.getByRole("dialog")).getByText("Not measured yet"),
  ).toBeVisible();
});

it("provides actions for the initial empty catalog and filtered empty catalog", async () => {
  renderCatalog([]);
  expect(
    await screen.findByRole("link", { name: "Review setup" }),
  ).toHaveAttribute("href", "/ui/home/");
  cleanup();
  renderCatalog([row()]);
  const search = await screen.findByRole("textbox", { name: "Search models" });
  fireEvent.change(search, { target: { value: "unmatched" } });
  fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
  expect(search).toHaveValue("");
  expect(
    screen.getByRole("button", { name: "Copy model name chat" }),
  ).toBeVisible();
});

it("uses human model titles while keeping exact ids as secondary code and detail facts", async () => {
  const id = "audio.generate.internal-workflow";
  renderCatalog(
    [],
    [
      row({
        id,
        display_name: id,
        title: "Chat model",
        name: id,
        alias: false,
        object: "model_catalog.model",
      }),
      row({
        id: "named-id",
        display_name: "named-id",
        name: "Household assistant",
        title: "Household assistant",
        alias: false,
        object: "model_catalog.model",
      }),
    ],
  );
  fireEvent.click(await screen.findByRole("button", { name: /All models/ }));
  const table = within(screen.getByRole("table"));
  expect(table.getByText("Chat model")).toBeVisible();
  expect(table.getByText(id).tagName).toBe("CODE");
  expect(table.getByText("Household assistant")).toBeVisible();
  fireEvent.click(table.getByText("Chat model"));
  const detail = within(screen.getByRole("dialog"));
  expect(detail.getByRole("heading", { name: "Chat model" })).toBeVisible();
  for (const identifier of detail.getAllByText(id))
    expect(identifier).toBeVisible();
});

it("uses the shared status badge for measured availability", async () => {
  renderCatalog([
    row({
      availability: {
        measured: true,
        available: false,
        status: "busy",
        reason: "A request is running.",
      },
    }),
  ]);
  const status = await screen.findByText("Busy");
  expect(status).toHaveAttribute("data-slot", "badge");
  expect(screen.queryByText("A request is running.")).toBeNull();
});
