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


const member: Viewer = { authenticated: true, name: "Member", email: "member@example.test", role: "member", admin: false };
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


const clients: QueryClient[] = [];
async function openDetails(fields: Record<string, unknown> = {}) {
  const entry = { ...row(), ...fields };
  vi.mocked(productApi.models).mockResolvedValue({
    schema_version: 1, object: "models.catalog", aliases: [entry], models: [],
    counts: { aliases: 1, models: 0, capabilities: 1 },
  } as ModelsCatalog);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  render(<ViewerContext.Provider value={member}><QueryClientProvider client={client}><ModelsClient /></QueryClientProvider></ViewerContext.Provider>);
  fireEvent.click(await screen.findByRole("button", { name: "chat" }));
  return within(screen.getByRole("dialog"));
}
beforeEach(() => {
  vi.mocked(productApi.models).mockReset();
  vi.mocked(accountApi.discovery).mockReset();
  vi.mocked(accountApi.discovery).mockResolvedValue({ recipes: { openai: { canonical_base_url: "https://gateway.example.test/openai/v1" } } });
});
afterEach(() => {
  cleanup();
  for (const client of clients.splice(0)) client.clear();
});

describe("model detail dates and source", () => {
  it("shows the Released day and a safe new-tab Source link", async () => {
    const released = "2026-06-15T12:00:00Z";
    const detail = await openDetails({ released_at: released, source_url: "https://example.test/model" });
    const label = detail.getByText("Released", { selector: "dt" });
    expect(label.nextElementSibling?.textContent).toBe(new Date(released).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }));
    const source = detail.getByRole("link", { name: "Source" });
    expect(source).toHaveAttribute("href", "https://example.test/model");
    expect(source).toHaveAttribute("target", "_blank");
    expect(source).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("omits date and Source facts when none were supplied", async () => {
    const detail = await openDetails();
    for (const label of ["Released", "Added here", "Updated", "Source"]) expect(detail.queryByText(label, { selector: "dt" })).toBeNull();
    expect(detail.queryByRole("link", { name: "Source" })).toBeNull();
  });

  it("renders every available date but omits invalid dates and source URLs", async () => {
    const detail = await openDetails({ added_at: "2026-06-15", updated_at: "2027-01-15T08:00:00Z", released_at: "not a date", source_url: "javascript:alert(1)" });
    expect(detail.getByText("Added here", { selector: "dt" })).toBeVisible();
    expect(detail.getByText("Updated", { selector: "dt" })).toBeVisible();
    expect(detail.queryByText("Released", { selector: "dt" })).toBeNull();
    expect(detail.queryByRole("link", { name: "Source" })).toBeNull();
  });

  it("omits a date given as a bare number, which has no unit", async () => {
    const detail = await openDetails({ released_at: 1800000000 });
    expect(detail.queryByText("Released", { selector: "dt" })).toBeNull();
    expect(detail.queryByRole("link", { name: "Source" })).toBeNull();
  });
});
