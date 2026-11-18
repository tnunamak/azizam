import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactElement } from "react";
import ModelsPage from "@/app/models/page";
import WorkflowsPage from "@/app/workflows/page";
import { ViewerContext, type Viewer } from "@/components/shell/session";
import { productApi } from "@/lib/product-api";
import { accountApi } from "@/lib/account-api";

vi.mock("@/lib/product-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/product-api")>();
  return { ...actual, productApi: { ...actual.productApi, models: vi.fn() } };
});
vi.mock("@/lib/account-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/account-api")>();
  return { ...actual, accountApi: { ...actual.accountApi, discovery: vi.fn() } };
});

const member: Viewer = {
  authenticated: true, name: "Member", email: "member@example.test", role: "member", admin: false,
};
const clients: QueryClient[] = [];
function mount(page: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  clients.push(client);
  return render(
    <ViewerContext.Provider value={member}>
      <QueryClientProvider client={client}>{page}</QueryClientProvider>
    </ViewerContext.Provider>,
  );
}
beforeEach(() => {
  vi.mocked(productApi.models).mockReset();
  vi.mocked(productApi.models).mockResolvedValue({
    schema_version: 1, object: "models.catalog", aliases: [], models: [],
    counts: { aliases: 0, models: 0, capabilities: 0 },
  });
  vi.mocked(accountApi.discovery).mockReset();
  vi.mocked(accountApi.discovery).mockResolvedValue({
    recipes: { openai: { canonical_base_url: "https://gateway.example.test/openai/v1" } },
  });
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
    schema_version: 1, object: "workflows.catalog", workflows: [],
  }), { status: 200, headers: { "content-type": "application/json" } })));
  Element.prototype.hasPointerCapture ??= () => false;
  Element.prototype.releasePointerCapture ??= () => {};
  Element.prototype.scrollIntoView ??= () => {};
});
afterEach(() => {
  cleanup();
  for (const client of clients.splice(0)) client.clear();
  vi.unstubAllGlobals();
});
function expectDiscovery(current: "Models" | "Workflows", heading: HTMLElement) {
  const nav = screen.getByRole("navigation", { name: "Discovery" });
  const models = within(nav).getByRole("link", { name: "Models" });
  const workflows = within(nav).getByRole("link", { name: "Workflows" });
  expect(models.getAttribute("href")).toBe("/models");
  expect(workflows.getAttribute("href")).toBe("/workflows");
  expect((current === "Models" ? models : workflows).getAttribute("aria-current")).toBe("page");
  expect((current === "Models" ? workflows : models).hasAttribute("aria-current")).toBe(false);
  expect(nav.compareDocumentPosition(heading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
}
describe("Discovery navigation on mounted pages", () => {
  it("renders the models page with Models current in the Discovery landmark", async () => {
    mount(<ModelsPage />);
    await waitFor(() => expect(productApi.models).toHaveBeenCalled());
    expectDiscovery("Models", screen.getByRole("heading", { level: 1, name: /^Models/ }));
  });
  it("renders the empty workflows page with Workflows current in the Discovery landmark", async () => {
    mount(<WorkflowsPage />);
    await screen.findByText("No workflows are available to you.");
    expectDiscovery("Workflows", screen.getByRole("heading", { level: 1, name: /^Workflows/ }));
    expect(fetch).toHaveBeenCalled();
  });
});
