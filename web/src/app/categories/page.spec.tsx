import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { adminFetch, ApiError, api, type AdminFetchInit } from "@/lib/api";
import { ViewerContext } from "@/components/shell/session";
import CategoriesPage from "./page";
import { deleteBlocker, itemsIn, moveItem, saveCategory, visibilityText } from "./simple";
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
const active = (snapshot: Snapshot) => ({ revision: 4, parent_revision: 3, active_counter: 4, snapshot, created_at: NOW, activated_at: NOW, author: "usr_owner", reason: "x" });

type Posted = { path: string; body: { base_revision?: number | null; expected_active_revision?: number | null; reason?: string; snapshot?: Snapshot } };
function mount({ snapshot = rules() as Snapshot | null, role = "owner", fail }: { snapshot?: Snapshot | null; role?: "owner" | "admin" | "member"; fail?: ApiError } = {}) {
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
      if (fail) throw fail;
      return path.endsWith("/candidates") ? { revision: 5 } : { revision: 5, active_counter: 5 };
    }
    if (path.endsWith("/active")) {
      if (!snapshot) throw new ApiError("no_active_snapshot", "None", 409);
      return active(snapshot);
    }
    if (path.endsWith("/history")) return { history: [] };
    if (path.endsWith("/services")) return { services: catalog };
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
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("The categories changed while you were editing");
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

it("makes a unique id for a new category and leaves the others alone", () => {
  const first = saveCategory(rules(), { label: "Adult", description: "", visibility: "owner", plans: [] });
  expect(first.id).toBe("adult-2");
  expect(Object.keys(first.snapshot.categories)).toEqual(["adult", "experimental", "adult-2"]);
  expect(deleteBlocker(rules(), "adult")).toBeNull();
});
