import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api, type AccessPlan, type AccessPlanDocument } from "@/lib/api";
import { PlanEditor, planShapeError } from "@/app/people/plans";

const document = (overrides: Partial<AccessPlanDocument> = {}): AccessPlanDocument => ({
  name: "Family",
  can_use: { chat: { items: "all" }, image: { items: [{ model: "flux" }] } },
  direct_engine_access: { engines: [], operations: [] },
  paid_providers: [],
  provider_fallback: false,
  can_load_models: true,
  receipt_detail: "basic",
  media_metadata: "none",
  ...overrides,
});
const plan = (doc: AccessPlanDocument): AccessPlan => ({
  id: "plan_family",
  name: doc.name,
  created_at: 1,
  updated_at: 1,
  plan: { ...doc, id: "plan_family" },
  summary: null,
});

function mount(doc: AccessPlanDocument | null) {
  vi.spyOn(api.accessPlans, "editor").mockResolvedValue({
    schema_version: 2,
    capabilities: [
      { id: "chat", label: "Chat", items: [] },
      { id: "image", label: "Images", items: [
        { kind: "model", id: "flux", available: true },
        { kind: "workflow", id: "sdxl", available: true },
      ] },
      { id: "video", label: "Video", items: [] },
    ],
    engines: [{ id: "llama", label: "llama", available: true }],
    paid_providers: [{ id: "openrouter", label: "OpenRouter", available: true }],
    receipt_detail: [{ id: "basic", label: "Basic", description: null }, { id: "full", label: "Full", description: null }],
    media_metadata: [{ id: "none", label: "Strip", description: null }, { id: "full", label: "Keep", description: null }],
    new_plan: document({ name: "", can_use: {} }),
  });
  const update = vi.spyOn(api.accessPlans, "update").mockResolvedValue({} as never);
  const create = vi.spyOn(api.accessPlans, "create").mockResolvedValue({} as never);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <PlanEditor plan={doc ? plan(doc) : null} close={vi.fn()} saved={vi.fn(async () => {})} />
    </QueryClientProvider>,
  );
  return { update, create };
}

async function canUse() {
  const legend = await screen.findByText("Can use", { selector: "legend" });
  return within(legend.closest("fieldset") as HTMLElement);
}

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Access plan editor", () => {
  it("shows what the plan grants: all items or only listed ones", async () => {
    mount(document());
    const set = await canUse();
    expect(set.getByRole("checkbox", { name: "Chat" })).toBeChecked();
    expect(set.getByRole("checkbox", { name: "Images" })).toBeChecked();
    expect(set.getByRole("checkbox", { name: "Video" })).not.toBeChecked();
    expect(set.getByRole("checkbox", { name: /^All images models/ })).not.toBeChecked();
    expect(set.getByRole("checkbox", { name: "flux" })).toBeChecked();
    expect(set.getByRole("checkbox", { name: "sdxl" })).not.toBeChecked();
  });

  it("saves the whole document without an id", async () => {
    const { update } = mount(document());
    const set = await canUse();
    await userEvent.click(set.getByRole("checkbox", { name: "Chat" }));
    await userEvent.click(set.getByRole("checkbox", { name: "sdxl" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "OpenRouter" }));
    await userEvent.click(screen.getByRole("button", { name: "Save access plan" }));
    const [, body] = update.mock.calls[0];
    expect(update.mock.calls[0][0]).toBe("plan_family");
    expect(body).not.toHaveProperty("id");
    expect(body.can_use).toEqual({ image: { items: [{ model: "flux" }, { workflow: "sdxl" }] } });
    expect(body.paid_providers).toEqual(["openrouter"]);
  });

  it("a new plan grants nothing until capabilities are chosen", async () => {
    const { create } = mount(null);
    const set = await canUse();
    expect(set.getByRole("checkbox", { name: "Chat" })).not.toBeChecked();
    await userEvent.type(screen.getByLabelText("Name"), "Kids");
    await userEvent.click(set.getByRole("checkbox", { name: "Video" }));
    await userEvent.click(screen.getByRole("button", { name: "Save access plan" }));
    expect(create.mock.calls[0][0].can_use).toEqual({ video: { items: "all" } });
  });

  it("refuses a pasted document that misses a field", () => {
    expect(planShapeError({ name: "x" })).toBe("The plan needs the field can_use.");
    expect(planShapeError(document())).toBeNull();
  });
});
