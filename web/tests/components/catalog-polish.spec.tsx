import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ModelsClient from "@/app/models/models-client";
import { FilterToggle } from "@/components/product/filter-toggle";

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
    vi.fn(async () =>
      Response.json({
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

describe("catalog polish", () => {
  it("uses a friendly title while keeping the exact copyable alias", async () => {
    renderCatalog([
      row("fast", "chat.completions"),
      row("image", "image.generate", true),
    ]);
    expect(await screen.findByRole("heading", { name: "Fast" })).toBeVisible();
    expect(
      screen.getByRole("heading", { name: "Images", level: 2 }),
    ).toBeVisible();
    expect(
      screen.queryByRole("heading", { name: "Images", level: 3 }),
    ).toBeNull();
    expect(
      screen.getByRole("button", { name: "Copy model name fast" }),
    ).toHaveTextContent("fast");
    expect(screen.getByText("Paid · $0.04 per image")).toBeVisible();
    expect(screen.getByText("Free (your hardware)")).toBeVisible();
    expect(screen.queryByText("Not measured yet")).toBeNull();
    expect(screen.queryByTitle("Typical time")).toBeNull();
    expect(screen.queryByText(/Two requests are ahead of yours/)).toBeNull();
    const image = screen
      .getByRole("button", { name: "Copy model name image" })
      .closest("article");
    fireEvent.click(within(image!).getByRole("button", { name: "Details" }));
    const detail = screen.getByRole("dialog");
    expect(within(detail).getByText("Paid · $0.04 per image")).toBeVisible();
    expect(within(detail).queryByText("Typical time")).toBeNull();
    expect(within(detail).queryByText("Not measured yet")).toBeNull();
    expect(within(detail).queryByText("Typical time")).toBeNull();
  });

  it("uses engine titles when a model title only repeats its capability", async () => {
    renderCatalog([
      {
        ...row("speech-model", "speech.synthesis"),
        title: "Speech",
        engine_label: "Chatterbox Turbo",
      } as ReturnType<typeof row> & { engine_label: string },
      {
        ...row("transcription-model", "speech.transcription"),
        title: "Transcription",
        engine_label: "Parakeet v3",
      } as ReturnType<typeof row> & { engine_label: string },
      {
        ...row("embedding-model", "text.embeddings"),
        title: "Embeddings · Embed",
      },
    ]);

    expect(
      await screen.findByRole("heading", { name: "Chatterbox Turbo" }),
    ).toBeVisible();
    expect(screen.getByRole("heading", { name: "Parakeet v3" })).toBeVisible();
    expect(
      screen.queryByRole("heading", { name: "Embeddings · Embed" }),
    ).toBeNull();
  });

  it("preserves independent no-provider-charge and paid filter choices", async () => {
    renderCatalog([
      row("chat", "chat.completions"),
      row("image", "image.generate", true),
    ]);
    const group = await screen.findByRole("group", {
      name: "Price filters (choose any)",
    });
    const noCharge = within(group).getByRole("button", {
      name: "No provider charge",
    });
    const paid = within(group).getByRole("button", { name: "Paid" });
    expect(noCharge).toHaveAttribute("aria-pressed", "true");
    expect(paid).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(paid);
    expect(paid).toHaveAttribute("aria-pressed", "false");
    expect(noCharge).toHaveAttribute("aria-pressed", "true");
    expect(
      screen.queryByRole("button", { name: "Copy model name image" }),
    ).toBeNull();
    expect(
      screen.getByRole("button", { name: "Copy model name chat" }),
    ).toBeVisible();
    fireEvent.click(noCharge);
    expect(screen.getByText("No models match these filters")).toBeVisible();
    fireEvent.click(paid);
    expect(
      screen.getByRole("button", { name: "Copy model name image" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Copy model name chat" }),
    ).toBeNull();
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
    await screen.findByText("Ready to start", { exact: true });
    expect(screen.queryByText("Unknown")).toBeNull();
    expect(screen.queryByText(/No health endpoint is configured/)).toBeNull();
    expect(
      screen.queryByText(/Live availability is not measured yet/),
    ).toBeNull();
    expect(
      screen.getByText("Ready to start", { exact: true }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Starts when you send a request/)).toBeNull();
    expect(screen.queryByText("Unavailable")).toBeNull();
  });

  it("uses friendly catalog titles when the display name repeats a capability", async () => {
    const speech = {
      ...row("speech-model", "speech.synthesis"),
      title: "Speech · Speech",
      display_name: "Speech · Speech",
      engine_label: "Chatterbox Turbo",
    };
    const transcription = {
      ...row("transcription-model", "speech.transcription"),
      title: "Transcription · Transcription",
      display_name: "Transcription · Transcription",
      engine_label: "Parakeet v3",
    };
    const embedding = {
      ...row("embedding-model", "text.embeddings"),
      title: "Embeddings · Embed",
      display_name: "Embeddings · Embed",
      engine_label: "Nomic Embed",
    };
    renderCatalog([speech, transcription, embedding]);

    expect(
      await screen.findByRole("heading", { name: "Chatterbox Turbo" }),
    ).toBeVisible();
    expect(screen.getByRole("heading", { name: "Parakeet v3" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Nomic Embed" })).toBeVisible();
    expect(
      screen.queryByRole("heading", { name: "Speech · Speech" }),
    ).toBeNull();
    expect(
      screen.queryByRole("heading", { name: "Transcription · Transcription" }),
    ).toBeNull();
    expect(
      screen.queryByRole("heading", { name: "Embeddings · Embed" }),
    ).toBeNull();
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
