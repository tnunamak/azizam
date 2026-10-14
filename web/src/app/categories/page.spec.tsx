import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { adminFetch, ApiError, api, type AdminFetchInit } from "@/lib/api";
import { ViewerContext } from "@/components/shell/session";
import CategoriesPage from "./page";
import { addPreset, deleteBlocker, itemsIn, moveItem, saveCategory, visibilityText, categoryOf, type Preset } from "./simple";
import type { CatalogService, Snapshot } from "./editor";

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
const category = (id: string, label: string, description: string, visible_to: string[] | null) => ({
  id, label, description, visible_to, kind: "set", values: ["included"], applies_to: ["release", "workflow"],
});
const rules = (over: Partial<Snapshot> = {}): Snapshot => ({
  id: "owner-rules", completeness_gate: true, people: {}, apps: {}, retained: {},
  categories: {
    adult: category("adult", "Adult", "For adults only.", []),
    experimental: category("experimental", "Experimental", "Still being built.", ["plan_family"]),
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
  { id: "adult", label: "Adult", description: "Content for adults only.", visibility: "owner" },
  { id: "experimental", label: "Experimental", description: "Still being built, so it may not work yet.", visibility: "owner" },
  { id: "shared", label: "Shared", description: "Fine for everyone.", visibility: "everyone" },
];
const active = (snapshot: Snapshot) => ({ revision: 4, parent_revision: 3, active_counter: 4, snapshot, created_at: NOW, activated_at: NOW, author: "usr_owner", reason: "x" });

type Posted = { path: string; body: { base_revision?: number | null; expected_active_revision?: number | null; reason?: string; snapshot?: Snapshot } };
function mount({ snapshot = rules() as Snapshot | null, role = "owner", fail, refreshed, failActivation = false, presets = presetList }: { snapshot?: Snapshot | null; role?: "owner" | "admin" | "member"; fail?: ApiError; refreshed?: Snapshot; failActivation?: boolean; presets?: Preset[] } = {}) {
  const posted: Posted[] = [];
  vi.spyOn(api.users, "list").mockResolvedValue([]);
  vi.spyOn(api.apps, "list").mockResolvedValue([]);
  vi.spyOn(api.accessPlans, "list").mockResolvedValue([
    { id: "plan_family", name: "Family", created_at: 0, updated_at: 0, plan: {} as never, summary: null },
    { id: "plan_guest", name: "Guest", created_at: 0, updated_at: 0, plan: {} as never, summary: null },
  ]);
  const fetchCategory = vi.mocked(adminFetch);
  fetchCategory.mockImplementation((async (path: string, init?: AdminFetchInit) => {
    if (init?.method === "POST") {
      posted.push({ path, body: init.body as Posted["body"] });
      if (fail && (!failActivation || path.endsWith("/activate"))) { if (refreshed) snapshot = refreshed; throw fail; }
      return path.endsWith("/candidates") ? { revision: 5 } : { revision: 5, active_counter: 5 };
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

it("lists each category with its meaning, who can see it and what is in it", async () => {
  mount();
  const experimental = await screen.findByRole("region", { name: "Experimental" });
  expect(within(experimental).getByText("Still being built.")).toBeVisible();
  expect(await within(experimental).findByText(/You, and people and apps on Family/)).toBeVisible();
  expect(within(experimental).getByText("Tile maker")).toBeVisible();
  expect(within(experimental).getByText("Workflow")).toBeVisible();
  const adult = screen.getByRole("region", { name: "Adult" });
  expect(within(adult).getByText("For adults only.")).toBeVisible();
  expect(within(adult).getByText("Only you")).toBeVisible();
  expect(within(adult).getByText("Nothing in Adult yet")).toBeVisible();
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
  expect(how).toHaveTextContent("who may see what is in it");
  expect(how).toHaveTextContent("Nothing is filed automatically");
  expect(how.textContent).not.toMatch(/snapshot|assertion|closure|revision|principal/i);
});

it("starts empty with one action, then adds a category visible to the owner only", async () => {
  const { posted } = mount({ snapshot: null });
  const user = userEvent.setup();
  expect(await screen.findByText("No categories yet")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Add category" }));
  const dialog = await screen.findByRole("dialog", { name: "Add category" });
  const save = within(dialog).getByRole("button", { name: "Add category" });
  expect(save).toBeDisabled();
  await user.type(within(dialog).getByLabelText(/Name/), "Adult");
  await user.type(within(dialog).getByLabelText("What it is for"), "For adults only.");
  expect(within(dialog).getByRole("radio", { name: /Only me/ })).toBeChecked();
  await user.click(save);
  await waitFor(() => expect(posted).toHaveLength(2));
  expect(posted[0].path).toBe("/admin/api/categories/candidates");
  expect(posted[0].body.base_revision).toBeNull();
  expect(posted[0].body.snapshot?.categories.adult).toMatchObject({ label: "Adult", description: "For adults only.", visible_to: [], values: ["included"] });
  expect(posted[1]).toMatchObject({ path: "/admin/api/categories/candidates/5/activate", body: { expected_active_revision: null } });
});

it("offers presets on an empty page and adds one as an ordinary owner-only category", async () => {
  const { posted } = mount({ snapshot: null });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Start from a preset" }));
  const dialog = await screen.findByRole("dialog", { name: "Start from a preset" });
  expect(within(dialog).getByText("Content for adults only.")).toBeVisible();
  expect(within(dialog).getByText("Still being built, so it may not work yet.")).toBeVisible();
  expect(within(dialog).getAllByText("At first only you can see it.")).toHaveLength(2);
  expect(within(dialog).getByText("At first everyone can see it.")).toBeVisible();
  await user.click(within(dialog).getByRole("button", { name: "Add Adult" }));
  await waitFor(() => expect(posted).toHaveLength(2));
  expect(posted[0].body.base_revision).toBeNull();
  expect(posted[0].body.snapshot?.categories.adult).toEqual({
    id: "adult", label: "Adult", kind: "set", values: ["included"], applies_to: ["release", "workflow"],
    description: "Content for adults only.", visible_to: [],
  });
  expect(Object.keys(posted[0].body.snapshot!.categories)).toEqual(["adult"]);
});

it("marks presets that are already added and offers only the rest", async () => {
  const { posted } = mount();
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Start from a preset" }));
  const dialog = await screen.findByRole("dialog", { name: "Start from a preset" });
  expect(within(dialog).getByLabelText("Adult is already added")).toBeVisible();
  expect(within(dialog).getByLabelText("Experimental is already added")).toBeVisible();
  expect(within(dialog).queryByRole("button", { name: "Add Adult" })).toBeNull();
  await user.click(within(dialog).getByRole("button", { name: "Add Shared" }));
  await waitFor(() => expect(posted).toHaveLength(2));
  expect(posted[0].body.snapshot?.categories.shared.visible_to).toBeNull();
  expect(posted[0].body.snapshot?.categories.adult.label).toBe("Adult");
});

it("hides the preset button when the gateway ships no presets", async () => {
  mount({ presets: [] });
  await screen.findByRole("region", { name: "Adult" });
  expect(screen.queryByRole("button", { name: "Start from a preset" })).toBeNull();
});

it("chooses access plans, and warns before making a category visible to everyone", async () => {
  const { posted } = mount();
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Edit Adult" }));
  const dialog = await screen.findByRole("dialog", { name: "Edit Adult" });
  await user.click(within(dialog).getByRole("radio", { name: /Me and chosen access plans/ }));
  expect(within(dialog).getByRole("button", { name: "Save" })).toBeDisabled();
  await user.click(within(dialog).getByRole("checkbox", { name: "Guest" }));
  await user.click(within(dialog).getByRole("button", { name: "Save" }));
  await waitFor(() => expect(posted).toHaveLength(2));
  expect(posted[0].body.snapshot?.categories.adult.visible_to).toEqual(["plan_guest"]);
  expect(posted[1].body.expected_active_revision).toBe(4);
});

it("tells the owner what Everyone means before saving", async () => {
  mount();
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Edit Adult" }));
  const dialog = await screen.findByRole("dialog", { name: "Edit Adult" });
  await user.click(within(dialog).getByRole("radio", { name: /^Everyone/ }));
  expect(within(dialog).getByText(/available to everyone/)).toBeVisible();
});

it("moves an item out of its category so it is available again", async () => {
  const { posted } = mount();
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Move Tile maker" }));
  const dialog = await screen.findByRole("dialog", { name: "Move Tile maker" });
  expect(within(dialog).getByRole("radio", { name: "Experimental" })).toBeChecked();
  await user.click(within(dialog).getByRole("radio", { name: "No category" }));
  expect(within(dialog).getByText(/everyone who can use the gateway can see and use it/)).toBeVisible();
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
  expect(confirm).toHaveTextContent("Anyone who could not see them before will be able to");
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

it("words who can see a category, and names a plan that was removed", () => {
  const name = (id: string) => (id === "plan_family" ? "Family" : "a removed access plan");
  expect(visibilityText(category("a", "A", "", null), name)).toBe("Everyone who can use the gateway");
  expect(visibilityText(category("a", "A", "", []), name)).toBe("Only you");
  expect(visibilityText(category("a", "A", "", ["plan_family", "gone"]), name)).toBe("You, and people and apps on Family, a removed access plan");
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
  const first = saveCategory(rules(), { label: "Adult", description: "", visibility: "owner", plans: [] });
  expect(first.id).toBe("adult-2");
  expect(Object.keys(first.snapshot.categories)).toEqual(["adult", "experimental", "adult-2"]);
  expect(deleteBlocker(rules(), "adult")).toBeNull();
});

it.each(["None", "Uncategorized"])("reserves the %s category id", (label) => {
  const first = saveCategory(rules(), { label, description: "", visibility: "owner", plans: [] });
  expect(first.id).toBe(label.toLowerCase() + "-2");
  expect(saveCategory(first.snapshot, { label, description: "", visibility: "owner", plans: [] }).id).toBe(label.toLowerCase() + "-3");
});

it.each(["none", "uncategorized"])("moves into a legacy %s category without clearing membership", async (id) => {
  const snapshot = rules();
  snapshot.categories[id] = category(id, "None", "", []);
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
  const created = saveCategory(rules(), { label: "None", description: "", visibility: "owner", plans: [] });
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

it.each([true, false])("shows move audiences and warns only when widening: %s", async (widening) => {
  const snapshot = rules();
  snapshot.categories.experimental.visible_to = widening ? [] : null;
  snapshot.categories.adult.visible_to = widening ? null : [];
  mount({ snapshot });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Move Tile maker" }));
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).queryByText("Only you")).not.toBeNull();
  expect(within(dialog).getAllByText("Everyone who can use the gateway").length).toBeGreaterThan(0);
  await user.click(within(dialog).getByRole("radio", { name: "Adult" }));
  const oldAudience = widening ? "Only you" : "Everyone who can use the gateway";
  const newAudience = widening ? "Everyone who can use the gateway" : "Only you";
  expect(within(dialog).getByText("Move Tile maker to Adult. Who can see it changes from " + oldAudience + " to " + newAudience + ".")).toBeVisible();
  expect(within(dialog).queryByText(/Anyone who could not see/ ) !== null).toBe(widening);
});

it.each([true, false])("summarises bulk moves and warns only when widening: %s", async (widening) => {
  const snapshot = rules();
  snapshot.categories.experimental.visible_to = widening ? [] : null;
  snapshot.categories.adult.visible_to = widening ? null : [];
  mount({ snapshot });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Add items to Adult" }));
  const dialog = await screen.findByRole("dialog");
  await user.click(within(dialog).getByRole("checkbox", { name: "Tile maker" }));
  await user.click(within(dialog).getByRole("checkbox", { name: "Chatty" }));
  expect(within(dialog).queryByText("1 selected item will leave another category.")).not.toBeNull();
  expect(within(dialog).getByText(/Who can see it: /)).toHaveTextContent(widening ? "Everyone who can use the gateway" : "Only you");
  expect(within(dialog).queryByText(/Anyone who could not see/) !== null).toBe(widening);
  await user.click(within(dialog).getByRole("checkbox", { name: "Tile maker" }));
  expect(within(dialog).getByText("0 selected items will leave another category.")).toBeVisible();
});

it("resets stale edits to latest fields and blocks an unchanged second save", async () => {
  const refreshed = rules();
  refreshed.categories.adult = category("adult", "Latest", "Latest meaning", ["plan_family"]);
  const { posted } = mount({ fail: new ApiError("stale_base", "stale", 409), refreshed });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Edit Adult" }));
  await user.type(screen.getByLabelText(/Name/), " old");
  await user.click(screen.getByRole("radio", { name: /^Everyone/ }));
  await user.click(screen.getByRole("button", { name: "Save" }));
  const dialog = await screen.findByRole("dialog", { name: "Edit Latest" });
  await waitFor(() => expect(within(dialog).getByLabelText(/Name/)).toHaveValue("Latest"));
  expect(within(dialog).getByLabelText("What it is for")).toHaveValue("Latest meaning");
  expect(within(dialog).getByRole("radio", { name: /Me and chosen access plans/ })).toBeChecked();
  expect(within(dialog).getByRole("checkbox", { name: "Family" })).toBeChecked();
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
