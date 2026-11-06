import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { adminFetch, ApiError, api, type AccessPlan, type AdminFetchInit } from "@/lib/api";
import { ViewerContext } from "@/components/shell/session";
import CategoriesPage from "./page";
import { addPreset, deleteBlocker, itemsIn, moveItem, saveCategory, categoryOf, type Preset } from "./simple";
import type { CatalogService, Snapshot } from "./editor";
import type { Audience, AudiencePreview } from "./audience-preview";

vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return { ...original, adminFetch: vi.fn() };
});
for (const method of ["hasPointerCapture", "setPointerCapture", "releasePointerCapture", "scrollIntoView"]) {
  Object.defineProperty(HTMLElement.prototype, method, { configurable: true, value: () => false });
}
beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.mocked(adminFetch).mockReset();
});

const NOW = "2026-10-13T12:00:00Z";
const fact = (category: string) => ({ category, value: "included", source: "owner", accepted: true, observed_at: NOW, accepted_at: NOW, accepting_owner: "usr_owner", mapping_version: "owner-edit-1" });
const service = (stem: string, kind: "workflow" | "release", category?: string) => ({
  id: stem + "@r1", checkable: true, dependencies: [],
  members: [{ id: stem + "@r1", kind, assertions: category ? [fact(category)] : [] }],
});
const category = (id: string, label: string, description: string) => ({
  id, label, description, kind: "set", values: ["included"], applies_to: ["release", "workflow"],
});
const rules = (over: Partial<Snapshot> = {}): Snapshot => ({
  id: "owner-rules", completeness_gate: true, people: {}, apps: {}, retained: {},
  categories: {
    adult: category("adult", "Adult", "For adults only."),
    experimental: category("experimental", "Experimental", "Still being built."),
  },
  services: { "tiles@r1": service("tiles", "workflow", "experimental") },
  ...over,
});
const catalog: CatalogService[] = [
  { stem: "tiles", label: "Tile maker", kind: "workflow", capabilities: ["image.generate"] },
  { stem: "painter", label: "Painter", kind: "workflow", capabilities: ["image.generate"] },
  { stem: "chatty", label: "Chatty", kind: "model", capabilities: ["chat.completions"] },
];
const presetList: Preset[] = [
  { id: "adult", label: "Adult", description: "Content for adults only." },
  { id: "experimental", label: "Experimental", description: "Still being built, so it may not work yet." },
  { id: "shared", label: "Shared", description: "Fine for everyone." },
];
/** The access plans the page reads: each plan's id, name and the categories it lists ("all", or the ids). */
const accessPlanList = (family: "all" | string[], guest: "all" | string[]): AccessPlan[] => [
  { id: "plan_family", name: "Family", created_at: 0, updated_at: 0, plan: { categories: family } as AccessPlan["plan"], summary: null },
  { id: "plan_guest", name: "Guest", created_at: 0, updated_at: 0, plan: { categories: guest } as AccessPlan["plan"], summary: null },
];
const active = (snapshot: Snapshot) => ({ revision: 4, parent_revision: 3, active_counter: 4, snapshot, created_at: NOW, activated_at: NOW, author: "usr_owner", reason: "x" });
const none: Audience = { gains: [], losses: [], cannot_tell: [] };
const gained = { item_id: "adult-flow", category_id: "adult",category_label: "Adult", app_id: "app-personal", app_name: "Personal", person_id: "person-family", person_label: "River", person_plan_id: "plan_family", app_plan_id: "plan_family" };
const lost = { item_id: "kids-flow", category_id: "experimental",category_label: "Experimental", app_id: "app-kids", app_name: "", person_id: "person-alex", person_label: null, person_plan_id: "plan_guest", app_plan_id: "plan_guest" };
const unreadable = { app_id: "app-old", app_name: null, person_id: "person-mia", person_label: "Mia", person_plan_id: null, app_plan_id: "plan_guest", reason: "person plan missing or unreadable" };

type Posted = { path: string; body: { base_revision?: number | null; expected_active_revision?: number | null; reason?: string; snapshot?: Snapshot } };
function mount({ snapshot = rules() as Snapshot | null, role = "owner", fail, refreshed, failActivation = false, presets = presetList, accessPlans = accessPlanList(["experimental"], []), audience = none, audienceReason = null }: { snapshot?: Snapshot | null; role?: "owner" | "admin" | "member"; fail?: ApiError; refreshed?: Snapshot; failActivation?: boolean; presets?: Preset[]; accessPlans?: AccessPlan[]; audience?: Audience | null; audienceReason?: AudiencePreview["audience_reason"] } = {}) {
  const posted: Posted[] = [];
  vi.spyOn(api.users, "list").mockResolvedValue([]);
  vi.spyOn(api.apps, "list").mockResolvedValue([{
    id: "app-personal", name: "Personal", kind: "personal", owner_user_id: "person-family",
    accountable_person: null, access_plan: "person", same_as_person: true,
    access_plan_name: "Family", effective_access_plan_id: "plan_family", created_at: 0, disabled_at: null,
  }]);
  vi.spyOn(api.accessPlans, "list").mockResolvedValue(accessPlans);
  const fetchCategory = vi.mocked(adminFetch);
  fetchCategory.mockImplementation((async (path: string, init?: AdminFetchInit) => {
    if (init?.method === "POST") {
      posted.push({ path, body: init.body as Posted["body"] });
      if (fail && (!failActivation || path.endsWith("/activate"))) { if (refreshed) snapshot = refreshed; throw fail; }
      return path.endsWith("/candidates") ? { revision: 5, preview: { audience, audience_reason: audienceReason } } : { revision: 5, active_counter: 5 };
    }
    if (path.endsWith("/active")) {
      if (!snapshot) throw new ApiError("no_active_snapshot", "None", 409);
      return active(snapshot);
    }
    if (path.endsWith("/history")) return { history: [] };
    if (path.endsWith("/services")) return { services: catalog, presets };
    throw new Error("Unexpected path: " + path);
  }) as never);
  render(<ViewerContext.Provider value={{ authenticated: true, name: "Sam", email: null, role, admin: role !== "member" }}>
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}>
      <CategoriesPage />
    </QueryClientProvider>
  </ViewerContext.Provider>);
  return { posted, fetchCategory };
}

it.each(["member", "admin"] as const)("keeps %s out without fetching owner data", (role) => {
  const { fetchCategory } = mount({ role });
  expect(screen.getByText("Only the owner can manage categories")).toBeVisible();
  expect(fetchCategory).not.toHaveBeenCalled();
});

it("lists each category with its meaning, the access plans that include it, and what is in it", async () => {
  mount({ accessPlans: accessPlanList(["experimental"], ["experimental", "adult"]) });
  const experimental = await screen.findByRole("region", { name: "Experimental" });
  expect(within(experimental).getByText("Still being built.")).toBeVisible();
  expect(await within(experimental).findByText("Family, Guest")).toBeVisible();
  expect(within(experimental).getByText("Tile maker")).toBeVisible();
  expect(within(experimental).getByText("Workflow")).toBeVisible();
  const adult = screen.getByRole("region", { name: "Adult" });
  expect(within(adult).getByText("For adults only.")).toBeVisible();
  expect(await within(adult).findByText("Guest")).toBeVisible();
  expect(within(adult).getByText("Nothing in Adult yet")).toBeVisible();
});

it("says when no access plan includes a category, and when there are no plans at all", async () => {
  mount({ accessPlans: accessPlanList([], []) });
  const adult = await screen.findByRole("region", { name: "Adult" });
  expect(await within(adult).findByText("No access plan includes it yet")).toBeVisible();
  cleanup();
  mount({ accessPlans: [] });
  const again = await screen.findByRole("region", { name: "Adult" });
  expect(await within(again).findByText("No access plans yet")).toBeVisible();
});

it("opens the category named in the address, where Check access links to it, and ignores one that is gone", async () => {
  window.history.pushState({}, "", "/categories/?category=adult");
  try {
    mount();
    expect(await screen.findByRole("heading", { name: "Edit Adult" })).toBeVisible();
    expect(screen.getByRole("textbox", { name: /Name/ })).toHaveValue("Adult");
    cleanup();
    window.history.pushState({}, "", "/categories/?category=gone");
    mount();
    await screen.findByRole("region", { name: "Adult" });
    expect(screen.queryByRole("dialog")).toBeNull();
  } finally {
    window.history.pushState({}, "", "/");
  }
});

it("explains what categories are based on without jargon", async () => {
  mount();
  await screen.findByRole("region", { name: "Adult" });
  const how = screen.getByText("How categories work").closest("details")!;
  expect(how).toHaveTextContent("Access plans decide who may use what is in a category");
  expect(how).toHaveTextContent("Nothing is filed automatically");
  expect(how.textContent).not.toMatch(/snapshot|assertion|closure|revision|principal/i);
});

it("starts empty with one action, then adds a category with a name and a meaning only", async () => {
  const { posted } = mount({ snapshot: null });
  const user = userEvent.setup();
  expect(await screen.findByText("No categories yet")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Add category" }));
  const dialog = await screen.findByRole("dialog", { name: "Add category" });
  const save = within(dialog).getByRole("button", { name: "Add category" });
  expect(save).toBeDisabled();
  await user.type(within(dialog).getByLabelText(/Name/), "Adult");
  await user.type(within(dialog).getByLabelText("What it is for"), "For adults only.");
  await user.click(save);
  await waitFor(() => expect(posted).toHaveLength(2));
  expect(posted[0].path).toBe("/admin/api/categories/candidates");
  expect(posted[0].body.base_revision).toBeNull();
  expect(posted[0].body.snapshot?.categories.adult).toMatchObject({ label: "Adult", description: "For adults only.", values: ["included"] });
  expect(posted[0].body.snapshot?.categories.adult).not.toHaveProperty("visible_to");
  expect(posted[1]).toMatchObject({ path: "/admin/api/categories/candidates/5/activate", body: { expected_active_revision: null } });
});

it("offers presets on an empty page and adds one as an ordinary category", async () => {
  const { posted } = mount({ snapshot: null });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Start from a preset" }));
  const dialog = await screen.findByRole("dialog", { name: "Start from a preset" });
  expect(within(dialog).getByText("Content for adults only.")).toBeVisible();
  expect(within(dialog).getByText("Still being built, so it may not work yet.")).toBeVisible();
  await user.click(within(dialog).getByRole("button", { name: "Add Adult" }));
  await waitFor(() => expect(posted).toHaveLength(2));
  expect(posted[0].body.base_revision).toBeNull();
  expect(posted[0].body.snapshot?.categories.adult).toEqual({
    id: "adult", label: "Adult", kind: "set", values: ["included"], applies_to: ["release", "workflow"],
    description: "Content for adults only.",
  });
  expect(Object.keys(posted[0].body.snapshot!.categories)).toEqual(["adult"]);
});

it("marks presets that are already added and offers only the rest", async () => {
  const { posted } = mount();
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Start from a preset" }));
  const dialog = await screen.findByRole("dialog", { name: "Start from a preset" });
  expect(within(dialog).getByText(/Adult is already added/)).toBeInTheDocument();
  expect(within(dialog).getByText(/Experimental is already added/)).toBeInTheDocument();
  expect(within(dialog).queryByRole("button", { name: "Add Adult" })).toBeNull();
  await user.click(within(dialog).getByRole("button", { name: "Add Shared" }));
  await waitFor(() => expect(posted).toHaveLength(2));
  expect(posted[0].body.snapshot?.categories.shared.label).toBe("Shared");
  expect(posted[0].body.snapshot?.categories.adult.label).toBe("Adult");
});

it("hides the preset button when the gateway ships no presets", async () => {
  mount({ presets: [] });
  await screen.findByRole("region", { name: "Adult" });
  expect(screen.queryByRole("button", { name: "Start from a preset" })).toBeNull();
});

it("offers no audience editor: the form has a name and a meaning, and points to People for plans", async () => {
  mount();
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Edit Adult" }));
  const dialog = await screen.findByRole("dialog", { name: "Edit Adult" });
  expect(within(dialog).queryByRole("radio")).toBeNull();
  expect(within(dialog).queryByRole("checkbox")).toBeNull();
  expect(within(dialog).getByText(/Each plan lists its categories under People/)).toBeVisible();
});

it("moves an item out of its category so it is available again", async () => {
  const { posted } = mount();
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Move Tile maker" }));
  const dialog = await screen.findByRole("dialog", { name: "Move Tile maker" });
  expect(within(dialog).getByRole("radio", { name: "Experimental" })).toBeChecked();
  await user.click(within(dialog).getByRole("radio", { name: "No category" }));
  expect(within(dialog).getByText("Move Tile maker to No category.")).toBeVisible();
  await user.click(within(dialog).getByRole("button", { name: "Move" }));
  await waitFor(() => expect(posted).toHaveLength(2));
  expect(posted[0].body.snapshot?.services).toEqual({});
});

it("moves an item from one category to another", async () => {
  const { posted } = mount();
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Move Tile maker" }));
  const dialog = await screen.findByRole("dialog", { name: "Move Tile maker" });
  await user.click(within(dialog).getByRole("radio", { name: "Adult" }));
  await user.click(within(dialog).getByRole("button", { name: "Move" }));
  await waitFor(() => expect(posted).toHaveLength(2));
  const moved = posted[0].body.snapshot!.services["tiles@r1"].members[0].assertions;
  expect(moved.map((a) => a.category)).toEqual(["adult"]);
});

it("adds items from the catalog, offering only what is not already there", async () => {
  const { posted } = mount();
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Add items to Adult" }));
  const dialog = await screen.findByRole("dialog", { name: "Add items to Adult" });
  const add = within(dialog).getByRole("button", { name: "Add item" });
  expect(add).toBeDisabled();
  expect(await within(dialog).findByText(/Now in Experimental/)).toBeVisible();
  await user.type(within(dialog).getByLabelText("Search"), "chat");
  expect(within(dialog).queryByRole("checkbox", { name: "Painter" })).toBeNull();
  await user.click(within(dialog).getByRole("checkbox", { name: "Chatty" }));
  await user.click(add);
  await waitFor(() => expect(posted).toHaveLength(2));
  const created = posted[0].body.snapshot!.services["chatty@r1"];
  expect(created.members[0]).toMatchObject({ id: "chatty@r1", kind: "release" });
  expect(created.members[0].assertions[0]).toMatchObject({ category: "adult", value: "included", accepted: true });
});

it("deletes a category only after a confirmation that says who gains access", async () => {
  const { posted } = mount();
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Edit Experimental" }));
  const dialog = await screen.findByRole("dialog", { name: "Edit Experimental" });
  await user.click(within(dialog).getByRole("button", { name: "Delete category" }));
  const confirm = await screen.findByRole("dialog", { name: "Delete Experimental?" });
  expect(confirm).toHaveTextContent("Anyone who could not use them before will be able to");
  expect(posted).toHaveLength(0);
  await user.click(within(confirm).getByRole("button", { name: "Delete category" }));
  await waitFor(() => expect(posted).toHaveLength(2));
  expect(Object.keys(posted[0].body.snapshot!.categories)).toEqual(["adult"]);
  expect(posted[0].body.snapshot!.services).toEqual({});
});

it("refuses to delete a category that an access rule still uses", async () => {
  const plan = { id: "kids", grants: [], require: [{ id: "r", category: "adult", op: "exists", values: [] }], deny: [], missing: {}, mode: "v3", legacy_tags: [] };
  mount({ snapshot: rules({ people: { mia: [plan] } }) });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Edit Adult" }));
  const dialog = await screen.findByRole("dialog", { name: "Edit Adult" });
  expect(within(dialog).getByRole("button", { name: "Delete category" })).toBeDisabled();
  expect(within(dialog).getByText(/access rule still uses this category/)).toBeVisible();
});

it("says when the categories changed underneath, and keeps the dialog open", async () => {
  mount({ fail: new ApiError("stale_base", "stale", 409) });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Edit Adult" }));
  const dialog = await screen.findByRole("dialog", { name: "Edit Adult" });
  await user.click(within(dialog).getByRole("button", { name: "Save" }));
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("Categories changed while you were editing");
});

it("shows who a category change reaches before it is turned on, and waits for Turn on", async () => {
  const { posted } = mount({ audience: { gains: [gained], losses: [lost], cannot_tell: [unreadable] } });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Edit Adult" }));
  const dialog = await screen.findByRole("dialog", { name: "Edit Adult" });
  await user.type(within(dialog).getByLabelText(/Name/), " changed");
  await user.click(within(dialog).getByRole("button", { name: "Save" }));
  const review = await screen.findByRole("dialog", { name: "Turn on these category rules?" });
  expect(within(review).getByRole("heading", { name: "Who this change reaches" })).toBeVisible();
  expect(within(review).getByText("Can now use")).toBeVisible();
  expect(within(review).getByText("Personal for River: adult-flow (Adult)")).toBeVisible();
  expect(within(review).getByText("Can no longer use")).toBeVisible();
  expect(within(review).getByText("Unnamed app for no person: kids-flow (Experimental)")).toBeVisible();
  expect(within(review).getByText("Can't tell")).toBeVisible();
  expect(within(review).getByText("Unnamed app for Mia: person plan missing or unreadable")).toBeVisible();
  expect(within(review).getByText(/Apps and people added later are not listed/)).toBeVisible();
  expect(posted.map((call) => call.path)).toEqual(["/admin/api/categories/candidates"]);
  await user.click(within(review).getByRole("button", { name: "Turn on" }));
  await waitFor(() => expect(posted).toHaveLength(2));
  expect(posted[1]).toMatchObject({ path: "/admin/api/categories/candidates/5/activate", body: { expected_active_revision: 4 } });
});

it("says the gateway could not check who a category change reaches, and still lets the owner turn it on", async () => {
  const { posted } = mount({ audience: null, audienceReason: "admin_store_unreadable" });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Edit Adult" }));
  const dialog = await screen.findByRole("dialog", { name: "Edit Adult" });
  await user.type(within(dialog).getByLabelText(/Name/), " changed");
  await user.click(within(dialog).getByRole("button", { name: "Save" }));
  const review = await screen.findByRole("dialog", { name: "Turn on these category rules?" });
  expect(within(review).getByText("We couldn't check who this change reaches.")).toBeVisible();
  expect(posted).toHaveLength(1);
  await user.click(within(review).getByRole("button", { name: "Turn on" }));
  await waitFor(() => expect(posted).toHaveLength(2));
  expect(posted[1].path).toBe("/admin/api/categories/candidates/5/activate");
});

it("turns a category change on at once, with no confirmation, when nobody gains or loses a category", async () => {
  const { posted } = mount({ audience: none });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Edit Adult" }));
  const dialog = await screen.findByRole("dialog", { name: "Edit Adult" });
  await user.type(within(dialog).getByLabelText(/Name/), " changed");
  await user.click(within(dialog).getByRole("button", { name: "Save" }));
  await waitFor(() => expect(posted).toHaveLength(2));
  expect(posted[1]).toMatchObject({ path: "/admin/api/categories/candidates/5/activate", body: { expected_active_revision: 4 } });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(screen.queryByRole("dialog", { name: "Turn on these category rules?" })).toBeNull();
});

it("turns nothing on when the owner cancels, and keeps the edit form open", async () => {
  const { posted } = mount({ audience: { gains: [gained], losses: [], cannot_tell: [] } });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Edit Adult" }));
  const dialog = await screen.findByRole("dialog", { name: "Edit Adult" });
  await user.type(within(dialog).getByLabelText(/Name/), " changed");
  await user.click(within(dialog).getByRole("button", { name: "Save" }));
  const review = await screen.findByRole("dialog", { name: "Turn on these category rules?" });
  await user.click(within(review).getByRole("button", { name: "Cancel" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Turn on these category rules?" })).toBeNull());
  expect(posted.map((call) => call.path)).toEqual(["/admin/api/categories/candidates"]);
  expect(screen.getByRole("dialog", { name: "Edit Adult" })).toBeVisible();
});

it("says so on the form when the rules changed before the owner turned the change on", async () => {
  const { posted } = mount({ audience: { gains: [gained], losses: [], cannot_tell: [] }, fail: new ApiError("stale_base", "stale", 409), failActivation: true });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Edit Adult" }));
  const dialog = await screen.findByRole("dialog", { name: "Edit Adult" });
  await user.type(within(dialog).getByLabelText(/Name/), " changed");
  await user.click(within(dialog).getByRole("button", { name: "Save" }));
  const review = await screen.findByRole("dialog", { name: "Turn on these category rules?" });
  await user.click(within(review).getByRole("button", { name: "Turn on" }));
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("Categories changed while you were editing");
  expect(screen.queryByRole("dialog", { name: "Turn on these category rules?" })).toBeNull();
  expect(posted.map((call) => call.path)).toEqual(["/admin/api/categories/candidates", "/admin/api/categories/candidates/5/activate"]);
});

it("keeps the old access rules under Advanced", async () => {
  mount();
  await screen.findByRole("region", { name: "Adult" });
  expect(screen.getByText("Advanced: rules for specific people and apps").closest("details")).not.toBeNull();
});

// --- the pure helpers the page is built on ---------------------------------

it("files an item under its stem and reuses the revision it already has", () => {
  const entry = { stem: "tiles", label: "Tile maker", kind: "workflow" as const, capabilities: [] };
  const moved = moveItem(rules(), entry, "adult", NOW);
  expect(Object.keys(moved.services)).toEqual(["tiles@r1"]);
  expect(itemsIn(moved, "adult", catalog).map((item) => item.label)).toEqual(["Tile maker"]);
  expect(itemsIn(moved, "experimental", catalog)).toEqual([]);
});

it("keeps a service that an access rule grants by name when its last item leaves", () => {
  const plan = { id: "p", grants: [{ capability: "image.generate", service: "tiles@r1" }], require: [], deny: [], missing: {}, mode: "v3", legacy_tags: [] };
  const entry = { stem: "tiles", label: "Tile maker", kind: "workflow" as const, capabilities: [] };
  const left = moveItem(rules({ people: { mia: [plan] } }), entry, null, NOW);
  expect(Object.keys(left.services)).toEqual(["tiles@r1"]);
});

it("adds a preset only once and keeps the owner's edits to an existing category", () => {
  const edited = rules();
  edited.categories.adult.label = "Grown-up";
  expect(addPreset(edited, presetList[0])).toBe(edited);
  const added = addPreset(edited, presetList[2]);
  expect(Object.keys(added.categories)).toEqual(["adult", "experimental", "shared"]);
  expect(added.categories.adult.label).toBe("Grown-up");
});

it("makes a unique id for a new category and leaves the others alone", () => {
  const first = saveCategory(rules(), { label: "Adult", description: "" });
  expect(first.id).toBe("adult-2");
  expect(Object.keys(first.snapshot.categories)).toEqual(["adult", "experimental", "adult-2"]);
  expect(deleteBlocker(rules(), "adult")).toBeNull();
});

it.each(["None", "Uncategorized"])("reserves the %s category id", (label) => {
  const first = saveCategory(rules(), { label, description: "" });
  expect(first.id).toBe(label.toLowerCase() + "-2");
  expect(saveCategory(first.snapshot, { label, description: "" }).id).toBe(label.toLowerCase() + "-3");
});

it.each(["none", "uncategorized"])("moves into a legacy %s category without clearing membership", async (id) => {
  const snapshot = rules();
  snapshot.categories[id] = category(id, "None", "");
  const { posted } = mount({ snapshot });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Move Tile maker" }));
  const dialog = await screen.findByRole("dialog");
  const destination = within(dialog).getByRole("radio", { name: "None" });
  const unfiled = within(dialog).getByRole("radio", { name: "No category" });
  expect(destination).not.toHaveAttribute("value", unfiled.getAttribute("value"));
  await user.click(destination);
  await user.click(within(dialog).getByRole("button", { name: "Move" }));
  await waitFor(() => expect(posted).toHaveLength(2));
  expect(categoryOf(posted[0].body.snapshot!, "tiles")).toBe(id);
});

it("creates None, moves into it, then explicitly moves to No category", async () => {
  const created = saveCategory(rules(), { label: "None", description: "" });
  expect(created.id).toBe("none-2");
  const { posted } = mount({ snapshot: created.snapshot });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Move Tile maker" }));
  await user.click(screen.getByRole("radio", { name: "None" }));
  await user.click(screen.getByRole("button", { name: "Move" }));
  await waitFor(() => expect(posted).toHaveLength(2));
  expect(categoryOf(posted[0].body.snapshot!, "tiles")).toBe(created.id);
  const moved = posted[0].body.snapshot!;
  cleanup();
  const second = mount({ snapshot: moved });
  await user.click(await screen.findByRole("button", { name: "Move Tile maker" }));
  await user.click(screen.getByRole("radio", { name: "No category" }));
  await user.click(screen.getByRole("button", { name: "Move" }));
  await waitFor(() => expect(second.posted).toHaveLength(2));
  expect(categoryOf(second.posted[0].body.snapshot!, "tiles")).toBeNull();
});

it("the move dialog names the access plans that include each destination", async () => {
  mount({ accessPlans: accessPlanList(["experimental"], ["adult"]) });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Move Tile maker" }));
  const dialog = await screen.findByRole("dialog", { name: "Move Tile maker" });
  expect(within(dialog).getByText("Family")).toBeVisible();
  expect(within(dialog).getByText("Guest")).toBeVisible();
  await user.click(within(dialog).getByRole("radio", { name: "Adult" }));
  expect(within(dialog).getByText("Move Tile maker to Adult.")).toBeVisible();
});

it("summarises a bulk add: how many picked items leave another category", async () => {
  mount();
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Add items to Adult" }));
  const dialog = await screen.findByRole("dialog");
  await user.click(within(dialog).getByRole("checkbox", { name: "Tile maker" }));
  await user.click(within(dialog).getByRole("checkbox", { name: "Chatty" }));
  expect(within(dialog).getByText("1 selected item will leave another category.")).toBeVisible();
  expect(within(dialog).getByText("Move Tile maker to Adult.")).toBeVisible();
  expect(within(dialog).getByText("Move Chatty to Adult.")).toBeVisible();
  await user.click(within(dialog).getByRole("checkbox", { name: "Tile maker" }));
  expect(within(dialog).getByText("0 selected items will leave another category.")).toBeVisible();
});

it("resets stale edits to latest fields and blocks an unchanged second save", async () => {
  const refreshed = rules();
  refreshed.categories.adult = category("adult", "Latest", "Latest meaning");
  const { posted } = mount({ fail: new ApiError("stale_base", "stale", 409), refreshed });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Edit Adult" }));
  await user.type(screen.getByLabelText(/Name/), " old");
  await user.click(screen.getByRole("button", { name: "Save" }));
  const dialog = await screen.findByRole("dialog", { name: "Edit Latest" });
  await waitFor(() => expect(within(dialog).getByLabelText(/Name/)).toHaveValue("Latest"));
  expect(within(dialog).getByLabelText("What it is for")).toHaveValue("Latest meaning");
  expect(within(dialog).getByRole("alert")).toHaveTextContent("Categories changed while you were editing. Review the latest settings before saving.");
  const save = within(dialog).getByRole("button", { name: "Save" });
  expect(save).toBeDisabled();
  await user.click(save);
  expect(posted).toHaveLength(1);
  await user.type(within(dialog).getByLabelText(/Name/), " reviewed");
  expect(save).toBeEnabled();
});

it("closes a stale editor when its category was deleted", async () => {
  const refreshed = rules();
  delete refreshed.categories.adult;
  const { posted } = mount({ fail: new ApiError("stale_base", "stale", 409), refreshed });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Edit Adult" }));
  await user.type(screen.getByLabelText(/Name/), " changed");
  await user.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(screen.queryByRole("dialog") !== null).toBe(false));
  expect(screen.getByRole("status")).toHaveTextContent("This category was deleted while you were editing.");
  expect(posted).toHaveLength(1);
});

it("does not claim a failed activation was definitely unsaved", async () => {
  const { posted } = mount({ fail: new ApiError("network_error", "lost", 500), failActivation: true });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Edit Adult" }));
  await user.type(screen.getByLabelText(/Name/), " changed");
  await user.click(screen.getByRole("button", { name: "Save" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("We could not confirm that this change was saved. Reload the categories before trying again.");
  expect(posted).toHaveLength(2);
});

it.each(["", "   "])("refuses an empty category name %j even on direct form submission", async (name) => {
  const { posted } = mount();
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Add category" }));
  const dialog = await screen.findByRole("dialog", { name: "Add category" });
  fireEvent.change(within(dialog).getByLabelText(/Name/), { target: { value: name } });
  expect(within(dialog).getByRole("button", { name: "Add category" })).toBeDisabled();
  fireEvent.submit(dialog.querySelector("form")!);
  expect(posted).toEqual([]);
});

it("blocks deletion when only an app deny rule uses the category", async () => {
  const plan = { id: "app-only", grants: [], require: [], deny: [{ id: "deny-adult", category: "adult", op: "exists", values: [] }], missing: {}, mode: "v3", legacy_tags: [] };
  const { posted } = mount({ snapshot: rules({ people: {}, apps: { studio: [plan] } }) });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Edit Adult" }));
  const dialog = await screen.findByRole("dialog", { name: "Edit Adult" });
  const remove = within(dialog).getByRole("button", { name: "Delete category" });
  expect(remove).toBeDisabled();
  await user.click(remove);
  expect(screen.queryByRole("dialog", { name: "Delete Adult?" })).toBeNull();
  expect(posted).toEqual([]);
});

it("moves an item without discarding advanced assertions or retained evidence", () => {
  const snapshot = rules();
  snapshot.categories.audience = { id: "audience", label: "Audience", kind: "set", values: ["general", "adult"], applies_to: ["workflow"] };
  const evidence = { ...fact("audience"), value: "general" };
  snapshot.services["tiles@r1"].members[0].assertions.push(evidence);
  snapshot.retained.saved = JSON.parse(JSON.stringify(snapshot.services["tiles@r1"]));
  const before = JSON.parse(JSON.stringify(snapshot));
  const moved = moveItem(snapshot, catalog[0], "adult", NOW);
  expect(moved.services["tiles@r1"].members[0].assertions).toEqual([
    evidence, { ...fact("adult"), accepting_owner: "" },
  ]);
  expect(moved.retained).toEqual(before.retained);
  expect(snapshot).toEqual(before);
});

it("keeps the tail of a long category list available as a move destination", async () => {
  const snapshot = rules();
  for (let n = 0; n < 60; n++) {
    const id = "group-" + n;
    snapshot.categories[id] = category(id, "Group " + n, "");
  }
  const { posted } = mount({ snapshot });
  const user = userEvent.setup();
  await screen.findByRole("region", { name: "Adult" });
  expect(screen.queryByRole("region", { name: "Group 59" }) !== null).toBe(true);
  await user.click(screen.getByRole("button", { name: "Move Tile maker" }));
  const dialog = await screen.findByRole("dialog", { name: "Move Tile maker" });
  expect(within(dialog).getAllByRole("radio")).toHaveLength(63);
  await user.click(within(dialog).getByRole("radio", { name: "Group 59" }));
  await user.click(within(dialog).getByRole("button", { name: "Move" }));
  await waitFor(() => expect(posted).toHaveLength(2));
  expect(posted[0].body.snapshot!.services["tiles@r1"].members[0].assertions.map((a) => a.category)).toEqual(["group-59"]);
});

it("retains every category edit after activation fails and retries the same draft", async () => {
  const { posted, fetchCategory } = mount();
  const serve = fetchCategory.getMockImplementation()!;
  let activationFailed = false;
  fetchCategory.mockImplementation((async (path: string, init?: AdminFetchInit) => {
    if (path.endsWith("/activate") && !activationFailed) {
      activationFailed = true;
      posted.push({ path, body: init!.body as Posted["body"] });
      throw new ApiError("save_failed", "Activation failed", 503);
    }
    return serve(path, init);
  }) as never);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Edit Adult" }));
  const dialog = await screen.findByRole("dialog", { name: "Edit Adult" });
  const name = within(dialog).getByLabelText(/Name/);
  const description = within(dialog).getByLabelText("What it is for");
  await user.clear(name);
  await user.type(name, "   ");
  expect(within(dialog).getByRole("button", { name: "Save" })).toBeDisabled();
  expect(posted).toHaveLength(0);
  await user.clear(name);
  await user.type(name, "Preview");
  await user.clear(description);
  await user.type(description, "Keep this carefully edited meaning.");
  await user.click(within(dialog).getByRole("button", { name: "Save" }));
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("We could not confirm that this change was saved. Reload the categories before trying again.");
  expect(name).toHaveValue("Preview");
  expect(description).toHaveValue("Keep this carefully edited meaning.");
  await waitFor(() => expect(within(dialog).getByRole("button", { name: "Save" })).toBeEnabled());
  expect(posted).toHaveLength(2);
  await user.click(within(dialog).getByRole("button", { name: "Save" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Edit Adult" })).toBeNull());
  expect(posted).toHaveLength(4);
  expect(posted[2].body.snapshot).toEqual(posted[0].body.snapshot);
  expect(posted[2].body.snapshot?.categories.adult).toMatchObject({
    label: "Preview", description: "Keep this carefully edited meaning.",
  });
  expect(posted[3].body.expected_active_revision).toBe(4);
});

