import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ModelsClient from "@/app/models/models-client";
import { FilterToggle } from "@/components/product/filter-toggle";

beforeEach(() => {
  Element.prototype.scrollIntoView ??= () => {};
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function row(id: string, intent: string, paid = false) {
  const capability = intent.startsWith("speech.trans")
    ? "transcribe"
    : intent.startsWith("speech.")
      ? "speech"
      : intent === "text.embeddings"
        ? "embed"
        : intent.startsWith("image.")
          ? "image"
          : "chat";
  const label = {
    chat: "Chat",
    image: "Images",
    speech: "Speech",
    transcribe: "Transcription",
    embed: "Embeddings",
  }[capability];
  return {
    id,
    object: "model_catalog.alias",
    display_name: id,
    title: id === "fast" ? "Fast" : intent === "image.generate" ? "Images" : id,
    alias: true,
    recommended: false,
    operations: [],
    resolves_to: {},
    description: "A configured model.",
    capabilities: [
      {
        intent,
        label,
        capability,
      },
    ],
    provider: paid ? "Example provider" : "Local engine",
    locality: paid ? "remote" : "local",
    cost: {
      kind: paid ? "paid" : "free",
      price: paid ? "$0.04 per image" : null,
    },
    timing: { typical_seconds: null, source: null },
    availability: {
      available: false,
      measured: true,
      status: "busy",
      reason: "Two requests are ahead of yours.",
    },
    io: { input: ["text"], output: ["text"] },
    examples: [],
    recent_receipts: [],
    use: { model: id },
  };
}

function rowWithAvailability(
  id: string,
  intent: string,
  availability: ReturnType<typeof row>["availability"],
) {
  return {
    ...row(id, intent),
    availability,
  };
}

function renderCatalog(aliases: ReturnType<typeof row>[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: RequestInfo | URL) =>
      String(url).includes("well-known")
        ? Response.json({ recipes: {} })
        : Response.json({
            schema_version: 1,
            object: "models.catalog",
            aliases,
            models: [],
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
/** True when the row shows the text anywhere (a phone line repeats facts). */
const rowHas = (id: string, text: string) =>
  within(rowOf(id)).queryAllByText(text).length > 0;

describe("catalog polish", () => {
  it("keeps the exact alias as the name and prices local and paid rows honestly", async () => {
    renderCatalog([
      row("fast", "chat.completions"),
      row("image", "image.generate", true),
    ]);
    await screen.findByRole("button", { name: "fast" });
    // "Fast" only repeats the alias, so it is not shown beside it.
    expect(within(rowOf("fast")).queryByText("Fast")).toBeNull();
    expect(rowHas("fast", "Local · no provider charge")).toBe(true);
    expect(rowHas("image", "$0.04 per image")).toBe(true);
    for (const old of ["Free (your hardware)", "Not measured yet"]) {
      expect(screen.queryByText(old)).toBeNull();
    }
    expect(screen.queryByTitle("Typical time")).toBeNull();
    expect(screen.queryByText(/Two requests are ahead of yours/)).toBeNull();
    fireEvent.click(nameButton("image"));
    const detail = within(screen.getByRole("dialog"));
    expect(detail.getByText("$0.04 per image")).toBeVisible();
    expect(detail.queryByText("Time per request")).toBeNull();
    expect(detail.queryByText("Not measured yet")).toBeNull();
  });

  it("shows a model's own name and never its engine label", async () => {
    renderCatalog([
      {
        ...row("speech-model", "speech.synthesis"),
        title: "Chatterbox Turbo",
        engine_label: "Chatterbox (speech)",
      } as ReturnType<typeof row> & { engine_label: string },
      {
        ...row("transcription-model", "speech.transcription"),
        title: "Parakeet v3",
        engine_label: "Parakeet (GPU)",
      } as ReturnType<typeof row> & { engine_label: string },
    ]);
    await screen.findByRole("button", { name: "speech-model" });
    expect(within(rowOf("speech-model")).getAllByText(/^Chatterbox Turbo/).length).toBeGreaterThan(0);
    expect(within(rowOf("transcription-model")).getAllByText(/^Parakeet v3/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/Chatterbox \(speech\)|Parakeet \(GPU\)/)).toBeNull();
    // The sections use the capability names the API and Try use.
    expect(
      screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent),
    ).toEqual(["Speech1", "Transcription1"]);
  });

  it("names embeddings by the plain task name Try uses, not member jargon", async () => {
    renderCatalog([
      row("embedding-model", "text.embeddings"),
      row("fast", "chat.completions"),
    ]);
    await screen.findByRole("button", { name: "embedding-model" });
    expect(
      screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent),
    ).toEqual(["Chat1", "Turn text into numbers1"]);
    expect(screen.queryByRole("heading", { name: /embedding/i })).toBeNull();
  });

  it("keeps unknown and startable idle availability distinct from unavailable", async () => {
    renderCatalog([
      rowWithAvailability("unknown-health", "speech.synthesis", {
        available: true,
        measured: true,
        status: "unknown",
        reason: "No health endpoint is configured.",
      }),
      {
        ...rowWithAvailability("on-demand", "speech.synthesis", {
          available: true,
          measured: false,
          status: "idle_startable",
          reason: "Starts when you send a request.",
        }),
        recommended: true,
      },
      rowWithAvailability("unmeasured", "chat.completions", {
        available: true,
        measured: false,
        status: "ready",
        reason: "Live availability is not measured yet.",
      }),
    ]);
    await screen.findByRole("button", { name: "on-demand" });
    expect(rowHas("on-demand", "Ready to start")).toBe(true);
    expect(within(rowOf("on-demand")).getByText("Recommended")).toBeVisible();
    expect(rowHas("unknown-health", "Couldn't check")).toBe(true);
    expect(rowHas("unmeasured", "Ready")).toBe(true);
    expect(screen.queryByText("Unknown")).toBeNull();
    expect(screen.queryByText(/No health endpoint is configured/)).toBeNull();
    expect(screen.queryByText(/Live availability is not measured yet/)).toBeNull();
    expect(screen.queryByText(/Starts when you send a request/)).toBeNull();
    expect(screen.queryByText("Unavailable")).toBeNull();
  });

  it("provides a pressed state without the primary action style", () => {
    const click = vi.fn();
    render(
      <FilterToggle pressed onClick={click}>
        Images
      </FilterToggle>,
    );
    const toggle = screen.getByRole("button", {
      name: "Images",
      pressed: true,
    });
    expect(toggle).toHaveAttribute("data-variant", "outline");
    fireEvent.click(toggle);
    expect(click).toHaveBeenCalledOnce();
  });
});
