import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
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

function event(id: string, action: string, actorId = "usr_1") {
  return {
    id,
    created_at: 1_770_000_000,
    actor_id: actorId,
    actor_kind: "admin",
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

const compactTarget = (element: HTMLElement) =>
  expect(element.className).toContain("min-h-11");

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

  it("marks every compact Audit action as a 44px phone target", async () => {
    auditList.mockResolvedValue(page(familyRecords, "cursor-1"));
    usersList.mockResolvedValue([]);
    const user = userEvent.setup();
    renderAudit();
    compactTarget(await screen.findByRole("button", { name: "Load more" }));
    compactTarget(screen.getByRole("button", { name: "access" }));
    compactTarget(
      screen.getAllByRole("button", { name: "View full event record" })[0]!,
    );
    await user.click(
      screen.getAllByRole("button", { name: "View full event record" })[0]!,
    );
    compactTarget(screen.getByRole("button", { name: "Copy event JSON" }));
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

  it("marks compact retries, controls, disclosures, and dialog actions as 44px phone targets", async () => {
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

    compactTarget(await screen.findByRole("button", { name: "Configure" }));
    compactTarget(screen.getByRole("button", { name: "Add extension" }));
    compactTarget(screen.getByRole("button", { name: "Edit" }));
    await user.click(screen.getByRole("button", { name: "Configure" }));
    compactTarget(screen.getByRole("switch", { name: "Enable OIDC sign-in" }));
    compactTarget(screen.getByText("Advanced options"));
    compactTarget(screen.getByRole("button", { name: "Cancel" }));
    compactTarget(screen.getByRole("button", { name: "Save" }));
  });
});
