import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import ModelsClient from "@/app/models/models-client";

beforeEach(() => {
  Element.prototype.scrollIntoView ??= () => {};
});

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
    vi.fn(async (url: RequestInfo | URL) =>
      String(url).includes("well-known")
        ? Response.json({ recipes: {} })
        : Response.json({
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

const nameButton = (id: string) => screen.getByRole("button", { name: id });
const rowOf = (id: string) => nameButton(id).closest("li") as HTMLElement;
/** One Details fact: the value beside its label. */
function fact(label: string): string | undefined {
  return [...screen.getByRole("dialog").querySelectorAll("dt")]
    .find((dt) => dt.textContent === label)
    ?.nextElementSibling?.textContent ?? undefined;
}

it("keeps the engine out of the row and shows a measured speed only in Details", async () => {
  renderCatalog([row()]);
  await screen.findByRole("button", { name: "chat" });
  const card = within(rowOf("chat"));
  expect(card.queryByText(/Household chat|capture-engine/)).toBeNull();
  expect(card.queryByText("Unknown", { exact: true })).toBeNull();
  expect(card.queryByText(/Live availability is not measured yet|Not measured yet/)).toBeNull();
  expect(card.queryByText(/ms\b/)).toBeNull();
  fireEvent.click(nameButton("chat"));
  expect(fact("Time per request")).toBe("About 151 ms, measured");
  // The engine label is for the owner; a member sees where it runs.
  expect(fact("Runs on")).toBe("This gateway's engines");
  expect(screen.queryByText("Household chat")).toBeNull();
});

it("distinguishes a configured typical speed from measured requests", async () => {
  renderCatalog([
    row({
      locality: "remote",
      engine_label: "Example provider",
      cost: { kind: "paid", price: "$0.02 per request" },
      timing: { typical_seconds: 1.2, source: "configured" },
    }),
  ]);
  await screen.findByRole("button", { name: "chat" });
  const card = within(rowOf("chat"));
  expect(card.getAllByText("$0.02 per request").length).toBeGreaterThan(0);
  expect(card.getByTitle(/via Example provider/)).toBeInTheDocument();
  expect(card.queryByText(/About 1\.2 s/)).toBeNull();
  fireEvent.click(nameButton("chat"));
  expect(fact("Time per request")).toBe("About 1.2 s, typical");
  expect(screen.queryByText(/measured/)).toBeNull();
});

it("gives each exact model its id as the name and a title only when it adds one", async () => {
  const id = "audio.generate.internal-workflow";
  renderCatalog(
    [],
    [
      row({ id, display_name: id, title: "Chat model", name: id, alias: false, object: "model_catalog.model" }),
      row({
        id: "named-id",
        display_name: "named-id",
        name: "Household assistant",
        title: "Household assistant",
        alias: false,
        object: "model_catalog.model",
        description: "Another description.",
      }),
    ],
  );
  await screen.findByRole("button", { name: id });
  // The id is the button; the title and description sit on its second line.
  expect(nameButton(id).tagName).toBe("BUTTON");
  expect(nameButton(id)).toHaveClass("font-mono");
  expect(within(rowOf(id)).getByTitle(/^Chat model/)).toBeInTheDocument();
  expect(within(rowOf("named-id")).getByTitle(/^Household assistant · Another/)).toBeInTheDocument();
  fireEvent.click(nameButton(id));
  const detail = within(screen.getByRole("dialog"));
  expect(detail.getByRole("heading", { name: id })).toBeVisible();
  expect(detail.getByText(/^Chat model\./)).toBeVisible();
});

it("shows busy as a quiet word in the row and as the status badge in Details", async () => {
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
  await screen.findByRole("button", { name: "chat" });
  const status = within(rowOf("chat")).getAllByText("Busy")[0];
  expect(status).not.toHaveAttribute("data-slot", "badge");
  expect(screen.queryByText("A request is running.")).toBeNull();
  fireEvent.click(nameButton("chat"));
  expect(within(screen.getByRole("dialog")).getByText("Busy")).toHaveAttribute("data-slot", "badge");
});

it("provides actions for the initial empty catalog and a search that matches nothing", async () => {
  renderCatalog([]);
  expect(await screen.findByRole("link", { name: "Review setup" })).toHaveAttribute("href", "/");
  cleanup();
  renderCatalog([row()]);
  const search = await screen.findByRole("searchbox");
  fireEvent.change(search, { target: { value: "unmatched" } });
  fireEvent.click(screen.getByRole("button", { name: "Clear search" }));
  expect(search).toHaveValue("");
  expect(screen.getByRole("button", { name: "chat" })).toBeVisible();
});
