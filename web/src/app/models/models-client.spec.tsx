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
}

beforeEach(() => {
  vi.mocked(productApi.models).mockReset();
});

afterEach(() => {
  cleanup();
});

describe("models catalog contract distinctions", () => {
  it("uses the shared product API client and labels allowed configured observed rows", async () => {
    renderModels(catalog([row()]));

    const copy = await screen.findByRole("button", { name: "Copy alias chat" });
    const card = within(copy.closest("article")!);

    expect(productApi.models).toHaveBeenCalledOnce();
    expect(card.getByText("Allowed by your access")).toBeVisible();
    expect(card.getByText("Configured")).toBeVisible();
    expect(card.getByText("Observed from request")).toBeVisible();
    expect(card.getByText("Observed:")).toBeVisible();
    expect(card.getByTitle("2027-01-15 08:00:00 UTC")).toBeVisible();
    expect(card.getByText("Enabled")).toBeVisible();
    expect(card.getByRole("button", { name: "Use" })).toBeVisible();
  });

  it("treats only explicit configured false as catalog-only and gives owners an engine action", async () => {
    renderModels(
      catalog([
        row({
          id: "catalog-chat",
          display_name: "Catalog chat",
          name: "Catalog chat",
          availability: {
            available: false,
            status: "unavailable",
            reason: "The service is not configured.",
            measured: true,
            enabled: true,
            configured: false,
            up: false,
            idle_startable: false,
            evidence: { source: "registry", kind: "unconfigured" },
          },
        }),
      ]),
      owner,
    );

    const action = await screen.findByRole("link", {
      name: "Connect an engine",
    });
    expect(action).toHaveAttribute("href", "/ui/engines/");
    expect(screen.getByText("Catalog only")).toBeVisible();
    expect(screen.getByText("From configuration")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Use" })).toBeNull();
  });

  it("keeps Use for busy rows and sends remote catalog-only owners to providers", async () => {
    renderModels(
      catalog([
        row({
          id: "busy-image",
          display_name: "Busy image",
          name: "Busy image",
          capabilities: [
            {
              intent: "image.generate",
              label: "Images",
              input_modalities: ["text"],
              output_modalities: ["image"],
              streaming: false,
              available: false,
            },
          ],
          availability: {
            available: false,
            status: "busy",
            reason: "A request is running.",
            measured: true,
            enabled: true,
            configured: true,
            up: true,
            idle_startable: false,
            evidence: { source: "health", observed_at_s: 1700000000 },
          },
        }),
        row({
          id: "remote-catalog",
          display_name: "Remote catalog",
          name: "Remote catalog",
          locality: "remote",
          provider: "Example provider",
          engine_label: "Example provider",
          availability: {
            available: false,
            status: "unavailable",
            reason: "The provider is not configured.",
            measured: true,
            enabled: true,
            configured: false,
            up: false,
            idle_startable: false,
            evidence: { source: "registry", kind: "unconfigured" },
          },
        }),
      ]),
      owner,
    );

    const busyCopy = await screen.findByRole("button", {
      name: "Copy alias busy-image",
    });
    const busyCard = within(busyCopy.closest("article")!);
    expect(busyCard.getByRole("button", { name: "Use" })).toBeVisible();
    expect(busyCard.queryByRole("link", { name: "Check status" })).toBeNull();

    const providerAction = screen.getByRole("link", {
      name: "Connect a provider",
    });
    expect(providerAction).toHaveAttribute("href", "/ui/providers/");
  });

  it("keeps omitted configuration fields as not reported and gives members a status action", async () => {
    const availability = {
      available: false,
      status: "unknown" as const,
      reason: "Not measured yet.",
      measured: false,
      evidence: {},
    };
    renderModels(catalog([row({ availability })]), member);

    const action = await screen.findByRole("link", { name: "Check status" });
    expect(action).toHaveAttribute("href", "/ui/status/");
    expect(screen.getByText("Configuration not reported")).toBeVisible();
    expect(screen.getByText("Not observed yet")).toBeVisible();
    expect(screen.getByText("Observation time not reported")).toBeVisible();
    expect(screen.queryByText("Catalog only")).toBeNull();
  });

  it("shows distinctions and one role action for unavailable direct model rows", async () => {
    renderModels(
      catalog(
        [],
        [
          row({
            id: "remote-model",
            object: "model_catalog.model",
            display_name: "Remote model",
            name: "Remote model",
            alias: false,
            locality: "remote",
            provider: "Example provider",
            engine_label: "Example provider",
            cost: { kind: "paid", price: "$0.10 per 1M tokens" },
            availability: {
              available: false,
              status: "unavailable",
              reason: "The provider is disabled.",
              measured: true,
              enabled: false,
              configured: true,
              up: false,
              idle_startable: false,
              evidence: { source: "registry", observed_at_s: 1700000000 },
            },
          }),
        ],
      ),
      owner,
    );

    fireEvent.click(await screen.findByRole("button", { name: /All models/ }));
    const table = within(screen.getByRole("table"));

    expect(table.getByText("Configured")).toBeVisible();
    expect(table.getByText("From configuration")).toBeVisible();
    expect(table.getByText("Disabled")).toBeVisible();
    expect(table.getByText("Observed:")).toBeVisible();
    expect(table.getByTitle("2023-11-14 22:13:20 UTC")).toBeVisible();
    expect(
      table.getByRole("link", { name: "Review provider" }),
    ).toHaveAttribute("href", "/ui/providers/");
    expect(table.queryByRole("link", { name: "Review engine" })).toBeNull();
  });
});
