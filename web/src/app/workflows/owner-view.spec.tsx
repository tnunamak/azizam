import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { ViewerContext, type Viewer } from "@/components/shell/session";
import { ownerApi, OwnerApiError } from "./owner-api";
import { OwnerView } from "./owner-view";
import WorkflowsPage from "./page";

// Wire fixtures in the shapes gateway/workflow_owner_api.py sends.
const etag = '"os-7"';
const id = "example.workflow";
const contract = { id, schema_version: 2, inputs: {}, outputs: {}, bindings: {} };
const choices = {
  exposure: ["server-exposure-a", "server-exposure-b"],
  trust_tier: ["server-trust-a", "server-trust-b"],
  resource_tier: ["server-tier-a", "server-tier-b"],
  resource_class: ["server-class-a", "server-class-b"],
};
const facts = {
  exposure: choices.exposure[0], trust_tier: choices.trust_tier[0],
  resource_tier: choices.resource_tier[0], resource_class: choices.resource_class[0],
  tags: ["keep-this-tag"],
};
const entry = {
  id, state: "active", published: false, record_revision: 7,
  contract_digest: "sha256:contract", record_digest: "sha256:record",
  provenance: { trusted: true, source: "owner" }, entry_problems: [{ code: "needs_configuration" }], needs_configuration: true,
};
const history = [{ record_revision: 6, record_digest: "sha256:old-record", contract_digest: "sha256:old-contract",
  saved_at_s: 100, actor: { kind: "owner", id: "owner", label: "Owner" }, change: "update" }];
const workflow = { ...entry, contract, facts, overrides: null, incarnation: "inc-1", history };
const detail = { workflow };
const list = { workflows: [entry], revision: 7, choices };
const commit = { revision: 8, workflow_id: id, record_revision: 8,
  contract_digest: "sha256:contract", record_digest: "sha256:new-record" };
const problems = [{ path: ["bindings", "example"], pointer: "/bindings/example", code: "binding_unknown", message: "Choose a binding." }];
const refusal = (code: string, extra: object = {}) => ({ error: { code, message: "Server sentence." }, ...extra });
const clients: QueryClient[] = [];

type Request = { url: string; init: RequestInit; method: string };
type Respond = (request: Request) => Response | Promise<Response>;
let requests: Request[] = [];
function json(body: unknown, status = 200, tag: string | null = etag) {
  return Response.json(body, { status, headers: tag === null ? {} : { ETag: tag } });
}
function serve(respond?: Respond) {
  const mock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const request = { url: String(input), init, method: init.method ?? "GET" };
    requests.push(request);
    if (respond) return respond(request);
    if (request.url === "/admin/api/workflows" && request.method === "GET") return json(list);
    if (request.url === `/admin/api/workflows/${id}` && request.method === "GET") return json(detail);
    if (request.url === "/admin/api/workflows/check") return json({ report: { ok: true, errors: [] } });
    if (request.url.startsWith("/admin/api/workflows") && request.method !== "GET") return json({ commit });
    if (request.url === "/me/api/workflows") return json({ schema_version: 1, workflows: [] });
    throw new Error(`Unexpected request: ${request.method} ${request.url}`);
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}
function mount(owner = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  clients.push(client);
  return render(<QueryClientProvider client={client}><OwnerView owner={owner} /></QueryClientProvider>);
}
async function open() {
  mount();
  fireEvent.click(await screen.findByRole("button", { name: `Manage ${id}` }));
  return screen.findByRole("textbox", { name: "Workflow JSON" });
}
const body = (request: Request) => request.init.body == null ? undefined : JSON.parse(String(request.init.body));
const writes = () => requests.filter((request) => request.method !== "GET" && !request.url.endsWith("/check"));
beforeEach(() => { requests = []; serve(); });
afterEach(() => { cleanup(); for (const client of clients.splice(0)) client.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("owner workflow I6 client", () => {
  test("list preserves its ETag and exposes only the returned choices", async () => {
    expect(await ownerApi.list()).toEqual({ data: list, etag });
    expect(requests[0].url).toBe("/admin/api/workflows");
    expect(requests[0].init.credentials).toBe("same-origin");
    expect(new Headers(requests[0].init.headers).has("If-Match")).toBe(false);
  });
  test("get encodes a complete workflow id and preserves its ETag", async () => {
    serve(() => json(detail));
    expect(await ownerApi.get("workflow/space name")).toEqual({ data: workflow, etag });
    expect(requests[0].url).toBe("/admin/api/workflows/workflow%2Fspace%20name");
    expect(requests[0].init.credentials).toBe("same-origin");
  });
  test("check sends the document and explicit facts without a write precondition", async () => {
    const draft = { contract, facts, overrides: "bundled.id" };
    expect(await ownerApi.check(draft)).toEqual({ ok: true, errors: [] });
    expect(requests[0].url).toBe("/admin/api/workflows/check");
    expect(requests[0].method).toBe("POST");
    expect(body(requests[0])).toEqual(draft);
    expect(new Headers(requests[0].init.headers).has("If-Match")).toBe(false);
  });
  const routes = [
    { name: "create", method: "POST", path: "", payload: { contract }, invoke: (tag: string) => ownerApi.create({ contract }, tag) },
    { name: "replace", method: "PUT", path: `/${id}`, payload: { contract, facts: null }, invoke: (tag: string) => ownerApi.replace(id, { contract, facts: null }, tag) },
    { name: "state", method: "POST", path: `/${id}/state`, payload: { state: "disabled" }, invoke: (tag: string) => ownerApi.setState(id, "disabled", tag) },
    { name: "publish", method: "POST", path: `/${id}/publish`, payload: { published: true }, invoke: (tag: string) => ownerApi.setPublished(id, true, tag) },
    { name: "restore", method: "POST", path: `/${id}/restore`, payload: { record_revision: 6 }, invoke: (tag: string) => ownerApi.restore(id, 6, tag) },
    { name: "delete", method: "DELETE", path: `/${id}`, payload: undefined, invoke: (tag: string) => ownerApi.delete(id, tag) },
  ];
  test.each(routes)("$name uses its I6 route, body and explicit ETag", async ({ method, path, payload, invoke }) => {
    serve(() => json({ commit }));
    expect(await invoke(etag)).toEqual(commit);
    expect(requests).toHaveLength(1);
    expect(requests[0].url).toBe(`/admin/api/workflows${path}`);
    expect(requests[0].method).toBe(method);
    expect(requests[0].init.credentials).toBe("same-origin");
    expect(new Headers(requests[0].init.headers).get("If-Match")).toBe(etag);
    if (payload !== undefined) expect(new Headers(requests[0].init.headers).get("Content-Type")).toBe("application/json");
    expect(body(requests[0])).toEqual(payload);
  });
  test.each(routes)("$name refuses to write without a read ETag", async ({ invoke }) => {
    await expect(invoke("")).rejects.toBeInstanceOf(OwnerApiError);
    expect(requests).toHaveLength(0);
  });
  test.each([
    { status: 412, code: "generation_mismatch" },
    { status: 409, code: "workflow_exists" },
    { status: 409, code: "id_is_bundled" },
    { status: 422, code: "workflow_invalid" },
  ])("maps $status $code to a typed failure", async ({ status, code }) => {
    serve(() => json({ error: { code, message: "Server sentence.", current_revision: 9 }, problems }, status));
    try { await ownerApi.replace(id, { contract }, etag); throw new Error("expected refusal"); }
    catch (error) {
      expect(error).toBeInstanceOf(OwnerApiError);
      expect(error).toMatchObject({ status, code, problems, currentRevision: 9 });
    }
  });
  test("a later list read cannot advance the ETag of an open detail", async () => {
    const opened = await ownerApi.get(id);
    serve(() => json(list, 200, '"os-99"'));
    await ownerApi.list();
    serve(() => json({ commit }));
    await ownerApi.replace(id, { contract }, opened.etag);
    expect(new Headers(requests.at(-1)!.init.headers).get("If-Match")).toBe(etag);
  });
  test.each(["list", "get"] as const)("%s without ETag is refused before editing", async (route) => {
    serve(() => json(route === "list" ? list : detail, 200, null));
    await expect(route === "list" ? ownerApi.list() : ownerApi.get(id)).rejects.toBeInstanceOf(OwnerApiError);
  });
});

describe("owner workflow view", () => {
  test("lists state, publication and configuration problems", async () => {
    mount();
    const rows = await screen.findByRole("list", { name: "Owner workflows" });
    expect(within(rows).getByRole("button", { name: `Manage ${id}` })).toBeVisible();
    expect(within(rows).getByText("Enabled")).toBeVisible();
    expect(within(rows).getByText(/Unpublished/)).toBeVisible();
    expect(within(rows).getByText(/Not ready: needs configuration/)).toBeVisible();
  });
  test("shows the record and history with a restore action", async () => {
    const editor = await open();
    expect(JSON.parse((editor as HTMLTextAreaElement).value)).toEqual(contract);
    fireEvent.click(screen.getByRole("button", { name: "Restore revision 6" }));
    await waitFor(() => expect(writes().some((request) => request.url.endsWith("/restore"))).toBe(true));
    expect(body(writes()[0])).toEqual({ record_revision: 6 });
    expect(new Headers(writes()[0].init.headers).get("If-Match")).toBe(etag);
  });
  test("Check displays every 422 problem at its path", async () => {
    serve(({ url, method }) => url.endsWith("/check") ? json({ report: { ok: false, errors: problems } })
      : json(method === "GET" && url === "/admin/api/workflows" ? list : detail));
    await open();
    fireEvent.click(screen.getByRole("button", { name: "Check" }));
    expect(await screen.findByText("/bindings/example")).toBeVisible();
    expect(screen.getByText(/Choose a binding\./)).toBeVisible();
  });
  test("Save displays 422 problems without losing the document", async () => {
    serve(({ url, method }) => method === "PUT" ? json(refusal("workflow_invalid", { problems }), 422)
      : json(url === "/admin/api/workflows" ? list : detail));
    const editor = await open();
    const changed = { ...contract, draft: true };
    fireEvent.change(editor, { target: { value: JSON.stringify(changed) } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("/bindings/example")).toBeVisible();
    expect(JSON.parse((screen.getByRole("textbox", { name: "Workflow JSON" }) as HTMLTextAreaElement).value)).toEqual(changed);
  });
  test("history is bounded to the last 20 returned entries", async () => {
    const entries = Array.from({ length: 25 }, (_, index) => ({ ...history[0], record_revision: 30 - index }));
    serve(({ url }) => json(url === "/admin/api/workflows" ? list : { workflow: { ...workflow, history: entries } }));
    await open();
    expect(screen.getAllByRole("button", { name: /^Restore revision / })).toHaveLength(20);
    expect(screen.getByRole("button", { name: "Restore revision 30" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Restore revision 10" })).toBeNull();
  });
  test("all four fact pick-lists take their options only from choices", async () => {
    await open();
    for (const [label, values] of [["Exposure", choices.exposure], ["Trust tier", choices.trust_tier],
      ["Resource tier", choices.resource_tier], ["Resource class", choices.resource_class]] as const) {
      const options = within(screen.getByRole("combobox", { name: label })).getAllByRole("option") as HTMLOptionElement[];
      expect(options.filter((option) => option.value !== "").map((option) => option.value)).toEqual(values);
    }
  });
  test("changing one fact keeps tags and the other facts", async () => {
    await open();
    fireEvent.change(screen.getByRole("combobox", { name: "Exposure" }), { target: { value: choices.exposure[1] } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(body(writes()[0]).facts).toEqual({ ...facts, exposure: choices.exposure[1] });
  });
  test("Clear facts sends explicit null on Save", async () => {
    await open();
    fireEvent.click(screen.getByRole("button", { name: "Clear facts" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(body(writes()[0])).toMatchObject({ facts: null });
  });
  test("saving a bundled workflow names it in overrides", async () => {
    serve(({ url, method }) => method !== "GET" ? json({ commit })
      : json(url === "/admin/api/workflows" ? list : { workflow: { ...workflow, provenance: { trusted: true, source: "local" } } }));
    const editor = await open();
    fireEvent.change(editor, { target: { value: JSON.stringify({ ...contract, edited: true }) } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(body(writes()[0]).overrides).toBe(id);
  });
  test("saving an owner workflow sends no overrides", async () => {
    const editor = await open();
    fireEvent.change(editor, { target: { value: JSON.stringify({ ...contract, edited: true }) } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(body(writes()[0])).not.toHaveProperty("overrides");
  });
  test("a detail in the unwrapped shape is refused", async () => {
    serve(() => json(workflow));
    await expect(ownerApi.get(id)).rejects.toMatchObject({ code: "invalid_response" });
  });
  test("saving an unchanged facts section omits facts", async () => {
    await open();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(body(writes()[0])).not.toHaveProperty("facts");
  });
  test("new workflow saves through create using the list ETag", async () => {
    mount();
    await screen.findByRole("button", { name: `Manage ${id}` });
    fireEvent.click(screen.getByRole("button", { name: "New workflow" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Workflow JSON" }), { target: { value: JSON.stringify(contract) } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0].url).toBe("/admin/api/workflows");
    expect(writes()[0].method).toBe("POST");
    expect(new Headers(writes()[0].init.headers).get("If-Match")).toBe(etag);
  });
  test.each([
    { label: "Publish", path: "publish", payload: { published: true } },
    { label: "Disable", path: "state", payload: { state: "disabled" } },
  ])("$label sends the opened record ETag", async ({ label, path, payload }) => {
    await open();
    fireEvent.click(screen.getByRole("button", { name: label }));
    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0].url).toBe(`/admin/api/workflows/${id}/${path}`);
    expect(new Headers(writes()[0].init.headers).get("If-Match")).toBe(etag);
    expect(body(writes()[0])).toEqual(payload);
  });
  test("412 reloads list and detail, keeps the draft locked, then accepts Reload latest", async () => {
    let refused = false;
    serve(({ url, method }) => {
      if (method === "PUT") { refused = true; return json({ error: { code: "generation_mismatch", message: "Server sentence.", current_revision: 9 } }, 412); }
      if (url === "/admin/api/workflows") return json(list, 200, refused ? '"os-9"' : etag);
      return json({ workflow: { ...workflow, contract: refused ? { ...contract, latest: true } : contract } }, 200, refused ? '"os-9"' : etag);
    });
    const editor = await open();
    fireEvent.change(editor, { target: { value: JSON.stringify({ ...contract, unsaved: true }) } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/The workflow changed since it was opened\./)).toBeVisible();
    await waitFor(() => {
      expect(requests.filter((request) => request.method === "GET" && request.url === "/admin/api/workflows").length).toBeGreaterThanOrEqual(2);
      expect(requests.filter((request) => request.method === "GET" && request.url.endsWith(`/${id}`)).length).toBeGreaterThanOrEqual(2);
    });
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect((screen.getByRole("textbox", { name: "Workflow JSON" }) as HTMLTextAreaElement).value).toContain("unsaved");
    fireEvent.click(screen.getByRole("button", { name: "Reload latest" }));
    await waitFor(() => expect((screen.getByRole("textbox", { name: "Workflow JSON" }) as HTMLTextAreaElement).value).toContain("latest"));
  });
  test("bundled deletion is refused with the server reason", async () => {
    serve(({ url, method }) => method === "DELETE" ? json(refusal("id_is_bundled"), 409)
      : json(url === "/admin/api/workflows" ? list : detail));
    await open();
    fireEvent.click(screen.getByRole("button", { name: "Delete workflow" }));
    expect(await screen.findByText(/included with Azizam and cannot be deleted/)).toBeVisible();
    expect(screen.getByRole("dialog")).toBeVisible();
  });
  test("invalid JSON never reaches check or save", async () => {
    const editor = await open();
    fireEvent.change(editor, { target: { value: "{" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(writes()).toHaveLength(0);
    expect(requests.filter((request) => request.url.endsWith("/check"))).toHaveLength(0);
  });
  test("a non-owner wrapper neither renders controls nor fetches owner data", async () => {
    mount(false);
    await act(async () => {});
    expect(screen.queryByRole("heading", { name: "Manage workflows" })).toBeNull();
    expect(requests).toHaveLength(0);
  });
  test.each(["member", "admin"])("the %s page keeps the catalog and never fetches owner routes", async (role) => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    clients.push(client);
    const viewer = { authenticated: true, name: "Sam", email: null, role, admin: role === "admin" } as Viewer;
    render(<QueryClientProvider client={client}><ViewerContext.Provider value={viewer}><WorkflowsPage /></ViewerContext.Provider></QueryClientProvider>);
    await waitFor(() => expect(requests.some((request) => request.url === "/me/api/workflows")).toBe(true));
    expect(screen.queryByRole("heading", { name: "Manage workflows" })).toBeNull();
    expect(requests.some((request) => request.url.startsWith("/admin/api/workflows"))).toBe(false);
  });
  test.each(["owner", "superadmin"])("the %s page mounts the owner view", async (role) => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    clients.push(client);
    const viewer = { authenticated: true, name: "Avery", email: null, role, admin: true } as Viewer;
    render(<QueryClientProvider client={client}><ViewerContext.Provider value={viewer}><WorkflowsPage /></ViewerContext.Provider></QueryClientProvider>);
    expect(await screen.findByRole("heading", { name: "Manage workflows" })).toBeVisible();
    expect(await screen.findByRole("button", { name: `Manage ${id}` })).toBeVisible();
    expect(requests.some((request) => request.url === "/admin/api/workflows")).toBe(true);
  });
});
