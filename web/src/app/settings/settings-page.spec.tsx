import { QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  role: "owner",
  success: vi.fn(),
}));
vi.mock("@/components/shell/session", () => ({
  useViewer: () => ({ role: mocks.role }),
}));
vi.mock("@/lib/toast", () => ({
  toast: { success: mocks.success, error: vi.fn() },
}));

import { createQueryClient, resetSessionCache } from "@/lib/api";
import SettingsPage from "@/app/settings/page";
import {
  emptyPreview,
  firstRunSnapshot,
  liveSnapshot,
} from "@/app/settings/sign-in/test-support";

type State = "seeded" | "empty" | "error";
type Request = {
  method: string;
  path: string;
  body: unknown;
  ifMatch: string | null;
};
type Reply = {
  status?: number;
  json: unknown;
  headers?: Record<string, string>;
};
type Handler = (request: Request) => Reply | Promise<Reply>;

// A tiny gateway: the real adminFetch runs against it, so ApiError, CSRF and
// If-Match handling are the production ones. Shapes are copied from the
// browser fixtures this spec replaces.
const routes = new Map<string, Handler>();
const requests: Request[] = [];
const ownerRequired: Reply = {
  status: 403,
  json: { error: { code: "owner_required", message: "Owner required" } },
};

function seed(state: State) {
  const status = state === "error" ? 503 : 200;
  const reply =
    (json: unknown): Handler =>
    () => ({ status, json });
  routes.set("/admin/api/session", () => ({
    json: {
      actor: {
        subject: "owner",
        csrf_token: "csrf",
        roles: ["owner"],
        email: null,
      },
    },
  }));
  routes.set(
    "/admin/api/settings/receipts",
    reply({ revision: "7", retention_days: null }),
  );
  routes.set(
    "/admin/api/settings/instance",
    reply({
      revision: "7",
      instance: {
        name: "Our gateway",
        base_url: "https://gateway.example.test",
        theme_default: "system",
      },
    }),
  );
  routes.set(
    "/admin/api/settings/prompt-memory",
    reply({
      revision: "7",
      prompt_memory: {
        default_remember: true,
        never_keep: false,
        retention_cap: "never",
      },
    }),
  );
  routes.set(
    "/admin/api/settings/defaults",
    reply({
      defaults: { invite_access_plan: state === "empty" ? null : "plan_local" },
      aliases: state === "empty" ? {} : { chat: "local-chat" },
    }),
  );
  routes.set(
    "/admin/api/settings/effective",
    reply({
      config: { proxy: { port: 18000 }, secret: "<redacted>" },
      export_url: "/admin/api/settings/effective/export",
    }),
  );
  routes.set(
    "/admin/api/signin",
    reply(state === "empty" ? firstRunSnapshot() : liveSnapshot()),
  );
  routes.set("/admin/api/signin/preview", () => ({ json: emptyPreview() }));
  routes.set(
    "/admin/api/operator/overrides",
    reply({
      overrides: {
        revisions: { media_job_admission_overrides: "7" },
        media_job_admission:
          state === "empty"
            ? {}
            : { max_queue_depth: 5, retry_after_seconds: 30 },
      },
    }),
  );
  routes.set(
    "/admin/api/extensions",
    reply({
      extension_packages: {
        packages:
          state === "empty"
            ? {}
            : {
                recordings: {
                  id: "recordings",
                  version: "1.0",
                  enabled: true,
                  enabled_at: 1_000,
                },
              },
      },
    }),
  );
  routes.set("/admin/api/access-plans", reply({ access_plans: [localPlan] }));
}
const localPlan = {
  id: "plan_local",
  name: "Local only",
  created_at: 1,
  updated_at: 1,
  plan: { id: "plan_local", name: "Local only" },
  summary: "Fixture local access",
};

beforeEach(() => {
  mocks.role = "owner";
  routes.clear();
  requests.length = 0;
  resetSessionCache();
  Element.prototype.hasPointerCapture ??= () => false;
  Element.prototype.releasePointerCapture ??= () => {};
  Element.prototype.scrollIntoView ??= () => {};
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  vi.stubGlobal(
    "fetch",
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://gateway.test");
      const headers = new Headers(init?.headers);
      const request: Request = {
        method: init?.method ?? "GET",
        path: url.pathname,
        body:
          typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
        ifMatch: headers.get("if-match"),
      };
      requests.push(request);
      const handler = routes.get(url.pathname);
      if (!handler)
        throw new Error(`unrouted ${request.method} ${url.pathname}`);
      const reply = await handler(request);
      return new Response(JSON.stringify(reply.json), {
        status: reply.status ?? 200,
        headers: { "content-type": "application/json", ...reply.headers },
      });
    },
  );
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function mount({ keepClientRetry = false }: { keepClientRetry?: boolean } = {}) {
  const client = createQueryClient();
  // The production client retries 5xx with a real back-off; a spec cannot wait for it.
  if (!keepClientRetry)
    client.setDefaultOptions({
      queries: { ...client.getDefaultOptions().queries, retry: false },
    });
  return render(
    <QueryClientProvider client={client}>
      <SettingsPage />
    </QueryClientProvider>,
  );
}
const region = (name: string) => screen.getByRole("region", { name });
async function editSection(name: string) {
  const user = userEvent.setup();
  const section = await screen.findByRole("region", { name });
  await user.click(
    await within(section).findByRole("button", { name: `Edit ${name}` }),
  );
  return { user, section };
}
async function chooseOption(
  user: ReturnType<typeof userEvent.setup>,
  field: string,
  option: string,
) {
  await user.click(screen.getByRole("combobox", { name: field }));
  await user.click(await screen.findByRole("option", { name: option }));
}
const sent = (method: string, path: string) =>
  requests.filter((r) => r.method === method && r.path === path);
const SECTIONS = [
  "Instance",
  "Sign-in",
  "People and access",
  "Prompts",
  "Receipts",
  "Media admission",
  "Extensions",
  "Advanced",
];

describe("settings page states", () => {
  it("shows every section with the saved values and only opens an editor on Edit (seeded)", async () => {
    seed("seeded");
    mount();
    for (const name of SECTIONS)
      expect(
        await screen.findByRole("heading", { name, level: 2 }),
      ).toBeVisible();
    expect(await screen.findByText("Our gateway")).toBeVisible();
    expect(screen.queryByLabelText("Instance name")).not.toBeInTheDocument();
    const { section: instance } = await editSection("Instance");
    expect(within(instance).getByLabelText("Instance name")).toHaveValue(
      "Our gateway",
    );
    // Sign-in is not behind an Edit button: its three blocks are on the page.
    const signIn = region("Sign-in");
    expect(
      await within(signIn).findByRole("heading", { name: "Ways to sign in" }),
    ).toBeVisible();
    expect(
      within(signIn).getByRole("switch", { name: "Keycloak" }),
    ).toBeChecked();
    expect(
      within(signIn).queryByRole("button", { name: "Edit Sign-in" }),
    ).not.toBeInTheDocument();
    const { user } = await editSection("Advanced");
    await user.click(screen.getByText("Effective configuration and export"));
    expect(
      await screen.findByRole("link", { name: "Export redacted snapshot" }),
    ).toHaveAttribute("href", "/admin/api/settings/effective/export");
  });

  it("shows the first-run sign-in and no extensions (empty)", async () => {
    seed("empty");
    mount();
    expect(
      await within(
        await screen.findByRole("region", { name: "Extensions" }),
      ).findByText("No extensions installed"),
    ).toBeVisible();
    await editSection("Instance");
    const signIn = region("Sign-in");
    expect(await within(signIn).findByText("Not set up")).toBeVisible();
    expect(
      within(signIn).getByText(
        "People sign in with a password. To add another way to sign in, connect a provider below.",
      ),
    ).toBeVisible();
    const { user } = await editSection("Extensions");
    expect(
      screen.getAllByText("No extensions installed").length,
    ).toBeGreaterThan(0);
    await editSection("Advanced");
    await user.click(screen.getByText("Effective configuration and export"));
    expect(
      await screen.findByRole("link", { name: "Export redacted snapshot" }),
    ).toBeVisible();
  });

  it("offers a retry in every section when the gateway is down (error)", async () => {
    seed("error");
    mount();
    expect(
      await within(
        await screen.findByRole("region", { name: "Receipts" }),
      ).findByText("Couldn’t load receipt settings"),
    ).toBeVisible();
    const user = userEvent.setup();
    await editSection("Advanced");
    await user.click(screen.getByText("Effective configuration and export"));
    await waitFor(() =>
      expect(screen.getAllByRole("button", { name: "Try again" })).toHaveLength(
        8,
      ),
    );
    for (const name of ["People and access"]) {
      const retry = within(region(name)).getByRole("button", {
        name: "Try again",
      });
      // 44 px target: the production wrapper forces min-h-11 on its buttons.
      expect(retry.closest("[class*='[&_button]:min-h-11']")).not.toBeNull();
    }
  });
});

describe("settings loading and failure", () => {
  it("announces the sign-in settings as busy until they arrive", async () => {
    seed("seeded");
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => (release = resolve));
    routes.set("/admin/api/signin", async () => {
      await gate;
      return { json: liveSnapshot() };
    });
    mount();
    const status = await within(
      await screen.findByRole("region", { name: "Sign-in" }),
    ).findByRole("status", {
      name: "Loading sign-in",
    });
    expect(status).toHaveAttribute("aria-busy", "true");
    release();
    await waitFor(() => expect(status).not.toBeInTheDocument());
  });

  it("retries a failed load, then keeps the typed draft when a save fails", async () => {
    seed("seeded");
    let failing = true;
    routes.set("/admin/api/settings/instance", () =>
      failing
        ? {
            status: 503,
            json: {
              error: {
                code: "unavailable",
                message: "Instance could not be saved. Try again.",
              },
            },
          }
        : {
            json: {
              instance: {
                name: "Recovered",
                base_url: "",
                theme_default: "system",
              },
              revision: "7",
            },
          },
    );
    mount();
    const section = await screen.findByRole("region", { name: "Instance" });
    const user = userEvent.setup();
    failing = false;
    await user.click(
      await within(section).findByRole("button", { name: "Try again" }),
    );
    await user.click(
      await within(section).findByRole("button", { name: "Edit Instance" }),
    );
    expect(await screen.findByLabelText("Instance name")).toHaveValue(
      "Recovered",
    );
    failing = true;
    await user.clear(screen.getByLabelText("Instance name"));
    await user.type(
      screen.getByLabelText("Instance name"),
      "Typed but not saved",
    );
    await user.click(screen.getByRole("button", { name: "Save instance" }));
    const alert = await within(section).findByRole("alert");
    expect(alert).toHaveTextContent("Unsaved changes");
    expect(alert).toHaveTextContent("Instance could not be saved");
    expect(screen.getByLabelText("Instance name")).toHaveValue(
      "Typed but not saved",
    );
  });

  it("shows the saved access plan for new invitations by name", async () => {
    seed("seeded");
    mount();
    expect(
      await screen.findByRole("combobox", {
        name: "Access plan for new invitations",
      }),
    ).toHaveTextContent("Local only");
  });
});

describe("settings roles", () => {
  it("does not retry 403s and tells an administrator that access defaults are the owner's", async () => {
    mocks.role = "admin";
    seed("seeded");
    routes.set("/admin/api/settings/instance", () => ownerRequired);
    routes.set("/admin/api/signin", () => ownerRequired);
    mount({ keepClientRetry: true });
    const instance = await screen.findByRole("region", { name: "Instance" });
    await waitFor(() =>
      expect(instance).toHaveTextContent("Only the owner can change this."),
    );
    expect(
      within(instance).queryByRole("button", { name: "Try again" }),
    ).not.toBeInTheDocument();
    await waitFor(() =>
      expect(region("Sign-in")).toHaveTextContent(
        "Only the owner can change this.",
      ),
    );
    expect(
      screen.queryByRole("button", { name: "Save changes" }),
    ).not.toBeInTheDocument();
    const access = await screen.findByRole("region", {
      name: "People and access",
    });
    await waitFor(() =>
      expect(access).toHaveTextContent("Only the owner can change this."),
    );
    expect(
      within(access).queryByRole("button", { name: "Save" }),
    ).not.toBeInTheDocument();
    expect(sent("GET", "/admin/api/settings/instance")).toHaveLength(1);
    expect(sent("GET", "/admin/api/signin")).toHaveLength(1);
  });

  it("hides owner-only forms from an administrator even when owner data is cached", async () => {
    mocks.role = "admin";
    seed("seeded");
    routes.set("/admin/api/signin", () => ({
      json: liveSnapshot({ viewer: { role: "admin", can_edit: false } }),
    }));
    mount();
    const signIn = region("Sign-in");
    await waitFor(() =>
      expect(signIn).toHaveTextContent("Only the owner can change sign-in."),
    );
    expect(
      screen.queryByRole("button", { name: "Save changes" }),
    ).not.toBeInTheDocument();
    expect(sent("POST", "/admin/api/signin/preview")).toHaveLength(0);
    await editSection("Instance");
    expect(
      screen.queryByRole("button", { name: "Save instance" }),
    ).not.toBeInTheDocument();
    const access = await screen.findByRole("region", {
      name: "People and access",
    });
    expect(access).toBeVisible();
    await waitFor(() =>
      expect(access).toHaveTextContent("Only the owner can change this."),
    );
    expect(
      within(access).queryByRole("combobox", {
        name: "Access plan for new invitations",
      }),
    ).not.toBeInTheDocument();
  });
});

describe("extensions", () => {
  it("enables the validated source only after an explicit code-trust choice", async () => {
    seed("empty");
    routes.set("/admin/api/extensions/validate", () => ({
      json: {
        review: {
          ok: true,
          package: { id: "trusted-fixture", version: "1" },
          errors: [],
          warnings: [],
          requires_code_trust: true,
          resolved_source: { resolved_ref: "fixture-commit" },
          object_counts: { services: 1 },
        },
      },
    }));
    routes.set("/admin/api/operator/service-registry", () => ({
      json: { service_registry: {} },
      headers: { etag: '"registry-1"' },
    }));
    routes.set("/admin/api/extensions/enable", () => ({ json: {} }));
    mount();
    const { user } = await editSection("Extensions");
    await user.click(screen.getByRole("button", { name: "Add extension" }));
    await user.type(
      screen.getByLabelText("Directory on the gateway"),
      "/extensions/fixture",
    );
    await user.click(screen.getByRole("button", { name: "Validate package" }));
    const enable = await screen.findByRole("button", {
      name: "Enable extension",
    });
    expect(enable).toBeDisabled();
    await user.click(
      screen.getByLabelText("I trust this package to run code on the gateway"),
    );
    expect(enable).toBeEnabled();
    await user.click(enable);
    await waitFor(() =>
      expect(mocks.success).toHaveBeenCalledWith("Extension enabled"),
    );
    expect(sent("POST", "/admin/api/extensions/enable")[0].ifMatch).toBe(
      '"registry-1"',
    );
    expect(sent("POST", "/admin/api/extensions/enable")[0].body).toEqual({
      source: { type: "local_directory", path: "/extensions/fixture" },
      review: { resolved_source: { resolved_ref: "fixture-commit" } },
    });
  });
});

describe("independent forms", () => {
  it("keeps the other form's draft when sign-in and defaults save", async () => {
    seed("seeded");
    let revision = "rev-1";
    routes.set("/admin/api/signin", (request) => {
      if (request.method === "PUT") {
        expect(request.ifMatch).toBe(revision);
        revision = "rev-2";
        return {
          json: {
            snapshot: liveSnapshot({
              revision,
              rules: { fallback: "open", rules: liveSnapshot().rules.rules },
            }),
            applied: { provider_turned_on: false, admin_removed: [] },
          },
        };
      }
      return { json: liveSnapshot({ revision }) };
    });
    mount();
    const user = userEvent.setup();

    // A draft in People and access, not saved yet.
    await screen.findByRole("combobox", {
      name: "Access plan for new invitations",
    });
    await chooseOption(user, "Access plan for new invitations", "Ask each time");

    // Save a sign-in change: the other draft stays.
    const signIn = region("Sign-in");
    await user.click(
      await within(signIn).findByRole("radio", {
        name: /Let them in, but with no access/,
      }),
    );
    const save = await within(signIn).findByRole("button", {
      name: "Save changes",
    });
    await waitFor(() => expect(save).toBeEnabled());
    await user.click(save);
    await waitFor(() =>
      expect(mocks.success).toHaveBeenCalledWith(
        "Saved. Changes apply to the next sign-in.",
      ),
    );
    expect(sent("PUT", "/admin/api/signin")[0].ifMatch).toBe("rev-1");
    expect(
      screen.getByRole("combobox", { name: "Access plan for new invitations" }),
    ).toHaveTextContent("Ask each time");

    // People and access: saving the invitation plan keeps the sign-in state.
    const access = region("People and access");
    routes.set("/admin/api/settings/defaults", (request) =>
      request.method === "PUT"
        ? { json: {} }
        : {
            json: {
              defaults: { invite_access_plan: null },
              aliases: { chat: "local-chat" },
            },
          },
    );
    await user.click(within(access).getByRole("button", { name: "Save" }));
    expect(
      await within(access).findByText("Access plan for new invitations saved"),
    ).toBeVisible();
    expect(sent("PUT", "/admin/api/settings/defaults")[0].body).toEqual({
      invite_access_plan: null,
    });
    expect(
      within(signIn).getByRole("radio", {
        name: /Let them in, but with no access/,
      }),
    ).toBeChecked();
  });
});

describe("long values", () => {
  it("keeps a 300-character name and a long URL in full: summary title, edit values", async () => {
    seed("seeded");
    const name = "n".repeat(300);
    const url = `https://example.test/${"path".repeat(100)}`;
    routes.set("/admin/api/settings/instance", () => ({
      json: {
        instance: { name, base_url: url, theme_default: "system" },
        revision: "7",
      },
    }));
    mount();
    const section = await screen.findByRole("region", { name: "Instance" });
    const label = await within(section).findByText(name);
    expect(label).toHaveAttribute("title", name);
    const detail = within(section).getByText(url, { exact: false });
    expect(detail.getAttribute("title")).toContain(url);
    expect(detail).toHaveClass("truncate");
    await editSection("Instance");
    expect(within(section).getByLabelText("Instance name")).toHaveValue(name);
    expect(within(section).getByLabelText("Public base URL")).toHaveValue(url);
  });
});

describe("default access choices", () => {
  it("starts at 'Ask each time', picks a plan by keyboard, returns focus to the trigger and links to the plans tab", async () => {
    seed("empty");
    routes.set("/admin/api/access-plans", () => ({
      json: { access_plans: [localPlan] },
    }));
    mount();
    const access = await screen.findByRole("region", {
      name: "People and access",
    });
    const plan = await within(access).findByRole("combobox", {
      name: "Access plan for new invitations",
    });
    expect(plan).toHaveTextContent("Ask each time");
    expect(
      within(access).getByText(/Nobody has a plan until you choose one/),
    ).toBeVisible();
    const user = userEvent.setup();
    plan.focus();
    await user.keyboard("{Enter}");
    expect(
      await screen.findByRole("option", { name: "Ask each time" }),
    ).toHaveFocus();
    await user.keyboard("{End}");
    expect(screen.getByRole("option", { name: "Local only" })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(plan).toHaveTextContent("Local only");
    expect(plan).toHaveFocus();
    const link = within(access).getByRole("link", {
      name: "View and edit access plans",
    });
    expect(link.getAttribute("href")).toMatch(/^\/people\/?\?tab=plans$/);
  });
});

// The revision-guarded forms share one contract: a dirty form keeps its
// draft across a background refresh, a clean form rebases onto it, a 409 asks
// for a reload, and every PUT carries the revision the form was last rebased to.
const revisionForms = [
  {
    section: "Instance",
    field: "Instance name",
    save: "Save instance",
    get: "/admin/api/settings/instance",
    put: "/admin/api/settings/instance",
    original: "Original",
    draft: "Unsaved name",
    refreshed: "External name",
    saved: "Normalized name",
  },
  {
    section: "Media admission",
    field: "Maximum queued jobs",
    save: "Save media admission",
    get: "/admin/api/operator/overrides",
    put: "/admin/api/operator/media-job-admission",
    original: "5",
    draft: "12",
    refreshed: "9",
    saved: "10",
  },
] as const;
type RevisionForm = (typeof revisionForms)[number];

function revisionResponse(
  form: RevisionForm,
  value: string,
  revision: string,
  mutation = false,
) {
  if (form.field === "Instance name")
    return {
      instance: { name: value, base_url: "", theme_default: "system" },
      revision,
    };
  const media = { max_queue_depth: Number(value) };
  return mutation
    ? { media_job_admission: media, revision }
    : {
        overrides: {
          media_job_admission: media,
          revisions: { media_job_admission_overrides: revision },
        },
      };
}
async function changeField(
  user: ReturnType<typeof userEvent.setup>,
  form: RevisionForm,
  value: string,
) {
  const input = screen.getByLabelText(form.field, { exact: true });
  await user.clear(input);
  await user.type(input, value);
}
async function expectField(form: RevisionForm, value: string) {
  await waitFor(() =>
    expect(
      screen.getByLabelText(form.field, { exact: true }),
    ).toHaveDisplayValue(value),
  );
}
// React Query refetches stale queries when the tab becomes visible again.
async function refresh(path: string) {
  vi.setSystemTime(Date.now() + 16_000);
  const before = sent("GET", path).length;
  const visibility = (value: string) => {
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value,
    });
    fireEvent(document, new Event("visibilitychange", { bubbles: true }));
  };
  visibility("hidden");
  visibility("visible");
  await waitFor(() => expect(sent("GET", path).length).toBeGreaterThan(before));
}

describe.each(revisionForms)("$save", (form) => {
  it("dirty refresh conflicts, clean refresh and successful response rebase revisions", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(1_800_000_000_000);
    seed("seeded");
    let value: string = form.original;
    let revision = "7";
    let conflict = true;
    const received: (string | null)[] = [];
    const handler: Handler = (request) => {
      if (request.method === "PUT") {
        received.push(request.ifMatch);
        if (conflict)
          return {
            status: 409,
            json: { error: { code: "settings_conflict", message: "Conflict" } },
          };
        value = form.saved;
        revision = "10";
        return { json: revisionResponse(form, value, revision, true) };
      }
      return { json: revisionResponse(form, value, revision) };
    };
    for (const path of new Set([form.get, form.put])) routes.set(path, handler);
    const { unmount } = mount();
    const { section, user } = await editSection(form.section);
    await expectField(form, form.original);
    await changeField(user, form, form.draft);
    value = form.refreshed;
    revision = "8";
    await refresh(form.get);
    await expectField(form, form.draft);
    await user.click(screen.getByRole("button", { name: form.save }));
    expect(await within(section).findByRole("alert")).toHaveTextContent(
      "Settings changed elsewhere; reload to see the latest",
    );
    await expectField(form, form.draft);
    expect(received).toEqual(["7"]);

    // "Reload": a new page load shows what the server holds.
    unmount();
    vi.setSystemTime(Date.now() + 16_000);
    mount();
    await editSection(form.section);
    await expectField(form, form.refreshed);
    value = form.original;
    revision = "9";
    await refresh(form.get);
    await expectField(form, form.original);
    conflict = false;
    await user.click(screen.getByRole("button", { name: form.save }));
    await expectField(form, form.saved);
    await user.click(screen.getByRole("button", { name: form.save }));
    await waitFor(() => expect(received).toEqual(["7", "9", "10"]));
  });

  it("save-time owner denial retains the draft", async () => {
    seed("seeded");
    routes.set(form.put, (request) =>
      request.method === "PUT"
        ? ownerRequired
        : { json: revisionResponse(form, form.original, "7") },
    );
    if (form.get !== form.put)
      routes.set(form.get, () => ({
        json: revisionResponse(form, form.original, "7"),
      }));
    mount();
    const { user, section } = await editSection(form.section);
    await screen.findByLabelText(form.field, { exact: true });
    await changeField(user, form, form.draft);
    await user.click(screen.getByRole("button", { name: form.save }));
    expect(await within(section).findByRole("alert")).toHaveTextContent(
      "Only the owner can change this.",
    );
    await expectField(form, form.draft);
  });
});
