import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { adminFetch, ApiError, api, type AdminFetchInit } from "@/lib/api";
import { ViewerContext } from "@/components/shell/session";
import CategoriesPage from "./page";

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
  if (path.endsWith("/services")) return { services: [] };
  if (path.endsWith("/restore")) return candidate;
  if (path.endsWith("/preview-as")) return allow;
  if (path.endsWith("/activate")) return { revision: 3, active_counter: 3 };
  throw new Error("Unexpected path: " + path);
}
function mount(respond: (path: string, init?: AdminFetchInit) => unknown = serve, role: "owner" | "admin" | "member" = "owner") {
  vi.spyOn(api.users, "list").mockResolvedValue([{ id: "sam", issuer: "", subject: "", display_name: "Sam", name: null, email: null, created_at: 0, last_login_at: null, roles: ["owner"] }]);
  vi.spyOn(api.apps, "list").mockResolvedValue([{ id: "studio", name: "Studio", kind: "personal", owner_user_id: "sam", owner_service_account_id: null, default_profile_id: null, created_at: 0, disabled_at: null, effective_profile_id: "", effective_profile_name: "", effective_profile_source: "fallback" }]);
  const fetchCategory = vi.mocked(adminFetch);
  fetchCategory.mockImplementation(async <T,>(path: string, init?: AdminFetchInit): Promise<T> => await respond(path, init) as T);
  render(<ViewerContext.Provider value={{ authenticated: true, name: "Sam", email: null, role, admin: role !== "member" }}>
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}>
      <CategoriesPage />
    </QueryClientProvider>
  </ViewerContext.Provider>);
  return fetchCategory;
}
async function pickPreview() {
  const user = userEvent.setup();
  await screen.findByRole("heading", { name: "Preview as" });
  for (const [label, option] of [["Person", "Sam"], ["App", "Studio"], ["Capability", "Images"], ["Service", "studio@1"]]) {
    await user.click(screen.getByRole("combobox", { name: label }));
    await user.click(await screen.findByRole("option", { name: option }));
  }
  await user.click(screen.getByRole("button", { name: "Check access" }));
  return user;
}

it.each(["member", "admin"] as const)("keeps %s out without fetching owner data", (role) => {
  const fetchCategory = mount(serve, role);
  expect(screen.getByText("Only the owner can manage categories")).toBeVisible();
  expect(fetchCategory).not.toHaveBeenCalled();
  expect(api.users.list).not.toHaveBeenCalled();
  expect(api.apps.list).not.toHaveBeenCalled();
});

it("renders active rules, grants, named waivers and accepted member evidence in plain words", async () => {
  mount();
  await screen.findByText("Revision 2");
  expect(screen.getAllByText(/Needs Audience: General/)).toHaveLength(2);
  expect(screen.getAllByText(/Blocks when Audience: Adult/)).toHaveLength(2);
  expect(screen.getAllByText("Allows Images with studio@1")).toHaveLength(2);
  expect(screen.getAllByText(/Missing-value waiver for kids-audience/)).toHaveLength(2);
  expect(screen.getByText(/Source: Owner review/)).toBeVisible();
  expect(screen.queryByText(/Unaccepted import/)).toBeNull();
  expect(screen.getByText("Release: helper-model@1")).toBeVisible();
  expect(screen.getByText("No accepted values.")).toBeVisible();
  expect(screen.getByText("Turned on: " + new Date(active.activated_at).toLocaleString())).toBeVisible();
  expect(screen.getByText(/Nothing is enforced by this page yet/)).toHaveTextContent("existing access plans still apply");
});

it("does not use creation time as activation time on older gateways", async () => {
  mount((path) => path.endsWith("/active") ? { ...active, activated_at: undefined } : serve(path));
  expect(await screen.findByText("Activation time not recorded")).toBeVisible();
  expect(screen.queryByText("Turned on: " + new Date(active.created_at).toLocaleString())).toBeNull();
});

it("previews allow using the selected person, app, capability, service and revision", async () => {
  const fetchCategory = mount();
  await pickPreview();
  expect(await screen.findByText("Allow · Revision 2")).toBeVisible();
  expect(screen.getByText("Every attached access plan allows this request.")).toBeVisible();
  expect(fetchCategory).toHaveBeenCalledWith("/admin/api/categories/preview-as", {
    method: "POST", body: { person: "sam", app: "studio", capability: "image.generate", service: "studio@1", revision: 2 },
  });
});

it("previews a deny with its plain reason, failing rule and member", async () => {
  mount((path) => path.endsWith("/preview-as") ? deny : serve(path));
  await pickPreview();
  const result = (await screen.findByText("Deny · Revision 2")).closest('[role="status"]') as HTMLElement;
  expect(within(result).getByText("An accepted value does not meet a required rule.")).toBeVisible();
  expect(within(result).getByText("kids-audience")).toBeVisible();
  expect(within(result).getByText("model@1")).toBeVisible();
});

it.each([
  ["NO_PLANS", "The person or app has no access plan."],
  ["NO_GRANT", "An access plan does not grant this capability with this service."],
  ["EXPLICIT_DENY", "A blocking rule matches this member."],
  ["DENY_INPUT_UNKNOWN", "A blocking rule cannot be checked because an accepted value is missing."],
  ["REQUIRE_MISSING", "An accepted value needed by a required rule is missing."],
  ["NO_APPLICABLE_SUBJECT", "There is no applicable member to check for this rule."],
  ["METADATA_INCOMPLETE", "The gateway is missing information needed to check this request."],
  ["LEGACY_TAG_MISMATCH", "The service tags do not match the legacy access plan."],
  ["IDENTITY_UNAVAILABLE", "The gateway could not identify the person or app."],
  ["POLICY_UNAVAILABLE", "The gateway could not check the category rules."],
  ["NEW_REASON", "The gateway did not provide a recognized reason for this result."],
])("explains %s without exposing a raw reason code", async (reason, sentence) => {
  mount((path) => path.endsWith("/preview-as") ? { ...deny, reason } : serve(path));
  await pickPreview();
  expect(await screen.findByText(sentence)).toBeVisible();
  expect(screen.queryByText(reason)).toBeNull();
});

it("clears the previous preview when a selection changes", async () => {
  mount((path) => path.endsWith("/active") ? { ...active, snapshot: { ...snapshot, people: { ...snapshot.people, guest: [plan] } } } : serve(path));
  const user = await pickPreview();
  await screen.findByText("Allow · Revision 2");
  await user.click(screen.getByRole("combobox", { name: "Person" }));
  await user.click(await screen.findByRole("option", { name: "guest" }));
  expect(screen.queryByText("Allow · Revision 2")).toBeNull();
});

it("restores to a change preview and activates only after Turn on with its expected base", async () => {
  let activated = false;
  const fetchCategory = mount((path) => {
    if (path.endsWith("/activate")) { activated = true; return { revision: 3, active_counter: 3 }; }
    if (path.endsWith("/active") && activated) return { ...active, revision: 3 };
    return serve(path);
  });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Restore revision 1" }));
  await screen.findByRole("heading", { name: "Change preview" });
  expect(screen.getByText("Who gains access")).toBeVisible();
  expect(screen.getByText("Who loses access")).toBeVisible();
  expect(screen.getByText("Reasons that change")).toBeVisible();
  expect(screen.getByText(/This compares 2 known requests/)).toBeVisible();
  expect(screen.getAllByText(/Sam · Studio · Images · other@1/, { selector: "li" })[0]).toBeVisible();
  expect(fetchCategory.mock.calls.some(([path]) => path.endsWith("/activate"))).toBe(false);
  expect(fetchCategory).toHaveBeenCalledWith("/admin/api/categories/revisions/1/restore", { method: "POST", body: { base_revision: 2, reason: "Restore revision 1" } });
  await waitFor(() => expect(screen.getByRole("button", { name: "Turn on" })).toBeEnabled());
  await user.click(screen.getByRole("button", { name: "Turn on" }));
  await screen.findByText("Revision 3 is now selected for category previews.");
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
  await user.click(await screen.findByRole("button", { name: "Restore revision 1" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Turn on" })).toBeEnabled());
  await user.click(screen.getByRole("button", { name: "Turn on" }));
  expect(await screen.findByText("The active rules changed. Restore again to review a new preview.")).toBeVisible();
  expect(await screen.findByText("Revision 4")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Turn on" })).toBeNull();
  expect(fetchCategory.mock.calls.filter(([path]) => path.endsWith("/activate"))).toHaveLength(1);
});

it("handles a stale restore before exposing any Turn on action", async () => {
  const fetchCategory = mount((path) => {
    if (path.endsWith("/restore")) throw new ApiError("stale_base", "Changed", 409);
    return serve(path);
  });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Restore revision 1" }));
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
  expect(await screen.findByText("No category rules are active")).toBeVisible();
  expect(screen.getByText(/Nothing is enforced by this page yet/)).toHaveTextContent("existing access plans still apply");
  expect(screen.queryByRole("heading", { name: "Preview as" })).toBeNull();
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
  expect(screen.queryByText("No category rules are active")).toBeNull();
  fail = false;
  await userEvent.setup().click(within(alert).getByRole("button", { name: "Try again" }));
  expect(await screen.findByText("Revision 2")).toBeVisible();
});

it("keeps retained output bundles distinct from a service with the same revision", async () => {
  mount((path) => path.endsWith("/active") ? {
    ...active, snapshot: { ...snapshot, retained: { "saved-output": snapshot.services["studio@1"] } },
  } : serve(path));
  expect(await screen.findByText("Service: studio@1 · Retained output: saved-output")).toBeVisible();
  expect(screen.getByText("Service: studio@1")).toBeVisible();
});

it("loads the service catalog only for editing and retries failures without losing draft edits", async () => {
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
  await screen.findByText("Revision 2");
  expect(fetchCategory.mock.calls.some(([path]) => path.endsWith("/services"))).toBe(false);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Edit rules" }));
  await screen.findByText(/Available services could not be loaded/);
  const editor = within(screen.getByRole("region", { name: "Edit category rules" }));
  await waitFor(() => expect(editor.getByRole("textbox", { name: "Reason for changes" })).toBeEnabled());
  await user.type(editor.getByRole("textbox", { name: "Reason for changes" }), "Keep this draft");
  await user.click(editor.getByRole("button", { name: "Retry available services" }));
  await editor.findByRole("combobox", { name: "Service to add" });
  expect(attempts).toBe(2);
  expect(editor.getByRole("textbox", { name: "Reason for changes" })).toHaveValue("Keep this draft");
});
