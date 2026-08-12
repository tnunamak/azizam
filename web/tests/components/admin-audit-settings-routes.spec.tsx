import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const adminFetch = vi.hoisted(() => vi.fn());
const auditList = vi.hoisted(() => vi.fn());
const auditGet = vi.hoisted(() => vi.fn());
const usersList = vi.hoisted(() => vi.fn());
const route = vi.hoisted(() => ({
  search: "",
  replace: vi.fn(),
  searchParams: new URLSearchParams(),
}));

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

vi.stubGlobal("ResizeObserver", ResizeObserverStub);

vi.mock("next/navigation", () => ({
  usePathname: () => "/admin/audit",
  useRouter: () => ({ replace: route.replace }),
  useSearchParams: () => route.searchParams,
}));

vi.mock("@/lib/api", () => ({
  adminFetch,
  adminRegistryMutation: vi.fn(),
  ApiError: class ApiError extends Error {},
  api: {
    audit: { list: auditList, get: auditGet },
    users: { list: usersList },
  },
  queryKeys: {
    audit: (params: unknown) => ["audit", params],
    auditEvent: (eventId: string | null) => ["audit", "event", eventId],
  },
}));

import AdminAuditPage from "@/app/admin/audit/page";
import AdminSettingsPage from "@/app/admin/settings/page";

function renderSettings() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <AdminSettingsPage />
    </QueryClientProvider>,
  );
}

function renderAudit() {
  route.replace.mockImplementation((href: string) => {
    window.history.replaceState({}, "", href);
  });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const page = () => (
    <QueryClientProvider client={client}>
      <AdminAuditPage />
    </QueryClientProvider>
  );
  const view = render(page());
  return { ...view, rerenderAudit: () => view.rerender(page()) };
}

function event(
  id: string,
  action: string,
  actorId = "usr_1",
  actorKind: "admin" | "cli" | "service_account" | "system" | "user" = "admin",
  actorLabel = "Admin",
) {
  return {
    id,
    created_at: 1_770_000_000,
    actor_id: actorId,
    actor_kind: actorKind,
    actor_label: actorLabel,
    action,
    target_kind: "app",
    target_id: "app_1",
    metadata: { name: "Example" },
  };
}

function page(
  records: ReturnType<typeof event>[],
  next_cursor: string | null = null,
) {
  return { records, next_cursor };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  route.search = "";
  route.searchParams = new URLSearchParams();
  window.history.replaceState({}, "", "/admin/audit");
});

describe("Admin Audit mounted behavior", () => {
  const familyRecords = [
    event("app", "app.create"),
    event("credential", "credential.create"),
    event("profile", "profile.create"),
    event("admin-key", "admin_key.create"),
    event("access", "runtime_profile_mapping.create"),
    event("operator", "operator.user_access_defaults.update"),
    event("actor-two", "app.disable", "usr_2"),
  ];

  it("sends every visible category to its exact server predicate", async () => {
    auditList.mockResolvedValue(page(familyRecords));
    usersList.mockResolvedValue([]);
    const user = userEvent.setup();
    renderAudit();

    await screen.findByRole("button", { name: "access" });
    for (const [family, actionPrefix] of [
      ["access", "runtime_profile_mapping."],
      ["admin_key", "admin_key."],
      ["app", "app."],
      ["credential", "credential."],
      ["operator", "operator."],
      ["profile", "profile."],
    ]) {
      await user.click(screen.getByRole("button", { name: family }));
      await waitFor(() => {
        expect(auditList).toHaveBeenCalledWith(
          expect.objectContaining({ action_prefix: actionPrefix }),
        );
      });
    }
  });

  it("rejects stale first and Load more generations, resets pending state, and clears only stale errors", async () => {
    const initial = deferred<ReturnType<typeof page>>();
    const filtered = deferred<ReturnType<typeof page>>();
    const more = deferred<ReturnType<typeof page>>();
    const nextFilter = deferred<ReturnType<typeof page>>();
    auditList.mockImplementation(
      (params: {
        actor_id?: string;
        action_prefix?: string;
        before?: string;
      }) => {
        if (params.before) return more.promise;
        if (params.actor_id === "usr_2") return filtered.promise;
        if (params.actor_id === "usr_1") return nextFilter.promise;
        return initial.promise;
      },
    );
    usersList.mockResolvedValue([]);
    const user = userEvent.setup();
    const view = renderAudit();

    route.searchParams = new URLSearchParams("?actor=usr_2");
    view.rerenderAudit();
    await waitFor(() =>
      expect(auditList).toHaveBeenCalledWith(
        expect.objectContaining({ actor_id: "usr_2" }),
      ),
    );
    filtered.resolve(
      page([event("current", "credential.create", "usr_2")], "cursor-2"),
    );
    await screen.findByText("Created key");
    initial.resolve(page([event("stale-first", "app.create")], "cursor-1"));
    await waitFor(() =>
      expect(screen.queryByText("Created app")).not.toBeInTheDocument(),
    );

    await user.click(screen.getByRole("button", { name: "Load more" }));
    expect(screen.getByRole("button", { name: "Load more" })).toHaveAttribute(
      "aria-busy",
      "true",
    );
    route.searchParams = new URLSearchParams("?actor=usr_1");
    view.rerenderAudit();
    await waitFor(() =>
      expect(auditList).toHaveBeenCalledWith(
        expect.objectContaining({ actor_id: "usr_1" }),
      ),
    );
    nextFilter.resolve(
      page([event("actor-one", "credential.create", "usr_1")], "cursor-3"),
    );
    const nextLoadMore = await screen.findByRole("button", {
      name: "Load more",
    });
    expect(nextLoadMore).not.toHaveAttribute("aria-busy", "true");
    more.reject(new Error("stale failure"));
    await waitFor(() =>
      expect(
        screen.queryByText("Couldn't load more audit events."),
      ).not.toBeInTheDocument(),
    );
  });

  it("keeps an active pagination error until a filter transition clears it", async () => {
    auditList.mockImplementation((params: { before?: string }) => {
      if (params.before) return Promise.reject(new Error("offline"));
      return Promise.resolve(page(familyRecords, "cursor-1"));
    });
    usersList.mockResolvedValue([]);
    const user = userEvent.setup();
    renderAudit();

    await user.click(await screen.findByRole("button", { name: "Load more" }));
    expect(
      await screen.findByText("Couldn't load more audit events."),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "credential" }));
    await waitFor(() =>
      expect(
        screen.queryByText("Couldn't load more audit events."),
      ).not.toBeInTheDocument(),
    );
  });

  it("clears deep-linked actor and category atomically while preserving event and return context", async () => {
    route.search = "?event=app&actor=usr_1&category=app&return=%2Fadmin";
    route.searchParams = new URLSearchParams(route.search);
    window.history.replaceState({}, "", `/admin/audit${route.search}`);
    auditList.mockImplementation((params: { actor_id?: string }) =>
      Promise.resolve(params.actor_id ? page([]) : page(familyRecords)),
    );
    auditGet.mockResolvedValue(event("app", "app.create"));
    usersList.mockResolvedValue([]);
    const user = userEvent.setup();
    renderAudit();

    fireEvent.click(
      await screen.findByRole("button", { name: "Clear filters" }),
    );
    expect(route.replace).toHaveBeenLastCalledWith(
      "/admin/audit?event=app&return=%2Fadmin",
    );
  });

  it("preserves deep-link filters and return context through event open and close", async () => {
    route.search = "?actor=usr_1&category=app&return=%2Fadmin";
    route.searchParams = new URLSearchParams(route.search);
    window.history.replaceState({}, "", `/admin/audit${route.search}`);
    auditList.mockResolvedValue(page(familyRecords));
    usersList.mockResolvedValue([]);
    const user = userEvent.setup();
    renderAudit();

    await screen.findAllByRole("button", { name: "View full event record" });
    await user.click(
      screen.getAllByRole("button", { name: "View full event record" })[0]!,
    );
    expect(route.replace).toHaveBeenLastCalledWith(
      "/admin/audit?actor=usr_1&category=app&return=%2Fadmin&event=app",
    );
    await user.keyboard("{Escape}");
    await waitFor(() =>
      expect(route.replace).toHaveBeenLastCalledWith(
        "/admin/audit?actor=usr_1&category=app&return=%2Fadmin",
      ),
    );
  });

  it("emits canonical target links with the full Audit return state", async () => {
    auditList.mockResolvedValue(page(familyRecords, "cursor-1"));
    usersList.mockResolvedValue([]);
    route.searchParams = new URLSearchParams(
      "?actor=usr_1&category=app&event=app",
    );
    auditGet.mockResolvedValue(event("app", "app.create"));
    renderAudit();
    expect(
      await screen.findByRole("link", { name: "Example" }),
    ).toHaveAttribute(
      "href",
      "/admin/apps?app=app_1&return=%2Fadmin%2Faudit%3Factor%3Dusr_1%26category%3Dapp%26event%3Dapp",
    );
  });

  it("renders the server-authoritative actor_label for human, service-account, and system events, unaffected by the users list", async () => {
    const humanEntry = event(
      "human",
      "app.create",
      "usr_1",
      "user",
      "Priya Shah",
    );
    const serviceEntry = event(
      "service",
      "operator.service.updated",
      "svc_backup",
      "service_account",
      "Backup automation",
    );
    const systemEntry = event("system", "app.disable", "cli", "cli", "System");
    auditList.mockResolvedValue(
      page([humanEntry, serviceEntry, systemEntry]),
    );
    // The users list is deliberately wrong/stale/absent for these actor IDs —
    // actor_label must not be derived from it.
    usersList.mockResolvedValue([
      { id: "usr_1", display_name: "Someone Else", email: "other@example.com" },
    ]);
    renderAudit();

    await screen.findByText("Created app");
    const feed = document.querySelector<HTMLElement>(".vf-activity-feed")!;
    expect(within(feed).getByText("Priya Shah")).toBeVisible();
    expect(within(feed).getByText("Backup automation")).toBeVisible();
    expect(within(feed).getByText("System")).toBeVisible();
  });

  it("keeps rendering actor_label after the current-user identity changes", async () => {
    const entry = event("evt", "app.create", "usr_1", "user", "Priya Shah");
    auditList.mockResolvedValue(page([entry]));
    usersList.mockResolvedValue([]);
    const view = renderAudit();

    await screen.findByText("Created app");
    let feed = document.querySelector<HTMLElement>(".vf-activity-feed")!;
    expect(within(feed).getByText("Priya Shah")).toBeVisible();

    // Simulate a current-user/session identity change and a refetch of the
    // same event — the label must still come from the event's own
    // actor_label, not from whichever user is currently signed in.
    usersList.mockResolvedValue([
      { id: "usr_1", display_name: "New Session User" },
    ]);
    auditList.mockResolvedValue(page([entry]));
    view.rerenderAudit();

    await waitFor(() => {
      feed = document.querySelector<HTMLElement>(".vf-activity-feed")!;
      expect(within(feed).getByText("Priya Shah")).toBeVisible();
    });
    expect(within(feed).queryByText("New Session User")).not.toBeInTheDocument();
  });
});

describe("Admin Settings mounted behavior", () => {
  it("does not open sign-in or media editors after their reads fail", async () => {
    adminFetch.mockRejectedValue(new Error("unavailable"));
    renderSettings();

    await waitFor(() => {
      expect(screen.getByText("Couldn't load sign-in settings.")).toBeVisible();
      expect(
        screen.getByText("Couldn't load media admission settings."),
      ).toBeVisible();
    });
    expect(screen.getByRole("button", { name: "Configure" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Edit" })).toBeDisabled();
  });

  it("reports OIDC required fields through Field accessibility wiring", async () => {
    adminFetch.mockImplementation(async (path: string) => {
      if (path === "/admin/api/oidc-config") return { oidc_config: null };
      if (path === "/admin/api/extensions")
        return { extension_packages: { packages: {} } };
      if (path === "/admin/api/operator/overrides")
        return { overrides: { media_job_admission: null } };
      throw new Error(`unexpected request: ${path}`);
    });
    const user = userEvent.setup();
    renderSettings();

    await user.click(await screen.findByRole("button", { name: "Configure" }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(
      await screen.findByText("Fix the highlighted fields before saving."),
    ).toBeVisible();
    await waitFor(() => expect(screen.getByRole("alert")).toHaveFocus());
    expect(screen.getByLabelText(/Issuer URL/)).toHaveAttribute(
      "aria-invalid",
      "true",
    );
    expect(screen.getByLabelText(/Client ID/)).toHaveAttribute(
      "aria-invalid",
      "true",
    );
    expect(screen.getByLabelText(/Redirect URI/)).toHaveAttribute(
      "aria-invalid",
      "true",
    );
  });

  function effectiveOidcConfig(
    overrides: Partial<{
      source: "bootstrap" | "database" | "merged" | "runtime_default";
    }> = {},
  ) {
    return {
      enabled: true,
      issuer: "https://accounts.example.com",
      client_id: "client-123",
      client_secret: "<redacted>",
      client_secret_set: true,
      redirect_uri: "https://gateway.example.com/auth/callback",
      scopes: ["openid", "email", "profile"],
      allowed_email_domains: [],
      admin_emails: [],
      admin_groups: [],
      admin_roles: [],
      authorization_endpoint: "",
      token_endpoint: "",
      jwks_uri: "",
      clock_skew_seconds: 60,
      groups_claim: "groups",
      updated_at: 1_770_000_000,
      apply_mode: "live" as const,
      provenance: { summary: overrides.source ?? "bootstrap", fields: {} },
      source: overrides.source ?? "bootstrap",
    };
  }

  function mockSettingsFetch(oidcConfig: unknown) {
    adminFetch.mockImplementation(async (path: string) => {
      if (path === "/admin/api/oidc-config") return { oidc_config: oidcConfig };
      if (path === "/admin/api/extensions")
        return { extension_packages: { packages: {} } };
      if (path === "/admin/api/operator/overrides")
        return { overrides: { media_job_admission: null } };
      throw new Error(`unexpected request: ${path}`);
    });
  }

  it("shows an honest unavailable state when no OIDC configuration is in effect", async () => {
    mockSettingsFetch(null);
    renderSettings();

    expect(
      await screen.findByText(
        "OIDC sign-in is not configured. No bootstrap or saved values are in effect.",
      ),
    ).toBeVisible();
    expect(
      screen.queryByText(/will be shown when the typed API contract/i),
    ).not.toBeInTheDocument();
  });

  it.each([
    ["bootstrap", "Bootstrap config"],
    ["database", "Saved in dashboard"],
    ["merged", "Bootstrap + dashboard (merged)"],
  ] as const)(
    "renders the %s provenance and live apply mode truthfully",
    async (source, label) => {
      mockSettingsFetch(effectiveOidcConfig({ source }));
      renderSettings();

      expect(await screen.findByText(label)).toBeVisible();
      expect(
        screen.getByText("Applied live — no restart required"),
      ).toBeVisible();
    },
  );
});
