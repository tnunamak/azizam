import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ModelsClient from "./models-client";
import { ViewerContext, type Viewer } from "@/components/shell/session";
import { accountApi } from "@/lib/account-api";
import { productApi, type CatalogRow, type ModelsCatalog } from "@/lib/product-api";

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


const unavailable = (id: string, overrides: Partial<CatalogRow> = {}) => row({
  id, availability: { ...row().availability, status: "unavailable", available: false }, ...overrides,
});
const toggle = () => screen.getByRole("switch", { name: "Show unavailable" });
const ids = () => [...screen.getByRole("region", { name: "Model names by task" }).querySelectorAll("li")]
  .map((li) => li.dataset.model);
let previousUrl: string;
beforeEach(() => {
  previousUrl = window.location.href;
  window.history.replaceState(null, "", "/models");
  vi.mocked(productApi.models).mockReset();
  vi.mocked(accountApi.discovery).mockReset();
  vi.mocked(accountApi.discovery).mockResolvedValue({
    recipes: { openai: { canonical_base_url: GATEWAY } },
  });
  Element.prototype.hasPointerCapture ??= () => false;
  Element.prototype.releasePointerCapture ??= () => {};
  Element.prototype.scrollIntoView ??= () => {};
});
afterEach(() => {
  cleanup();
  window.history.replaceState(null, "", previousUrl);
});

describe("Show unavailable", () => {
  it("hides unavailable chat models by default and counts them in their section", async () => {
    renderModels(catalog([unavailable("down"), row({ id: "up" })]));
    await screen.findByRole("button", { name: "up" });
    expect(toggle()).not.toBeChecked();
    expect(screen.queryByRole("button", { name: "down" })).toBeNull();
    const section = screen.getByRole("region", { name: /^Chat/ });
    expect(within(section).getByText("1 unavailable hidden")).toBeVisible();
    expect(screen.getByText("1 of 2 model names.")).toBeVisible();
  });

  it("shows unavailable models last and preserves rank order inside each partition", async () => {
    renderModels(catalog([
      unavailable("down", { recommended: true }),
      row({ id: "up" }), row({ id: "recommended", recommended: true }),
      unavailable("down-secondary", { secondary_profile: true }),
    ], [exact("up-exact")]));
    await screen.findByRole("button", { name: "up" });
    const before = window.location.href;
    fireEvent.click(toggle());
    expect(toggle()).toBeChecked();
    expect(ids()).toEqual(["recommended", "up", "up-exact", "down", "down-secondary"]);
    expect(screen.queryByText(/unavailable hidden/)).toBeNull();
    fireEvent.click(toggle());
    expect(ids()).toEqual(["recommended", "up", "up-exact"]);
    expect(screen.getByText("2 unavailable hidden")).toBeVisible();
    expect(window.location.href).toBe(before);
  });

  it("reads unavailable=1 with capability once without rewriting the address", async () => {
    window.history.replaceState(null, "", "?unavailable=1&capability=chat");
    const before = window.location.href;
    renderModels(catalog([unavailable("down"), row({ id: "up" }), task("draw", "image")]));
    await screen.findByRole("button", { name: "down" });
    expect(toggle()).toBeChecked();
    expect(ids()).toEqual(["up", "down"]);
    expect(screen.queryByRole("button", { name: "draw" })).toBeNull();
    fireEvent.click(toggle());
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "up" } });
    expect(toggle()).not.toBeChecked();
    expect(window.location.href).toBe(before);
  });

  it("keeps a hidden-only task section discoverable and excludes only unavailable", async () => {
    renderModels(catalog([
      unavailable("down", { capability: "image" }),
      row({ id: "degraded", availability: { ...row().availability, status: "degraded", available: false } }),
      row({ id: "unknown", availability: { ...row().availability, status: "unknown", available: false } }),
    ]));
    await screen.findByRole("button", { name: "degraded" });
    expect(ids()).toEqual(["degraded", "unknown"]);
    expect(within(screen.getByRole("region", { name: /^Images/ })).getByText("1 unavailable hidden")).toBeVisible();
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "down" } });
    expect(screen.getByText("1 unavailable hidden")).toBeVisible();
    expect(screen.getByText("1 matching model is unavailable and hidden. Turn on Show unavailable to see it.")).toHaveAttribute("role", "status");
    fireEvent.click(toggle());
    expect(ids()).toEqual(["down"]);
  });

  it("applies search, task and price filters and counts facets in both switch states", async () => {
    const user = userEvent.setup();
    renderModels(catalog([
      row({ id: "chat-free" }),
      unavailable("chat-paid", { locality: "remote", cost: { kind: "paid", price: "$1" } }),
      task("draw", "image", { locality: "remote", cost: { kind: "paid", price: "$1" } }),
    ]));
    await screen.findByRole("button", { name: "chat-free" });
    for (const show of [false, true]) {
      if (show) await user.click(toggle());
      await user.click(screen.getByRole("combobox", { name: "Task" }));
      await user.click(screen.getByRole("option", { name: `Chat (${show ? 2 : 1})` }));
      await user.click(screen.getByRole("combobox", { name: "Price" }));
      await user.click(screen.getByRole("option", { name: `Paid (${show ? 1 : 0})` }));
      expect(screen.queryByRole("button", { name: "chat-free" })).toBeNull();
      expect(screen.queryByRole("button", { name: "draw" })).toBeNull();
      if (show) expect(screen.getByRole("button", { name: "chat-paid" })).toBeVisible();
      else expect(screen.getByText("1 unavailable hidden")).toBeVisible();
      fireEvent.change(screen.getByRole("searchbox"), { target: { value: "absent" } });
      expect(screen.queryByRole("button", { name: "chat-paid" })).toBeNull();
      await user.click(screen.getByRole("combobox", { name: "Task" }));
      expect(screen.getByRole("option", { name: "All tasks (0)" })).toBeVisible();
      await user.keyboard("{Escape}");
      await user.click(screen.getByRole("button", { name: "Clear filters" }));
    }
  });
});
