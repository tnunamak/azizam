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
  return {
    id,
    object: "model_catalog.alias",
    display_name: id,
    alias: true,
    operations: [],
    resolves_to: {},
    description: "A configured model.",
    capabilities: [{ intent, label: intent }],
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
    expect(
      await screen.findByRole("heading", { name: "Fast chat" }),
    ).toBeVisible();
    expect(
      screen.getByRole("heading", { name: "Images", level: 3 }),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Copy alias fast" }),
    ).toHaveTextContent("fast");
    expect(screen.getByText("Paid · $0.04 per image")).toBeVisible();
    expect(screen.getByText("Local · no provider charge")).toBeVisible();
    expect(screen.queryByText(/Free/)).toBeNull();
    expect(screen.getAllByText("Not measured yet")).toHaveLength(2);
    expect(screen.getAllByTitle("Typical time")).toHaveLength(2);
    expect(screen.getAllByText(/Two requests are ahead of yours/)).toHaveLength(
      2,
    );
    fireEvent.click(screen.getAllByRole("button", { name: "Details" })[0]);
    const detail = screen.getByRole("dialog");
    expect(within(detail).getByText("Typical time")).toBeVisible();
    expect(within(detail).getByText("Not measured yet")).toBeVisible();
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
      screen.queryByRole("button", { name: "Copy alias image" }),
    ).toBeNull();
    expect(
      screen.getByRole("button", { name: "Copy alias chat" }),
    ).toBeVisible();
    fireEvent.click(noCharge);
    expect(screen.getByText("No models match these filters")).toBeVisible();
    fireEvent.click(paid);
    expect(
      screen.getByRole("button", { name: "Copy alias image" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Copy alias chat" }),
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
