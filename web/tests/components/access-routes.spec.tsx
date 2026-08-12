import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ConfirmDialog } from "@/components/ui";
import AdminAppsPage from "@/app/admin/apps/page";
import AdminProfilesPage from "@/app/admin/profiles/page";
import type { App, PolicyEditorContract, Profile, SimulateResult } from "@/lib/api";

const searchParams = vi.hoisted(() => new URLSearchParams());
const apiMock = vi.hoisted(() => ({
  session: vi.fn(),
  status: vi.fn(),
  apps: {
    list: vi.fn(),
  },
  users: {
    list: vi.fn(),
  },
  credentials: {
    list: vi.fn(),
  },
  profiles: {
    list: vi.fn(),
    create: vi.fn(),
  },
  runtimeProfileMappings: {
    list: vi.fn(),
  },
  policy: {
    simulate: vi.fn(),
  },
  policyEditor: { get: vi.fn() },
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => searchParams,
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: apiMock,
  };
});

function renderWithQuery(ui: React.ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return {
    queryClient,
    ...render(
      <QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>,
    ),
  };
}

function profile(overrides: Partial<Profile> = {}): Profile {
  return {
    id: "profile-a",
    name: "Profile A",
    description: null,
    profile: {},
    protocols: [],
    workflows: [],
    status_visibility: "basic",
    created_at: 1_700_000_000,
    ...overrides,
  };
}

function app(overrides: Partial<App> = {}): App {
  return {
    id: "app-a",
    name: "Fixture app",
    kind: "service",
    owner_user_id: null,
    owner_service_account_id: null,
    default_profile_id: null,
    effective_profile_id: "profile-a",
    effective_profile_name: "Profile A",
    effective_profile_source: "app_default",
    disabled_at: null,
    created_at: 1_700_000_000,
    ...overrides,
  };
}

function allowedResult(profileId: string): SimulateResult {
  return {
    profile_id: profileId,
    decision: {
      allowed: true,
      decision: "allowed",
      sources: [],
      reasons: [`profile:${profileId}`],
      visibility_level: "basic",
    },
  };
}

function policyEditor(): PolicyEditorContract {
  const dynamic = (id: string) => ({ id, label: id, description: null, available: true });
  return {
    schema_version: 1,
    profile: {
      create: { profile: { fallback: { cloud_allowed: false }, native_passthrough: { enabled: false, allowed_backends: [], allowed_operations: [], audit: true }, status_visibility: { level: "basic" }, target_policy: { visibility: "catalog", activation: "may_activate" } } },
      set_semantics: { omitted: "no_opinion", empty: "deny_all" },
      choices: { protocols: [dynamic("extension-api")], intents: [dynamic("extension.run")], workflows: [dynamic("extension.workflow")], models: [dynamic("extension-model")], exposure: [], trust_tiers: [], resource_tiers: [], tags: [dynamic("extension-tag")], status_visibility: [{ id: "basic", label: "Basic", description: null }], target_visibility: [], target_activation: [] },
    },
    runtime_mappings: { source_kinds: [], fallback_profile: { id: "fallback-from-server", label: "Gateway default", description: null, assignable: true } },
  };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  searchParams.forEach((_, key) => searchParams.delete(key));
});

beforeEach(() => {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn().mockReturnValue({ matches: true }),
  });
  class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  vi.stubGlobal("ResizeObserver", ResizeObserverStub);
  apiMock.policyEditor.get.mockResolvedValue(policyEditor());
});

describe("access route behavior", () => {
  it("keeps app credential joins explicit while loading and after failure", async () => {
    apiMock.apps.list.mockResolvedValue([app()]);
    apiMock.users.list.mockResolvedValue([]);
    apiMock.credentials.list.mockImplementation(() => new Promise(() => {}));
    apiMock.profiles.list.mockResolvedValue([profile()]);
    apiMock.session.mockResolvedValue({ actor: { roles: [] } });
    apiMock.status.mockResolvedValue({ auth_keys: [] });

    renderWithQuery(<AdminAppsPage />);

    expect(
      await screen.findByRole("button", { name: "Select Fixture app" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("0 keys")).not.toBeInTheDocument();
    expect(screen.queryByText("Never")).not.toBeInTheDocument();
    expect(screen.getAllByText("…").length).toBeGreaterThan(0);

    apiMock.credentials.list.mockRejectedValue(new Error("credentials down"));
    const { queryClient } = renderWithQuery(<AdminAppsPage />);
    await waitFor(() =>
      expect(queryClient.getQueryState(["credentials"])?.status).toBe("error"),
    );
    expect(await screen.findAllByText("Unavailable")).not.toHaveLength(0);
    expect(screen.queryByText("0 keys")).not.toBeInTheDocument();
    expect(screen.queryByText("Never")).not.toBeInTheDocument();
  });

  it("consumes destructive rejection and keeps the dialog target mounted", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn().mockRejectedValue(new Error("nope"));
    const onOpenChange = vi.fn();
    const unhandled = vi.fn();
    window.addEventListener("unhandledrejection", unhandled);

    render(
      <ConfirmDialog
        open
        onOpenChange={onOpenChange}
        title="Disable app?"
        description="Fixture app stays targeted."
        confirmLabel="Disable app"
        onConfirm={onConfirm}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Disable app" }));
    await waitFor(() => expect(onConfirm).toHaveBeenCalledOnce());
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
    expect(screen.getByRole("dialog", { name: "Disable app?" })).toBeVisible();
    expect(unhandled).not.toHaveBeenCalled();
    window.removeEventListener("unhandledrejection", unhandled);
  });

  it("reconciles Test Access selection when available profiles are replaced", async () => {
    const user = userEvent.setup();
    const profileA = profile({ id: "profile-a", name: "Profile A" });
    const profileB = profile({ id: "profile-b", name: "Profile B" });
    const profileC = profile({ id: "profile-c", name: "Profile C" });
    apiMock.policy.simulate.mockImplementation(({ profile: profileId }) =>
      Promise.resolve(allowedResult(profileId)),
    );

    apiMock.profiles.list.mockResolvedValue([profileA, profileB]);
    apiMock.apps.list.mockResolvedValue([]);
    apiMock.runtimeProfileMappings.list.mockResolvedValue([]);
    const view = renderWithQuery(<AdminProfilesPage />);

    await user.click(
      await screen.findByRole("button", { name: "Test access" }),
    );

    await user.selectOptions(screen.getByLabelText("Profile"), "profile-b");
    await user.click(screen.getByRole("button", { name: "Run test" }));
    expect(apiMock.policy.simulate).toHaveBeenLastCalledWith(
      expect.objectContaining({ profile: "profile-b" }),
    );
    expect(await screen.findByText("profile:profile-b")).toBeVisible();

    apiMock.profiles.list.mockResolvedValue([profileC]);
    await view.queryClient.refetchQueries({ queryKey: ["profiles"] });

    await waitFor(() =>
      expect(screen.getByLabelText("Profile")).toHaveValue("profile-c"),
    );
    expect(screen.queryByText("profile:profile-b")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Run test" }));
    expect(apiMock.policy.simulate).toHaveBeenLastCalledWith(
      expect.objectContaining({ profile: "profile-c" }),
    );
  });

  it("focuses profile form errors after a failed mutation", async () => {
    const user = userEvent.setup();
    apiMock.profiles.create.mockRejectedValue(new Error("server refused"));
    apiMock.profiles.list.mockResolvedValue([profile()]);
    apiMock.apps.list.mockResolvedValue([]);
    apiMock.runtimeProfileMappings.list.mockResolvedValue([]);

    renderWithQuery(<AdminProfilesPage />);

    await user.click(
      await screen.findByRole("button", { name: "Create profile" }),
    );
    await user.type(screen.getByLabelText(/Name/), "Blocked profile");
    await user.click(screen.getByRole("button", { name: "Create profile" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Something went wrong");
    expect(alert).toHaveFocus();
  });

  it("submits a data-discovered policy ID without a React catalog entry", async () => {
    const user = userEvent.setup();
    apiMock.profiles.create.mockResolvedValue(profile({ id: "extension-profile" }));
    apiMock.profiles.list.mockResolvedValue([]);
    apiMock.apps.list.mockResolvedValue([]);
    apiMock.runtimeProfileMappings.list.mockResolvedValue([]);

    renderWithQuery(<AdminProfilesPage />);
    await user.click(await screen.findByRole("button", { name: "Create profile" }));
    await user.type(screen.getByLabelText(/Name/), "Extension profile");
    await user.click(screen.getByRole("button", { name: /Customize access/ }));
    await user.click(screen.getAllByLabelText("Restrict")[0]);
    await user.click(screen.getByText("extension-api"));
    await user.click(screen.getByRole("button", { name: "Create profile" }));

    await waitFor(() => expect(apiMock.profiles.create).toHaveBeenCalledOnce());
    expect(apiMock.profiles.create).toHaveBeenCalledWith(expect.objectContaining({
      profile: expect.objectContaining({ protocols: ["extension-api"] }),
    }));
  });

  it("selects an app row from the keyboard with noninteractive row content", async () => {
    const user = userEvent.setup();
    apiMock.apps.list.mockResolvedValue([app()]);
    apiMock.users.list.mockResolvedValue([]);
    apiMock.credentials.list.mockResolvedValue([]);
    apiMock.profiles.list.mockResolvedValue([profile()]);
    apiMock.session.mockResolvedValue({ actor: { roles: [] } });
    apiMock.status.mockResolvedValue({ auth_keys: [] });

    renderWithQuery(<AdminAppsPage />);

    const select = await screen.findByRole("button", {
      name: "Select Fixture app",
    });
    const row = select.closest(".vf-access-row");
    expect(row).toBeNull();

    select.focus();
    await user.keyboard("{Enter}");
    expect(screen.getByText("Linked API keys")).toBeVisible();
  });
});
