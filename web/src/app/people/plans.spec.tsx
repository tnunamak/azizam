import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { adminFetch, api, type AccessPlan, type AccessPlanDocument } from "@/lib/api";
import type { Audience } from "@/app/categories/audience-preview";
import { PlanEditor, planShapeError } from "@/app/people/plans";

vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return { ...original, adminFetch: vi.fn() };
});

const document = (overrides: Partial<AccessPlanDocument> = {}): AccessPlanDocument => ({
  name: "Family",
  can_use: { chat: { items: "all" }, image: { items: [{ model: "flux" }] } },
  direct_engine_access: { engines: [], operations: [] },
  paid_providers: [],
  provider_fallback: false,
  caps: { usd_per_request: null, usd_per_day: null, usd_per_month: null },
  can_load_models: true,
  receipt_detail: "basic",
  media_metadata: "none",
  categories: "all",
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

const none: Audience = { gains: [], losses: [], cannot_tell: [] };
const categoriesSnapshot = {
  id: "owner-rules", completeness_gate: true, people: {}, apps: {}, retained: {}, services: {},
  categories: {
    adult: { id: "adult", label: "Adult", description: "For adults only.", kind: "set", values: ["included"], applies_to: ["release", "workflow"] },
    experimental: { id: "experimental", label: "Experimental", description: "", kind: "set", values: ["included"], applies_to: ["release", "workflow"] },
  },
};
/** A change that takes Adult away from one person's app. */
const losesAdult: Audience = {
  gains: [],
  losses: [{ item_id: "adult-flow", category_id: "adult", category_label: "Adult", app_id: "app-kids", app_name: "Kids app", person_id: "person-alex", person_label: "Alex", person_plan_id: "plan_family", app_plan_id: "plan_family" }],
  cannot_tell: [],
};

/** `reach` is what the preview says; null means the gateway could not check. */
function mount(doc: AccessPlanDocument | null, reach: Audience | null = none) {
  return mountWith(doc, async () => ({ audience: reach, audience_reason: reach ? null : "admin_store_unreadable" }));
}

/** `planReach` answers each preview request, so a test can hold one open or make one fail. */
function mountWith(doc: AccessPlanDocument | null, planReach: () => Promise<unknown>) {
  vi.mocked(adminFetch).mockReset();
  vi.mocked(adminFetch).mockImplementation((async (path: string) => {
    if (path === "/admin/api/categories/active") return { revision: 3, snapshot: categoriesSnapshot };
    if (path === "/admin/api/categories/plan-reach") return planReach();
    throw new Error("Unexpected path: " + path);
  }) as never);
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
    new_plan: document({ name: "", can_use: {}, categories: [] }),
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

  it("can use every type of work, including ones added later, or only the ones chosen", async () => {
    const { update } = mount(document({ can_use: "all" }));
    expect(await screen.findByRole("radio", { name: "All types of work, including ones added later" })).toBeChecked();
    const set = await canUse();
    expect(set.queryByRole("checkbox", { name: "Chat" })).toBeNull();
    await userEvent.click(set.getByRole("radio", { name: "Only these" }));
    expect(set.getByRole("checkbox", { name: "Chat" })).not.toBeChecked();
    await userEvent.click(set.getByRole("checkbox", { name: "Chat" }));
    await userEvent.click(screen.getByRole("button", { name: "Save access plan" }));
    expect(update.mock.calls[0][1].can_use).toEqual({ chat: { items: "all" } });
  });

  it("saves can_use as \"all\" when the owner chooses every type of work", async () => {
    const { update } = mount(document());
    await userEvent.click((await canUse()).getByRole("radio", { name: "All types of work, including ones added later" }));
    await userEvent.click(screen.getByRole("button", { name: "Save access plan" }));
    expect(update.mock.calls[0][1].can_use).toBe("all");
  });

  it("the raw JSON editor accepts can_use \"all\" and saves it", async () => {
    const { update } = mount(document());
    await canUse();
    const json = screen.getByLabelText("Plan JSON");
    fireEvent.change(json, { target: { value: JSON.stringify({ ...document(), can_use: "all" }) } });
    expect(json).toHaveAttribute("aria-invalid", "false");
    await userEvent.click(screen.getByRole("button", { name: "Save access plan" }));
    expect(update.mock.calls[0][1].can_use).toBe("all");
  });

  it("saves spending caps as dollar amounts, and an empty box as no cap", async () => {
    const { update } = mount(document({
      caps: { usd_per_request: "0.50", usd_per_day: null, usd_per_month: null },
    }));
    await canUse();
    expect(screen.getByLabelText("Per request")).toHaveValue("0.50");
    expect(screen.getByLabelText("Per day")).toHaveValue("");
    await userEvent.type(screen.getByLabelText("Per day"), "5");
    await userEvent.clear(screen.getByLabelText("Per request"));
    await userEvent.click(screen.getByRole("button", { name: "Save access plan" }));
    expect(update.mock.calls[0][1].caps).toEqual({
      usd_per_request: null, usd_per_day: "5", usd_per_month: null,
    });
  });

  it("refuses a pasted document that misses a field", () => {
    expect(planShapeError({ name: "x" })).toBe("The plan needs the field can_use.");
    const { caps: _caps, ...withoutCaps } = document();
    expect(planShapeError(withoutCaps)).toBe("The plan needs the field caps.");
    const { categories: _categories, ...withoutCategories } = document();
    expect(planShapeError(withoutCategories)).toBe("The plan needs the field categories.");
    expect(planShapeError(document())).toBeNull();
  });

  it("refuses a can_use that is neither \"all\" nor an object keyed by capability", () => {
    const withCanUse = (can_use: unknown) => ({ ...document(), can_use });
    const message = 'The can_use must be "all" or an object keyed by capability.';
    for (const can_use of [null, "chat", ["chat"], 3]) {
      expect(planShapeError(withCanUse(can_use))).toBe(message);
    }
    expect(planShapeError(withCanUse("all"))).toBeNull();
    expect(planShapeError(withCanUse({}))).toBeNull();
  });

  it("refuses categories that are neither \"all\" nor a list of non-empty ids, so the list never gets them", () => {
    const withCategories = (categories: unknown) => ({ ...document(), categories });
    const message = 'The categories must be "all" or a list of category ids.';
    for (const categories of [null, {}, "adult", [""], [3]]) {
      expect(planShapeError(withCategories(categories))).toBe(message);
    }
    expect(planShapeError(withCategories("all"))).toBeNull();
    expect(planShapeError(withCategories(["adult"]))).toBeNull();
  });

  it("includes every category, including ones added later, or only the ones chosen", async () => {
    const { update } = mount(document({ categories: "all" }));
    expect(await screen.findByRole("radio", { name: "All categories, including ones added later" })).toBeChecked();
    expect(screen.queryByRole("checkbox", { name: "Adult" })).toBeNull();
    await userEvent.click(screen.getByRole("radio", { name: "Only the categories I choose" }));
    await userEvent.click(await screen.findByRole("checkbox", { name: "Adult" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Save access plan" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Save access plan" }));
    expect(update.mock.calls[0][1].categories).toEqual(["adult"]);
  });

  it("saves only the categories a plan lists", async () => {
    const { update } = mount(document({ categories: ["adult"] }));
    await userEvent.click(await screen.findByRole("checkbox", { name: "Adult" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Experimental" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Save access plan" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Save access plan" }));
    expect(update.mock.calls[0][1].categories).toEqual(["experimental"]);
  });

  it("a new plan lists no categories until the owner chooses some", async () => {
    const { create } = mount(null);
    await userEvent.type(await screen.findByLabelText("Name"), "Kids");
    expect(await screen.findByRole("radio", { name: "Only the categories I choose" })).toBeChecked();
    await userEvent.click(screen.getByRole("button", { name: "Save access plan" }));
    expect(create.mock.calls[0][0].categories).toEqual([]);
  });

  it("shows who a change to the categories reaches, before Save, and does not check an unchanged list", async () => {
    const { update } = mount(document({ categories: ["adult"] }), losesAdult);
    await screen.findByRole("checkbox", { name: "Adult" });
    expect(screen.queryByRole("region", { name: "Who this change reaches" })).toBeNull();
    expect(vi.mocked(adminFetch)).not.toHaveBeenCalledWith("/admin/api/categories/plan-reach", expect.anything());
    await userEvent.click(screen.getByRole("checkbox", { name: "Adult" }));
    const reach = await screen.findByRole("region", { name: "Who this change reaches" });
    expect(within(reach).getByText("Can no longer use")).toBeVisible();
    expect(within(reach).getByText("Kids app for Alex: adult-flow (Adult)")).toBeVisible();
    expect(vi.mocked(adminFetch)).toHaveBeenLastCalledWith(
      "/admin/api/categories/plan-reach",
      expect.objectContaining({
        method: "POST",
        body: expect.objectContaining({ plan_id: "plan_family", plan: expect.objectContaining({ categories: [] }) }),
      }),
    );
    expect(update).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Save access plan" }));
    expect(update.mock.calls[0][1].categories).toEqual([]);
  });

  it("says the gateway could not check who a change reaches, and saves only when the owner chooses to", async () => {
    const { update } = mount(document({ categories: ["adult"] }), null);
    await userEvent.click(await screen.findByRole("checkbox", { name: "Adult" }));
    expect(await screen.findByText("We couldn't check who this change reaches.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Save access plan" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Save anyway" }));
    expect(update.mock.calls[0][1].categories).toEqual([]);
  });

  it("does not save while the preview is still being checked, even when the form is submitted", async () => {
    let answer!: (value: unknown) => void;
    const { update } = mountWith(document({ categories: [] }), () => new Promise((resolve) => { answer = resolve; }));
    await userEvent.click(await screen.findByRole("radio", { name: "All categories, including ones added later" }));
    const save = screen.getByRole("button", { name: "Save access plan" });
    expect(await screen.findByText("Checking who this change reaches…")).toBeVisible();
    expect(save).toBeDisabled();
    fireEvent.submit(save.closest("form") as HTMLFormElement);
    expect(update).not.toHaveBeenCalled();
    answer({ audience: none, audience_reason: null });
    await waitFor(() => expect(save).toBeEnabled());
    await userEvent.click(save);
    expect(update.mock.calls[0][1].categories).toBe("all");
  });

  it("blocks Save when the preview fails, and saves anyway only when the owner chooses to", async () => {
    const { update } = mountWith(document({ categories: ["adult"] }), () => Promise.reject(new Error("offline")));
    await userEvent.click(await screen.findByRole("checkbox", { name: "Adult" }));
    expect(await screen.findByText("We couldn't check who this change reaches.")).toBeVisible();
    const save = screen.getByRole("button", { name: "Save access plan" });
    expect(save).toBeDisabled();
    fireEvent.submit(save.closest("form") as HTMLFormElement);
    expect(update).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Save anyway" }));
    expect(update.mock.calls[0][1].categories).toEqual([]);
  });

  it("asks for a name before it checks who a change reaches", async () => {
    mount(document({ categories: ["adult"] }), losesAdult);
    await userEvent.clear(await screen.findByLabelText("Name"));
    await userEvent.click(await screen.findByRole("checkbox", { name: "Adult" }));
    expect(screen.getByText("Enter a name to see who this change reaches.")).toBeVisible();
    expect(screen.queryByRole("region", { name: "Who this change reaches" })).toBeNull();
    expect(vi.mocked(adminFetch)).not.toHaveBeenCalledWith("/admin/api/categories/plan-reach", expect.anything());
  });

  it.each(["null", "{}", '"adult"', '[""]', "[3]"])(
    "refuses a pasted document whose categories are %s, keeps the last valid choice, and blocks Save",
    async (categories) => {
      mount(document({ categories: ["adult"] }));
      await screen.findByRole("checkbox", { name: "Adult" });
      fireEvent.change(screen.getByLabelText("Plan JSON"), {
        target: { value: JSON.stringify({ ...document(), categories: JSON.parse(categories) }) },
      });
      expect(await screen.findByRole("alert")).toHaveTextContent('"all" or a list of category ids');
      expect(screen.getByRole("checkbox", { name: "Adult" })).toBeChecked();
      expect(screen.getByRole("button", { name: "Save access plan" })).toBeDisabled();
      await userEvent.click(screen.getByRole("button", { name: "Discard invalid JSON changes" }));
      expect(screen.getByRole("button", { name: "Save access plan" })).toBeEnabled();
    },
  );

  it("takes a valid list of categories from the pasted JSON", async () => {
    mount(document({ categories: ["adult"] }));
    await screen.findByRole("checkbox", { name: "Adult" });
    fireEvent.change(screen.getByLabelText("Plan JSON"), {
      target: { value: JSON.stringify({ ...document(), categories: ["experimental"] }) },
    });
    expect(await screen.findByRole("checkbox", { name: "Experimental" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Adult" })).not.toBeChecked();
  });
});
