import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { adminFetch, ApiError, api, type AdminFetchInit } from "@/lib/api";
import { ViewerContext } from "@/components/shell/session";
import { AdvancedRules } from "./advanced";

vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return { ...original, adminFetch: vi.fn() };
});
for (const method of ["hasPointerCapture", "setPointerCapture", "releasePointerCapture", "scrollIntoView"]) {
  Object.defineProperty(HTMLElement.prototype, method, { configurable: true, value: () => false });
}
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.mocked(adminFetch).mockReset();
});

const plan = {
  id: "family", mode: "v3", legacy_tags: [],
  grants: [{ capability: "image.generate", service: "studio@1" }],
  require: [{ id: "kids-audience", category: "audience", op: "in", values: ["general"] }],
  deny: [{ id: "no-adult", category: "audience", op: "in", values: ["adult"] }],
  missing: { "kids-audience": "allow" },
};
const snapshot = {
  id: "rules-2",
  categories: { audience: { id: "audience", label: "Audience", values: ["general", "adult"], applies_to: ["release"] } },
  people: { sam: [plan] },
  apps: { studio: [{ ...plan, id: "app-plan" }] },
  services: {
    "studio@1": {
      id: "studio@1", checkable: true,
      members: [{ id: "model@1", kind: "release", assertions: [
        { category: "audience", value: "general", accepted: true, source: "Owner review", accepted_at: "2026-10-10", accepting_owner: "sam", observed_at: "2026-10-09" },
        { category: "audience", value: "adult", accepted: false, source: "Unaccepted import", accepted_at: "", accepting_owner: "", observed_at: "" },
      ] }],
      dependencies: [{ id: "helper@1", checkable: true, dependencies: [], members: [{ id: "helper-model@1", kind: "release", assertions: [] }] }],
    },
  },
  retained: {},
};
const active = {
  revision: 2, parent_revision: 1, active_counter: 2, snapshot,
  created_at: "2026-10-09T12:00:00Z", activated_at: "2026-10-10T13:00:00Z",
  author: "sam", reason: "Reviewed audience",
};
const history = { history: [
  { sequence: 1, action: "activate", revision: 1, previous_revision: null, source_revision: null, active_counter: 1, created_at: "2026-10-08T12:00:00Z", author: "sam", reason: "First rules" },
  { sequence: 2, action: "activate", revision: 2, previous_revision: 1, source_revision: null, active_counter: 2, created_at: active.activated_at, author: "sam", reason: active.reason },
] };
const allow = { outcome: "allow", reason: "ALLOW", rule: "", category: "", member: "", waivers: [] };
const deny = { outcome: "deny", reason: "REQUIRE_MISMATCH", rule: "kids-audience", category: "audience", member: "model@1", waivers: [] };
const request = { person: "sam", app: "studio", capability: "image.generate", service: "studio@1", phase: "request", metadata_complete: true };
const candidate = {
  ...active, revision: 3, parent_revision: 2, restored_from_revision: 1, preview_base_revision: 2,
  preview: { gains: [request], losses: [{ ...request, service: "other@1" }], reason_changes: [{ request, before: deny, after: allow }], scope: [request, { ...request, service: "other@1" }] },
};
function serve(path: string) {
  if (path.endsWith("/active")) return active;
  if (path.endsWith("/history")) return history;
  if (path.endsWith("/services")) return { services: [{ stem: "studio", label: "Studio model", kind: "model", capabilities: ["image.generate"] }, { stem: "model", label: "Image model", kind: "model", capabilities: ["image.generate"] }, { stem: "helper-model", label: "Helper model", kind: "model", capabilities: [] }] };
  if (path.endsWith("/restore")) return candidate;
  if (path.endsWith("/preview-as")) return allow;
  if (path.endsWith("/activate")) return { revision: 3, active_counter: 3 };
  throw new Error("Unexpected path: " + path);
}
function mount(respond: (path: string, init?: AdminFetchInit) => unknown = serve, role: "owner" | "admin" | "member" = "owner") {
  vi.spyOn(api.users, "list").mockResolvedValue([{ id: "sam", issuer: "", subject: "", display_name: "Sam", name: null, email: null, created_at: 0, last_login_at: null, roles: ["owner"] }]);
  vi.spyOn(api.apps, "list").mockResolvedValue([{ id: "studio", name: "Studio", kind: "personal", owner_user_id: "sam", accountable_person: { id: "sam", name: "Sam" }, access_plan: "person", same_as_person: true, access_plan_name: null, effective_access_plan_id: null, created_at: 0, disabled_at: null }]);
  const fetchCategory = vi.mocked(adminFetch);
  fetchCategory.mockImplementation(async <T,>(path: string, init?: AdminFetchInit): Promise<T> => await respond(path, init) as T);
  render(<ViewerContext.Provider value={{ authenticated: true, name: "Sam", email: null, role, admin: role !== "member" }}>
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}>
      <AdvancedRules />
    </QueryClientProvider>
  </ViewerContext.Provider>);
  return fetchCategory;
}
async function openRules() {
  await screen.findByRole("heading", { name: "Check access" });
  const details = screen.getByText("Current rules").closest("details")!;
  if (!details.open) await userEvent.setup().click(details.querySelector("summary")!);
}
async function pickPreview() {
  const user = userEvent.setup();
  await screen.findByRole("heading", { name: "Check access" });
  for (const [label, option] of [["Person", "Sam"], ["App", "Studio"], ["Capability", "Images"], ["Model or workflow", "Studio model"]]) {
    await user.click(screen.getByRole("combobox", { name: label }));
    await user.click(await screen.findByRole("option", { name: option }));
  }
  await user.click(screen.getByRole("button", { name: "Check access" }));
  return user;
}

it("renders active rules, grants, named waivers and accepted member evidence in plain words", async () => {
  mount();
  await openRules();
  expect(screen.getAllByText(/Needs Audience: General/)).toHaveLength(2);
  expect(screen.getAllByText(/Blocks when Audience: Adult/)).toHaveLength(2);
  expect(screen.getAllByText("Allows Images with Studio model")).toHaveLength(2);
  expect(screen.getAllByText(/Allow when information is missing for Audience/)).toHaveLength(2);
  expect(screen.getByText(/Source: Owner review/)).toBeVisible();
  expect(screen.queryByText(/Unaccepted import/)).toBeNull();
  expect(screen.getByText("Model release: Helper model")).toBeVisible();
  expect(screen.getByText("No accepted values.")).toBeVisible();
  expect(screen.getByText("Turned on: " + new Date(active.activated_at).toLocaleString())).toBeVisible();
  expect(screen.getByText(/Active rules apply to the people and apps listed in them/)).toBeVisible();
  expect(screen.queryByText(/Nothing is enforced|enforcement comes later/)).toBeNull();
});

it("does not use creation time as activation time on older gateways", async () => {
  mount((path) => path.endsWith("/active") ? { ...active, activated_at: undefined } : serve(path));
  await openRules(); expect(screen.getByText("Activation time not recorded")).toBeVisible();
  expect(screen.queryByText("Turned on: " + new Date(active.created_at).toLocaleString())).toBeNull();
});

it("previews allow using the selected person, app, capability, service and revision", async () => {
  const fetchCategory = mount();
  await pickPreview();
  expect(await screen.findByText("Allow")).toBeVisible();
  expect(screen.getByText("Every attached access plan allows this request.")).toBeVisible();
  expect(fetchCategory).toHaveBeenCalledWith("/admin/api/categories/preview-as", {
    method: "POST", body: { person: "sam", app: "studio", capability: "image.generate", service: "studio@1", revision: 2 },
  });
});

it("previews a deny with its plain reason, failing rule and member", async () => {
  mount((path) => path.endsWith("/preview-as") ? deny : serve(path));
  await pickPreview();
  const result = (await screen.findByText("Deny")).closest('[role="status"]') as HTMLElement;
  expect(within(result).getByText("An accepted value does not meet a required rule.")).toBeVisible();
  expect(within(result).getByText("kids-audience")).not.toBeVisible();
  await userEvent.setup().click(within(result).getAllByText("Details")[0]);
  expect(within(result).getByText("kids-audience")).toBeVisible();
  expect(within(result).getByText("model@1")).toBeVisible();
});

it.each([
  ["NO_PLANS", "The person or app has no access plan."],
  ["NO_GRANT", "An access plan does not grant this capability with this model or workflow."],
  ["EXPLICIT_DENY", "A blocking rule matches this member."],
  ["DENY_INPUT_UNKNOWN", "A blocking rule cannot be checked because an accepted value is missing."],
  ["REQUIRE_MISSING", "An accepted value needed by a required rule is missing."],
  ["NO_APPLICABLE_SUBJECT", "There is no applicable member to check for this rule."],
  ["METADATA_INCOMPLETE", "The gateway is missing information needed to check this request."],
  ["LEGACY_TAG_MISMATCH", "The service tags do not match the legacy access plan."],
  ["IDENTITY_UNAVAILABLE", "The gateway could not identify the person or app."],
  ["POLICY_UNAVAILABLE", "The gateway could not check the category rules."],
  ["NEW_REASON", "We couldn't tell why the access check failed. Try again."],
])("explains %s without exposing a raw reason code", async (reason, sentence) => {
  mount((path) => path.endsWith("/preview-as") ? { ...deny, reason } : serve(path));
  await pickPreview();
  expect(await screen.findByText(sentence)).toBeVisible();
  expect(screen.queryByText(reason)).toBeNull();
});

it("clears the previous preview when a selection changes", async () => {
  mount((path) => path.endsWith("/active") ? { ...active, snapshot: { ...snapshot, people: { ...snapshot.people, guest: [plan] } } } : serve(path));
  const user = await pickPreview();
  await screen.findByText("Allow");
  await user.click(screen.getByRole("combobox", { name: "Person" }));
  await user.click(await screen.findByRole("option", { name: "Person name unavailable" }));
  expect(screen.queryByText("Allow")).toBeNull();
});

it("restores to a change preview and activates only after Turn on with its expected base", async () => {
  let activated = false;
  const fetchCategory = mount((path) => {
    if (path.endsWith("/activate")) { activated = true; return { revision: 3, active_counter: 3 }; }
    if (path.endsWith("/active") && activated) return { ...active, revision: 3 };
    return serve(path);
  });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Restore rules from " + new Date(history.history[0].created_at).toLocaleString() }));
  await screen.findByRole("heading", { name: "Change preview" });
  expect(screen.getByText("Who gains access")).toBeVisible();
  expect(screen.getByText("Who loses access")).toBeVisible();
  expect(screen.getByText("Reasons that change")).toBeVisible();
  expect(screen.getByText(/This compares 2 known requests/)).toBeVisible();
  expect(screen.getAllByText(/Sam · Studio · Images · Name unavailable/, { selector: "li" })[0]).toBeVisible();
  expect(fetchCategory.mock.calls.some(([path]) => path.endsWith("/activate"))).toBe(false);
  expect(fetchCategory).toHaveBeenCalledWith("/admin/api/categories/revisions/1/restore", { method: "POST", body: { base_revision: 2, reason: "Restore revision 1" } });
  await waitFor(() => expect(screen.getByRole("button", { name: "Turn on" })).toBeEnabled());
  await user.click(screen.getByRole("button", { name: "Turn on" }));
  await screen.findByText("These category rules are now turned on.");
  expect(fetchCategory).toHaveBeenCalledWith("/admin/api/categories/candidates/3/activate", { method: "POST", body: { expected_active_revision: 2 } });
  expect(screen.queryByRole("heading", { name: "Change preview" })).toBeNull();
});

it("shows stale activation, refreshes rules and discards the old preview without retrying", async () => {
  let stale = false;
  const fetchCategory = mount((path) => {
    if (path.endsWith("/activate")) { stale = true; throw new ApiError("stale_base", "Changed", 409); }
    if (path.endsWith("/active") && stale) return { ...active, revision: 4 };
    return serve(path);
  });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Restore rules from " + new Date(history.history[0].created_at).toLocaleString() }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Turn on" })).toBeEnabled());
  await user.click(screen.getByRole("button", { name: "Turn on" }));
  expect(await screen.findByText("The active rules changed. Restore again to review a new preview.")).toBeVisible();
  await openRules(); expect(screen.getByText("Revision 4")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Turn on" })).toBeNull();
  expect(fetchCategory.mock.calls.filter(([path]) => path.endsWith("/activate"))).toHaveLength(1);
});

it("handles a stale restore before exposing any Turn on action", async () => {
  const fetchCategory = mount((path) => {
    if (path.endsWith("/restore")) throw new ApiError("stale_base", "Changed", 409);
    return serve(path);
  });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Restore rules from " + new Date(history.history[0].created_at).toLocaleString() }));
  await screen.findByText("The active rules changed. Restore again to review a new preview.");
  expect(screen.queryByRole("button", { name: "Turn on" })).toBeNull();
  expect(fetchCategory.mock.calls.filter(([path]) => path.endsWith("/active")).length).toBeGreaterThan(1);
});

it("shows the no-active empty state while retaining the existing-plan warning", async () => {
  mount((path) => {
    if (path.endsWith("/active")) throw new ApiError("no_active_snapshot", "No rules", 409);
    if (path.endsWith("/history")) return { history: [] };
    return serve(path);
  });
  expect(await screen.findByText("No access rules yet")).toBeVisible();
  expect(screen.getByText(/Active rules apply to the people and apps listed in them/)).toBeVisible();
  expect(screen.queryByText(/Nothing is enforced|enforcement comes later/)).toBeNull();
  expect(screen.queryByRole("heading", { name: "Check access" })).toBeNull();
});

it("distinguishes loading and unavailable rules from an empty snapshot, and retries", async () => {
  let fail = true;
  mount((path) => {
    if (path.endsWith("/active") && fail) throw new ApiError("policy_unavailable", "Unavailable", 503);
    return serve(path);
  });
  expect(screen.getByLabelText("Loading category rules")).toHaveAttribute("aria-busy", "true");
  const alert = await screen.findByRole("alert");
  expect(within(alert).getByText("Couldn't load category rules")).toBeVisible();
  expect(screen.queryByText("No access rules yet")).toBeNull();
  fail = false;
  await userEvent.setup().click(within(alert).getByRole("button", { name: "Try again" }));
  await openRules(); expect(within(screen.getByRole("region", { name: "Current rules" })).getByText("Revision 2")).toBeVisible();
});

it("keeps retained output bundles distinct from a service with the same revision", async () => {
  mount((path) => path.endsWith("/active") ? {
    ...active, snapshot: { ...snapshot, retained: { "saved-output": snapshot.services["studio@1"] } },
  } : serve(path));
  await openRules(); expect(screen.getByText("Model or workflow: Studio model · Retained output: saved-output")).toBeVisible();
  expect(screen.getByText("Model or workflow: Studio model")).toBeVisible();
});

it("loads display names before editing and retries failures without losing draft edits", async () => {
  let attempts = 0;
  const fetchCategory = mount((path) => {
    if (path.endsWith("/services")) {
      attempts++;
      if (attempts === 1) throw new Error("catalog unavailable");
      return { services: [{ stem: "workflow:paint", label: "Paint workflow", kind: "workflow", capabilities: ["image.edit"] }] };
    }
    return serve(path);
  });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ actor: { subject: "sam" } }) }));
  await openRules();
  expect(fetchCategory.mock.calls.some(([path]) => path.endsWith("/services"))).toBe(true);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Edit rules" }));
  await screen.findByText(/Models and workflows couldn't be loaded/);
  const editor = within(screen.getByRole("region", { name: "Edit category rules" }));
  await waitFor(() => expect(editor.getByRole("textbox", { name: "Reason for changes" })).toBeEnabled());
  await user.type(editor.getByRole("textbox", { name: "Reason for changes" }), "Keep this draft");
  await user.click(editor.getByRole("button", { name: "Try again" }));
  await editor.findByRole("combobox", { name: "Model or workflow to add" });
  expect(attempts).toBe(2);
  expect(editor.getByRole("textbox", { name: "Reason for changes" })).toHaveValue("Keep this draft");
});

it("states enforcement and coverage before collapsed rule details", async () => {
  mount();
  expect(await screen.findByText("Current rules govern 1 person and 1 app.")).toBeVisible();
  expect(screen.getByText(/Active rules apply to the people and apps listed in them/)).toBeVisible();
  expect(screen.queryByText(/Nothing is enforced|enforcement comes later|selected for category previews/)).toBeNull();
  const details = screen.getByText("Current rules").closest("details")!;
  expect(details).not.toHaveAttribute("open");
  expect(screen.getByRole("heading", { name: "Check access" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Edit rules" })).toBeVisible();
});

it("offers first access rules without suggesting unavailable history", async () => {
  mount((path) => path.endsWith("/active") ? null : path.endsWith("/history") ? { history: [] } : serve(path));
  expect(await screen.findByText("No access rules yet")).toBeVisible();
  expect(screen.getByRole("button", { name: "Edit rules" })).toBeEnabled();
  expect(screen.queryByText(/restore saved rules from History/)).toBeNull();
});
