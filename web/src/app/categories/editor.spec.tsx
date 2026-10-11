import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { adminFetch, ApiError, type AdminFetchInit } from "@/lib/api";
import { CategoriesEditor, categoryId, isPinned, prepareSnapshot, rebuildSnapshot, updateCurrentService, validateSnapshot, valueInUse, type Snapshot } from "./editor";

vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return { ...original, adminFetch: vi.fn() };
});
for (const method of ["hasPointerCapture", "setPointerCapture", "releasePointerCapture", "scrollIntoView"]) {
  Object.defineProperty(HTMLElement.prototype, method, { configurable: true, value: () => false });
}
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.mocked(adminFetch).mockReset(); });
const copy = <T,>(value: T): T => JSON.parse(JSON.stringify(value));
function rules(): Snapshot {
  const service = {
    id: "studio@1", checkable: true, dependencies: [],
    members: [{ id: "model@1", kind: "release", assertions: [
      { category: "audience", value: "general", accepted: true, source: "Owner review", observed_at: "2026-10-09", accepted_at: "2026-10-10", accepting_owner: "sam", mapping_version: "map-1" },
      { category: "audience", value: { raw: "Unreviewed import" }, accepted: false, source: "Import", observed_at: "2026-10-08", accepted_at: "", accepting_owner: "", mapping_version: "map-0" },
    ] }],
  };
  const plan = {
    id: "family", grants: [{ capability: "image.generate", service: "studio@1", release: "model@1", workflow: "workflow@1" }],
    require: [{ id: "kids-audience", category: "audience", op: "in", values: ["general"] }],
    deny: [{ id: "no-training", category: "data-handling", op: "in", values: ["may-train"] }],
    missing: { "kids-audience": "allow" }, mode: "v3", legacy_tags: [],
  };
  return {
    id: "rules-2", completeness_gate: true,
    categories: {
      audience: { id: "audience", label: "Audience", kind: "set", values: ["general", "adult"], applies_to: ["release", "route", "workflow"] },
      "data-handling": { id: "data-handling", label: "Data handling", kind: "set", values: ["may-train", "no-training"], applies_to: ["route"] },
    },
    people: { sam: [plan] }, apps: { studio: [{ ...copy(plan), id: "app-plan" }] },
    services: { "studio@1": service }, retained: { "output-1": copy(service) },
  };
}
const request = { person: "sam", app: "studio", capability: "image.generate", service: "studio@1" };
const denied = { outcome: "deny", reason: "REQUIRE_MISMATCH", rule: "kids-audience", category: "audience", member: "model@1", waivers: [] };
const allowed = { outcome: "allow", reason: "ALLOW", rule: "", category: "", member: "", waivers: [] };
const preview = { gains: [request], losses: [{ ...request, service: "other@1" }], reason_changes: [{ request, before: denied, after: allowed }], scope: [request] };
function candidate(snapshot = rules(), base: number | null = 2) {
  return { revision: (base ?? 0) + 1, snapshot, preview_base_revision: base, preview };
}
function serve(path: string, init?: AdminFetchInit): unknown {
  if (path.endsWith("/candidates")) {
    const body = init!.body as { snapshot: Snapshot; base_revision: number | null };
    return candidate(body.snapshot, body.base_revision);
  }
  if (path.endsWith("/activate")) return { revision: 3, active_counter: 3 };
  if (path.endsWith("/active")) return { revision: 2, snapshot: rules() };
  throw new Error("Unexpected path: " + path);
}
const choices = { people: [{ id: "sam", label: "Sam" }], apps: [{ id: "studio", label: "Studio" }] };
function mount(snapshot: Snapshot | null = rules(), respond: (path: string, init?: AdminFetchInit) => unknown = serve) {
  vi.mocked(adminFetch).mockImplementation(async <T,>(path: string, init?: AdminFetchInit): Promise<T> => await respond(path, init) as T);
  const onClose = vi.fn(), onActivated = vi.fn();
  const props = { active: snapshot ? { revision: 2, snapshot } : null, owner: "sam", ...choices, onClose, onActivated };
  const view = render(<CategoriesEditor {...props} />);
  return { ...view, props, onClose, onActivated, fetch: vi.mocked(adminFetch), user: userEvent.setup() };
}
async function pick(user: ReturnType<typeof userEvent.setup>, label: string, option: string) {
  await user.click(screen.getByRole("combobox", { name: label }));
  await user.click(await screen.findByRole("option", { name: option }));
}
async function showPreview(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByRole("textbox", { name: "Reason for changes" }), "Reviewed access");
  await user.click(screen.getByRole("button", { name: "Preview changes" }));
  await screen.findByRole("region", { name: "Change preview" });
}

it("builds rules from scratch locally and posts the exact snapshot and null base", async () => {
  const { user, fetch } = mount(null);
  await user.type(screen.getByRole("textbox", { name: "New category label" }), "Audience");
  await user.click(screen.getByRole("button", { name: "Add category" }));
  await user.type(screen.getByRole("textbox", { name: "New value for Audience" }), "general");
  await user.click(screen.getByRole("button", { name: "Add value to Audience" }));
  await pick(user, "Person for new access plan", "Sam");
  await user.click(screen.getByRole("button", { name: "Add person access plan" }));
  await pick(user, "App for new access plan", "Studio");
  await user.click(screen.getByRole("button", { name: "Add app access plan" }));
  expect(fetch).not.toHaveBeenCalled();
  const emptyPlan = { id: "access-plan-1", grants: [], require: [], deny: [], missing: {}, mode: "v3", legacy_tags: [] };
  await showPreview(user);
  expect(fetch).toHaveBeenCalledOnce();
  expect(fetch).toHaveBeenCalledWith("/admin/api/categories/candidates", {
    method: "POST", body: { base_revision: null, reason: "Reviewed access", snapshot: {
      id: "owner-rules", categories: { audience: { id: "audience", label: "Audience", kind: "set", values: ["general"], applies_to: ["release", "route", "workflow"] } },
      people: { sam: [emptyPlan] }, apps: { studio: [emptyPlan] }, services: {}, completeness_gate: true, retained: {},
    } },
  });
});

it("round-trips unchanged fields including provenance, selectors, legacy fields and retained bundles", async () => {
  const original = rules();
  original.people.sam.push({ id: "legacy-plan", grants: [], require: [], deny: [], missing: {}, mode: "legacy", legacy_tags: ["reviewed"] });
  const expected = copy(original);
  expected.categories.audience.label = "Audience rating";
  const { user, fetch } = mount(original);
  await user.clear(screen.getByRole("textbox", { name: "Label for audience" }));
  await user.type(screen.getByRole("textbox", { name: "Label for audience" }), "Audience rating");
  await showPreview(user);
  expect(fetch).toHaveBeenCalledWith("/admin/api/categories/candidates", { method: "POST", body: { base_revision: 2, reason: "Reviewed access", snapshot: expected } });
  expect(original.categories.audience.label).toBe("Audience");
  expect(original.people.sam[1].legacy_tags).toEqual(["reviewed"]);
});

it("blocks removal of a value used by a person or app access plan", async () => {
  const snapshot = rules(); snapshot.services = {}; snapshot.retained = {};
  const { user, fetch } = mount(snapshot);
  await user.click(screen.getByRole("button", { name: "Remove General from Audience" }));
  expect(screen.getByRole("alert")).toHaveTextContent("General is used by an access plan or a service.");
  expect(screen.getByRole("button", { name: "Remove General from Audience" })).toBeVisible();
  expect(fetch).not.toHaveBeenCalled();
});

it("blocks value removal when only a retained output or unaccepted assertion uses it", async () => {
  const snapshot = rules(); snapshot.people = {}; snapshot.apps = {}; snapshot.services = {};
  snapshot.retained["output-1"].members[0].assertions[0].accepted = false;
  const { user } = mount(snapshot);
  await user.click(screen.getByRole("button", { name: "Remove General from Audience" }));
  expect(screen.getByRole("alert")).toHaveTextContent("Remove that use before removing this value.");
  expect(valueInUse(snapshot, "audience", "general")).toBe(true);
});

it("adds and removes an unused value without sending a request", async () => {
  const { user, fetch } = mount();
  await user.click(screen.getByRole("button", { name: "Remove Adult from Audience" }));
  expect(screen.queryByRole("button", { name: "Remove Adult from Audience" })).toBeNull();
  await user.type(screen.getByRole("textbox", { name: "New value for Audience" }), "teen");
  await user.click(screen.getByRole("button", { name: "Add value to Audience" }));
  expect(screen.getByRole("button", { name: "Remove Teen from Audience" })).toBeVisible();
  expect(fetch).not.toHaveBeenCalled();
});

it("records owner provenance and versions edited services without changing retained output evidence", async () => {
  const original = rules();
  const { user, fetch } = mount(original);
  const accepted = screen.getByRole("group", { name: "Accepted Audience for model@1" });
  await user.click(within(accepted).getByRole("checkbox", { name: "Adult" }));
  expect(screen.getByRole("region", { name: "Service grant changes" })).toHaveTextContent(/Sam · Family: studio@1 → studio@r2-[a-z0-9]+/);
  expect(screen.getByRole("region", { name: "Service grant changes" })).toHaveTextContent(/Studio · App plan: studio@1 → studio@r2-[a-z0-9]+/);
  expect(fetch).not.toHaveBeenCalled();
  await showPreview(user);
  const posted = (fetch.mock.calls[0][1]!.body as { snapshot: Snapshot }).snapshot;
  expect(posted.services["studio@1"]).toBeUndefined();
  const changed = Object.values(posted.services)[0];
  expect(changed.id).toMatch(/^studio@r2-[a-z0-9]+$/);
  expect(changed.members[0].assertions[0]).toEqual(original.services["studio@1"].members[0].assertions[0]);
  expect(changed.members[0].assertions[1]).toEqual(original.services["studio@1"].members[0].assertions[1]);
  expect(changed.members[0].assertions[2]).toEqual({
    category: "audience", value: "adult", accepted: true, source: "owner", accepting_owner: "sam", mapping_version: "owner-edit-1",
    observed_at: expect.any(String), accepted_at: expect.any(String),
  });
  expect(Number.isFinite(Date.parse(changed.members[0].assertions[2].accepted_at))).toBe(true);
  expect(posted.people.sam[0].grants[0]).toEqual({ ...original.people.sam[0].grants[0], service: changed.id });
  expect(posted.apps.studio[0].grants[0].service).toBe(changed.id);
  expect(posted.retained).toEqual(original.retained);
  expect(validateSnapshot(posted)).toEqual([]);
});

it("updates shared dependencies consistently and versions every changed ancestor", () => {
  const base = rules();
  const shared = copy(base.services["studio@1"]);
  base.services = {
    "first@r1": { id: "first@r1", checkable: true, members: [], dependencies: [copy(shared)] },
    "second@r1": { id: "second@r1", checkable: true, members: [], dependencies: [copy(shared)] },
  };
  base.people = {}; base.apps = {};
  const changed = copy(shared); changed.members[0].assertions[0].value = "adult";
  const draft = updateCurrentService(copy(base), changed);
  const posted = prepareSnapshot(draft, base, "test");
  expect(posted.services["first@r2-test"].dependencies[0].id).toBe("studio@r2-test");
  expect(posted.services["second@r2-test"].dependencies[0]).toEqual(posted.services["first@r2-test"].dependencies[0]);
  expect(posted.retained).toEqual(base.retained);
  expect(validateSnapshot(posted)).toEqual([]);
});

it("shows person and app required, deny and named waiver rows in plain words", () => {
  mount();
  expect(screen.getAllByText("Needs Audience: General")).toHaveLength(2);
  expect(screen.getAllByText("Never: Data handling: May train")).toHaveLength(2);
  expect(screen.getAllByText("Missing-value waiver for kids-audience: skips only this requirement when its value is missing.")).toHaveLength(2);
  expect(screen.getByRole("region", { name: "Person access plans" })).toBeVisible();
  expect(screen.getByRole("region", { name: "App access plans" })).toBeVisible();
});

it("keeps optional grant selectors when editing the capability", async () => {
  const { user, fetch } = mount();
  const section = within(screen.getByRole("region", { name: "Person access plans" }));
  await user.click(section.getByRole("combobox", { name: "Capability" }));
  await user.click(await screen.findByRole("option", { name: "Chat" }));
  await showPreview(user);
  const posted = (fetch.mock.calls[0][1]!.body as { snapshot: Snapshot }).snapshot;
  expect(posted.people.sam[0].grants[0]).toEqual({ ...rules().people.sam[0].grants[0], capability: "chat.completions" });
  expect(posted.apps).toEqual(rules().apps);
});

it("renames a requirement and carries its named missing-value waiver with it", async () => {
  const { user, fetch } = mount();
  const section = within(screen.getByRole("region", { name: "Person access plans" }));
  const rowName = section.getAllByRole("textbox", { name: "Rule name" })[0];
  await user.clear(rowName); await user.type(rowName, "reviewed-audience");
  await showPreview(user);
  const posted = (fetch.mock.calls[0][1]!.body as { snapshot: Snapshot }).snapshot;
  expect(posted.people.sam[0].missing).toEqual({ "reviewed-audience": "allow" });
});

it("shows returned gains, losses and reason changes before explicit compare-and-swap activation", async () => {
  const { user, fetch, onActivated } = mount();
  expect(screen.queryByRole("button", { name: "Turn on" })).toBeNull();
  await showPreview(user);
  const region = within(screen.getByRole("region", { name: "Change preview" }));
  expect(region.getByText("Who gains access")).toBeVisible();
  expect(region.getByText("Who loses access")).toBeVisible();
  expect(region.getByText("Reasons that change")).toBeVisible();
  expect(region.getByText(/An accepted value does not meet a required rule/)).toBeVisible();
  expect(region.getByText(/Every attached access plan allows this request/)).toBeVisible();
  expect(region.getByText(/does not cover unknown future requests/)).toBeVisible();
  expect(fetch.mock.calls.filter(([path]) => path.endsWith("/activate"))).toHaveLength(0);
  await user.click(region.getByRole("button", { name: "Turn on" }));
  await screen.findByText("Revision 3 is now selected for category previews.");
  expect(fetch).toHaveBeenCalledWith("/admin/api/categories/candidates/3/activate", { method: "POST", body: { expected_active_revision: 2 } });
  expect(onActivated).toHaveBeenCalledOnce();
  expect(screen.queryByRole("button", { name: "Turn on" })).toBeNull();
});

it("discards an obsolete candidate after a local edit or a reason change", async () => {
  const { user } = mount();
  await showPreview(user);
  await user.type(screen.getByRole("textbox", { name: "Reason for changes" }), " again");
  expect(screen.queryByRole("button", { name: "Turn on" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "Preview changes" }));
  await screen.findByRole("button", { name: "Turn on" });
  await user.type(screen.getByRole("textbox", { name: "Label for audience" }), " rating");
  expect(screen.queryByRole("button", { name: "Turn on" })).toBeNull();
});

it("rebuilds a stale candidate on fresh active rules without losing unrelated changes", async () => {
  let previews = 0;
  const latest = rules(); latest.categories.audience.label = "Latest audience";
  const { user, fetch } = mount(rules(), (path, init) => {
    if (path.endsWith("/candidates") && ++previews === 1) throw new ApiError("stale_base", "Changed", 409);
    if (path.endsWith("/active")) return { revision: 4, snapshot: latest };
    return serve(path, init);
  });
  await user.type(screen.getByRole("textbox", { name: "New category label" }), "New category");
  await user.click(screen.getByRole("button", { name: "Add category" }));
  await user.type(screen.getByRole("textbox", { name: "New value for New category" }), "reviewed");
  await user.click(screen.getByRole("button", { name: "Add value to New category" }));
  await user.type(screen.getByRole("textbox", { name: "Reason for changes" }), "Local change");
  await user.click(screen.getByRole("button", { name: "Preview changes" }));
  await user.click(await screen.findByRole("button", { name: "Rebuild on current rules" }));
  await screen.findByText("Your edit is rebuilt on the current rules. Preview changes again before turning them on.");
  expect(screen.getByRole("textbox", { name: "Label for audience" })).toHaveValue("Latest audience");
  expect(screen.getByRole("textbox", { name: "Label for new-category" })).toHaveValue("New category");
  await user.click(screen.getByRole("button", { name: "Preview changes" }));
  await screen.findByRole("button", { name: "Turn on" });
  const calls = fetch.mock.calls.filter(([path]) => path.endsWith("/candidates"));
  expect(calls).toHaveLength(2);
  expect(calls[1][1]!.body).toMatchObject({ base_revision: 4, reason: "Local change" });
  expect(fetch.mock.calls.some(([path]) => path.endsWith("/activate"))).toBe(false);
});

it("invalidates stale activation without retrying it and offers a rebuild", async () => {
  const { user, fetch } = mount(rules(), (path, init) => {
    if (path.endsWith("/activate")) throw new ApiError("stale_base", "Changed", 409);
    return serve(path, init);
  });
  await showPreview(user);
  await user.click(screen.getByRole("button", { name: "Turn on" }));
  expect(await screen.findByRole("button", { name: "Rebuild on current rules" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "Turn on" })).toBeNull();
  expect(screen.getByRole("button", { name: "Preview changes" })).toBeDisabled();
  expect(fetch.mock.calls.filter(([path]) => path.endsWith("/activate"))).toHaveLength(1);
});

it("keeps a conflicting local edit and asks for review rather than silently overwriting", () => {
  const base = rules(), draft = copy(base), latest = copy(base);
  draft.categories.audience.label = "Local label"; latest.categories.audience.label = "Someone else's label";
  expect(() => rebuildSnapshot(base, draft, latest)).toThrow("Your edit is still here.");
  expect(draft.categories.audience.label).toBe("Local label");
  expect(latest.categories.audience.label).toBe("Someone else's label");
});

it("blocks retry after an uncertain activation until current rules are loaded", async () => {
  const { user, fetch } = mount(rules(), (path, init) => {
    if (path.endsWith("/activate")) throw new Error("Connection closed");
    return serve(path, init);
  });
  await showPreview(user);
  await user.click(screen.getByRole("button", { name: "Turn on" }));
  expect(await screen.findByText("Couldn't confirm whether these rules were turned on. Load the current rules before trying again.")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Turn on" })).toBeNull();
  expect(screen.getByRole("button", { name: "Discard edit and load current rules" })).toBeVisible();
  expect(fetch.mock.calls.filter(([path]) => path.endsWith("/activate"))).toHaveLength(1);
});

it("shows server errors plainly and never enables activation for a failed preview", async () => {
  const { user } = mount(rules(), () => { throw new ApiError("invalid_request", "Invalid category policy request.", 400); });
  await user.type(screen.getByRole("textbox", { name: "Reason for changes" }), "Reviewed");
  await user.click(screen.getByRole("button", { name: "Preview changes" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Invalid category policy request.");
  expect(screen.queryByRole("button", { name: "Turn on" })).toBeNull();
});

it("reports duplicate categories, blank value entry and a missing reason before any request", async () => {
  const { user, fetch } = mount(null);
  await user.type(screen.getByRole("textbox", { name: "New category label" }), "Audience");
  await user.click(screen.getByRole("button", { name: "Add category" }));
  await user.type(screen.getByRole("textbox", { name: "New category label" }), "Audience");
  await user.click(screen.getByRole("button", { name: "Add category" }));
  expect(screen.getByRole("alert")).toHaveTextContent("A category with that ID already exists.");
  await user.click(screen.getByRole("button", { name: "Add value to Audience" }));
  expect(screen.getByRole("alert")).toHaveTextContent("Enter a value before adding it.");
  await user.click(screen.getByRole("button", { name: "Preview changes" }));
  expect(screen.getByRole("alert")).toHaveTextContent("Enter a reason for these changes.");
  expect(fetch).not.toHaveBeenCalled();
  expect(categoryId("  Data handling!  ")).toBe("data-handling");
});

it("rejects duplicate row IDs and missing categories, values, unpinned grants and waiver targets", () => {
  const snapshot = rules();
  snapshot.people.sam[0].deny[0].id = "kids-audience";
  snapshot.people.sam[0].require[0].category = "unknown";
  snapshot.apps.studio[0].require[0].values = ["unknown"];
  snapshot.apps.studio[0].grants[0].service = "unknown";
  snapshot.apps.studio[0].missing = { unknown: "allow" };
  const errors = validateSnapshot(snapshot).join(" ");
  expect(errors).toContain("Rule IDs must be unique.");
  expect(errors).toContain("refers to a missing category");
  expect(errors).toContain("refers to a missing value");
  expect(errors).toContain("Use a pinned service revision for every grant.");
  expect(errors).toContain("A missing-value waiver must name a required rule.");
});

it("preserves inert unaccepted evidence but validates accepted category references", () => {
  const snapshot = rules();
  expect(validateSnapshot(snapshot)).toEqual([]);
  snapshot.services["studio@1"].members[0].assertions[1].accepted = true;
  expect(validateSnapshot(snapshot).join(" ")).toContain("refers to a missing value");
});

it("prevents duplicate preview submissions and closing while a request is pending", async () => {
  let resolve!: (value: unknown) => void;
  const pending = new Promise((done) => { resolve = done; });
  const { user, fetch } = mount(rules(), () => pending);
  await user.type(screen.getByRole("textbox", { name: "Reason for changes" }), "Reviewed");
  const button = screen.getByRole("button", { name: "Preview changes" });
  fireEvent.click(button); fireEvent.click(button);
  expect(fetch).toHaveBeenCalledOnce();
  expect(screen.getByRole("button", { name: "Close editor" })).toBeDisabled();
  resolve(candidate());
  await screen.findByRole("button", { name: "Turn on" });
});

it("closes local edits without creating or activating a candidate", async () => {
  const { user, fetch, onClose } = mount();
  await user.type(screen.getByRole("textbox", { name: "Label for audience" }), " rating");
  await user.click(screen.getByRole("button", { name: "Close editor" }));
  expect(onClose).toHaveBeenCalledOnce();
  expect(fetch).not.toHaveBeenCalled();
});

it("preserves the local edit and blocks preview when the page learns of newer active rules", async () => {
  const { user, props, rerender, fetch } = mount();
  await user.type(screen.getByRole("textbox", { name: "Label for audience" }), " local");
  rerender(<CategoriesEditor {...props} active={{ revision: 4, snapshot: rules() }} />);
  expect(screen.getByRole("textbox", { name: "Label for audience" })).toHaveValue("Audience local");
  expect(screen.getByRole("button", { name: "Preview changes" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Rebuild on current rules" })).toBeVisible();
  expect(fetch).not.toHaveBeenCalled();
});

it("keeps an unchanged snapshot byte-for-byte when preparing a candidate", () => {
  const snapshot = rules(), before = JSON.stringify(snapshot);
  expect(JSON.stringify(prepareSnapshot(snapshot, copy(snapshot), "test"))).toBe(before);
});

it("blocks all saves until a real owner identity is available", async () => {
  const { props, rerender, fetch } = mount();
  rerender(<CategoriesEditor {...props} owner="" />);
  expect(screen.getByRole("alert")).toHaveTextContent("Your owner identity could not be loaded.");
  expect(screen.getByRole("button", { name: "Preview changes" })).toBeDisabled();
  expect(screen.getByRole("textbox", { name: "Label for audience" })).toBeDisabled();
  expect(fetch).not.toHaveBeenCalled();
});

it("validates completeness, category keys, duplicate or empty values and applicability", () => {
  const snapshot = rules();
  snapshot.completeness_gate = false;
  snapshot.categories.audience.id = "other";
  snapshot.categories.audience.values = ["", "general", "general"];
  snapshot.categories.audience.applies_to = [];
  const errors = validateSnapshot(snapshot).join(" ");
  expect(errors).toContain("must keep the information check turned on");
  expect(errors).toContain("category ID does not match");
  expect(errors).toContain("values cannot be empty");
  expect(errors).toContain("value IDs must be unique");
  expect(errors).toContain("needs at least one supported member type");
});

it("validates accepted assertion provenance and subject applicability", () => {
  const snapshot = rules();
  const assertion = snapshot.services["studio@1"].members[0].assertions[0];
  assertion.mapping_version = "";
  snapshot.categories.audience.applies_to = ["route"];
  const errors = validateSnapshot(snapshot).join(" ");
  expect(errors).toContain("needs its source, dates, owner and mapping version");
  expect(errors).toContain("does not apply to release members");
});

it("enforces explicit legacy and category access plan modes", () => {
  const snapshot = rules();
  snapshot.people.sam[0].mode = "legacy";
  snapshot.apps.studio[0].legacy_tags = ["old-tag"];
  const errors = validateSnapshot(snapshot).join(" ");
  expect(errors).toContain("A legacy access plan cannot have category rules");
  expect(errors).toContain("A category access plan cannot have legacy tags");
});

it("validates pinned subjects and optional grant selectors", () => {
  expect(isPinned("model@revision")).toBe(true);
  expect(isPinned("model@branch@revision")).toBe(true);
  expect(isPinned("sha256:a")).toBe(true);
  expect(isPinned("sha256:")).toBe(false);
  expect(isPinned("@revision")).toBe(false);
  expect(isPinned("model@")).toBe(false);
  const snapshot = rules();
  snapshot.services["studio@1"].members[0].id = "unversioned";
  snapshot.people.sam[0].grants[0].release = "unversioned";
  snapshot.apps.studio[0].grants[0].workflow = "unversioned";
  const errors = validateSnapshot(snapshot).join(" ");
  expect(errors).toContain("Every service member needs a pinned revision");
  expect(errors).toContain("Use a pinned model release");
  expect(errors).toContain("Use a pinned workflow revision");
});

it("checks rule operations and service keys while allowing an empty set of in candidates", () => {
  const snapshot = rules();
  snapshot.people.sam[0].require[0].values = [];
  expect(validateSnapshot(snapshot)).toEqual([]);
  snapshot.apps.studio[0].deny[0].op = "exists";
  snapshot.services["studio@1"].id = "wrong@1";
  const errors = validateSnapshot(snapshot).join(" ");
  expect(errors).toContain("Choose how to check rule no-training");
  expect(errors).toContain("checks whether a value is present");
  expect(errors).toContain("A service ID does not match");
});

it("does not reuse an occupied pinned service revision", () => {
  const base = rules();
  base.retained["other-output"] = { ...copy(base.services["studio@1"]), id: "studio@r2-test" };
  const draft = copy(base); draft.services["studio@1"].members[0].assertions[0].value = "adult";
  expect(prepareSnapshot(draft, base, "test").services["studio@r3-test"].id).toBe("studio@r3-test");
});

it("requires a separate owner choice before converting legacy tags to category rules", async () => {
  const snapshot = rules();
  snapshot.people.sam = [{ id: "legacy-plan", grants: [], require: [], deny: [], missing: {}, mode: "legacy", legacy_tags: ["old-tag"] }];
  const { user, fetch } = mount(snapshot);
  const section = within(screen.getByRole("region", { name: "Person access plans" }));
  expect(section.getByRole("button", { name: "Add requirement" })).toBeDisabled();
  await user.click(section.getByRole("button", { name: "Use category rules for this access plan" }));
  expect(section.getByRole("button", { name: "Add requirement" })).toBeEnabled();
  expect(fetch).not.toHaveBeenCalled();
  await showPreview(user);
  const posted = (fetch.mock.calls[0][1]!.body as { snapshot: Snapshot }).snapshot;
  expect(posted.people.sam[0]).toEqual({ id: "legacy-plan", grants: [], require: [], deny: [], missing: {}, mode: "v3", legacy_tags: [] });
});

it("pauses edits while owner identity is being refreshed, even with a cached subject", () => {
  const { props, rerender, fetch } = mount();
  rerender(<CategoriesEditor {...props} ownerLoading />);
  expect(screen.getByText("Loading owner identity")).toBeVisible();
  expect(screen.getByRole("button", { name: "Preview changes" })).toBeDisabled();
  expect(screen.getByRole("textbox", { name: "Label for audience" })).toBeDisabled();
  expect(fetch).not.toHaveBeenCalled();
});

it("bumps a previously tokenized revision and separates independent editing sessions", () => {
  const base = rules(), service = copy(base.services["studio@1"]);
  service.id = "studio@r7-previous";
  base.services = { [service.id]: service };
  for (const plan of [...base.people.sam, ...base.apps.studio]) plan.grants[0].service = service.id;
  const draft = copy(base); draft.services[service.id].members[0].assertions[0].value = "adult";
  const first = prepareSnapshot(draft, base, "first");
  const second = prepareSnapshot(draft, base, "second");
  expect(first.services["studio@r8-first"].id).toBe("studio@r8-first");
  expect(second.services["studio@r8-second"].id).toBe("studio@r8-second");
  expect(first.people.sam[0].grants[0].service).toBe("studio@r8-first");
  expect(first.retained).toEqual(base.retained);
});

it("pauses saving when the page cannot confirm the active rules", async () => {
  const { user, props, rerender, fetch } = mount();
  await showPreview(user);
  rerender(<CategoriesEditor {...props} paused />);
  expect(screen.getByRole("button", { name: "Turn on" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Close preview" }));
  expect(screen.getByRole("button", { name: "Preview changes" })).toBeDisabled();
  expect(fetch.mock.calls.filter(([path]) => path.endsWith("/activate"))).toHaveLength(0);
});
