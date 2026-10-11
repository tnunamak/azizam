import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api, adminFetch, ApiError, type Profile } from "@/lib/api";
import { ViewerContext } from "@/components/shell/session";
import PeoplePage from "@/app/people/page";
import SettingsPage from "@/app/settings/page";
import { PlanEditor } from "@/app/people/plans";

vi.mock("@/lib/api", async (original) => ({
  ...(await original<typeof import("@/lib/api")>()),
  adminFetch: vi.fn(),
}));
const plan: Profile = {
  id: "local",
  name: "Local plan",
  description: null,
  created_at: 1,
  protocols: [],
  workflows: [],
  status_visibility: "full",
  profile: {},
};
const person = {
  id: "morgan",
  name: "Morgan",
  email: "morgan@example.test",
  profile_id: "local",
  plan_name: "Local plan",
  disabled: false,
  last_active: null,
  requests_this_month: null,
};
const invite = {
  id: "sam",
  email: "sam@example.test",
  profile_id: "local",
  plan_name: "Local plan",
  expires_at: Date.now() / 1000 + 86400,
  created_at: 1,
  used_at: null,
  revoked_at: null,
};
const oidc = {
  enabled: false,
  issuer: "https://id.example.test",
  client_id: "client",
  redirect_uri: "",
  scopes: ["openid"],
  admin_emails: [],
  admin_groups: [],
  admin_roles: [],
};
const access = { self_service_apps: true, auto_create_personal_app: false };
let responses: Record<string, unknown>;
let client: QueryClient;
function mount(node: React.ReactNode, role = "owner") {
  client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ViewerContext.Provider
        value={{
          authenticated: true,
          name: "Viewer",
          email: null,
          role,
          admin: true,
        }}
      >
        {node}
      </ViewerContext.Provider>
    </QueryClientProvider>,
  );
}
async function edit(name: string) {
  await userEvent.click(
    await screen.findByRole("button", { name: `Edit ${name}` }),
  );
}
beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  HTMLElement.prototype.hasPointerCapture = () => false;
  HTMLElement.prototype.setPointerCapture = () => {};
  HTMLElement.prototype.releasePointerCapture = () => {};
  HTMLElement.prototype.scrollIntoView = () => {};

  responses = {
    "/admin/api/people": { people: [person], profiles: [plan] },
    "/admin/api/invites": { invites: [invite], profiles: [plan] },
    "/admin/api/settings/instance": {
      instance: {
        name: "Gateway",
        base_url: "https://public.example.test",
        theme_default: "system",
      },
      revision: "7",
    },
    "/admin/api/oidc-config": { oidc_config: oidc, revision: "7" },
    "/admin/api/operator/sign-in": {
      mode: "built-in",
      revision: "7",
      built_in: true,
      oidc: false,
    },
    "/admin/api/settings/defaults": {
      defaults: { profile_id: "local" },
      aliases: {},
    },
    "/admin/api/operator/overrides": {
      overrides: { revisions: { media_job_admission_overrides: "7" } },
    },
    "/admin/api/extensions": { extension_packages: { packages: {} } },
    "/admin/api/settings/receipts": { retention_days: null, revision: "0" },
    "/admin/api/settings/prompt-memory": {
      prompt_memory: {
        default_remember: true,
        never_keep: false,
        retention_cap: "never",
      },
      revision: "7",
    },
    "/admin/api/people/morgan/reset": {
      url: "/ui/sign-in/#reset=fixture",
      expires_at: invite.expires_at,
    },
    "/admin/api/invites/sam/link": { url: "/ui/invite/#token=fixture" },
  };
  vi.mocked(adminFetch).mockImplementation(async (path) => {
    const result = responses[path];
    if (result instanceof Error) throw result;
    return result as never;
  });
  vi.spyOn(api.profiles, "list").mockResolvedValue([plan]);
  vi.spyOn(api.userAccessDefaults, "get").mockResolvedValue(access);
  vi.spyOn(api.userAccessDefaults, "update").mockImplementation(
    async (data) => ({ ...access, ...data }),
  );
  vi.spyOn(api.policyEditor, "get").mockResolvedValue({
    profile: {
      create: { profile: {} },
      choices: Object.fromEntries(
        [
          "intents",
          "exposure",
          "models",
          "protocols",
          "workflows",
          "trust_tiers",
          "resource_tiers",
          "tags",
        ].map((key) => [key, []]),
      ),
    },
  } as never);
});
afterEach(() => {
  cleanup();
  client?.clear();
  vi.unstubAllGlobals();
});

describe("People and Settings red-team regressions", () => {
  it("disables invitations and hides person and invite actions for administrators", async () => {
    mount(<PeoplePage />, "admin");
    await screen.findByText("Morgan");
    expect(
      screen.getByRole("button", { name: "Invite someone" }),
    ).toBeDisabled();
    expect(
      screen.queryByRole("button", { name: "Actions for Morgan" }),
    ).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Invites" }));
    expect(
      screen.queryByRole("button", { name: /Actions for invitation/ }),
    ).not.toBeInTheDocument();
  });
  it.each(["People", "Invites"])(
    "shows an owner denial without retry for %s",
    async (tab) => {
      responses[tab === "People" ? "/admin/api/people" : "/admin/api/invites"] =
        new ApiError("owner_required", "Owner required", 403);
      mount(<PeoplePage />);
      if (tab === "Invites")
        await userEvent.click(screen.getByRole("tab", { name: tab }));
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "Only the owner can change this.",
      );
      expect(
        screen.queryByRole("button", { name: "Try again" }),
      ).not.toBeInTheDocument();
    },
  );
  it("preserves transient list-load headings and retry actions", async () => {
    responses["/admin/api/people"] = new ApiError(
      "unavailable",
      "Unavailable",
      503,
    );
    responses["/admin/api/invites"] = new ApiError(
      "unavailable",
      "Unavailable",
      503,
    );
    mount(<PeoplePage />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn’t load this",
    );
    expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
    await userEvent.click(screen.getByRole("tab", { name: "Invites" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn’t load this");
    expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
  });
  it("shows owner-only states without retry and keeps administrator app defaults available", async () => {
    for (const path of [
      "/admin/api/settings/instance",
      "/admin/api/settings/defaults",
      "/admin/api/operator/sign-in",
      "/admin/api/settings/prompt-memory",
    ])
      responses[path] = new ApiError("owner_required", "Owner required", 403);
    mount(<SettingsPage />, "admin");
    await screen.findAllByText("Only the owner can change this.");
    expect(
      screen.queryByRole("button", { name: "Try again" }),
    ).not.toBeInTheDocument();
    expect(
      await screen.findByRole("switch", {
        name: "Let people create their own apps",
      }),
    ).toBeEnabled();
    expect(
      screen.queryByRole("button", { name: "Save sign-in" }),
    ).not.toBeInTheDocument();
  });
  it("hides owner-only settings forms even when administrator queries contain cached success data", async () => {
    mount(<SettingsPage />, "admin");
    await edit("Sign-in");
    expect(
      screen.queryByRole("button", { name: "Save sign-in choice" }),
    ).not.toBeInTheDocument();
    await edit("Instance");
    expect(
      screen.queryByRole("button", { name: "Save instance" }),
    ).not.toBeInTheDocument();
    await screen.findByRole("region", { name: "Defaults" });
    expect(
      screen.queryByRole("button", { name: "Save default plan" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Save app defaults" }),
    ).toBeEnabled();
  });
  it("waits for the configured public base URL before allowing a relative link to be copied", async () => {
    let resolveInstance!: (value: unknown) => void;
    const pending = new Promise((resolve) => {
      resolveInstance = resolve;
    });
    vi.mocked(adminFetch).mockImplementation(async (path) =>
      path === "/admin/api/settings/instance"
        ? ((await pending) as never)
        : (responses[path] as never),
    );
    mount(<PeoplePage />);
    await userEvent.click(
      await screen.findByRole("button", { name: "Actions for Morgan" }),
    );
    await userEvent.click(
      screen.getByRole("menuitem", { name: "Create password reset link" }),
    );
    await screen.findByRole("dialog");
    expect(screen.getByRole("textbox", { name: "Link" })).toHaveValue("");
    await act(async () => {
      resolveInstance(responses["/admin/api/settings/instance"]);
    });
    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: "Link" })).toHaveValue(
        "https://public.example.test/ui/sign-in/#reset=fixture",
      ),
    );
  });
  it("names independently loading sign-in forms for assistive technology", async () => {
    let resolveSignIn!: (value: unknown) => void;
    const pending = new Promise((resolve) => {
      resolveSignIn = resolve;
    });
    vi.mocked(adminFetch).mockImplementation(async (path) =>
      path === "/admin/api/operator/sign-in"
        ? ((await pending) as never)
        : (responses[path] as never),
    );
    mount(<SettingsPage />);
    expect(
      await screen.findByRole("status", { name: "Loading sign-in choices" }),
    ).toHaveAttribute("aria-busy", "true");
    expect(
      screen.getAllByRole("status", { name: "Loading sign-in choices" }),
    ).toHaveLength(1);
    await act(async () => {
      resolveSignIn(responses["/admin/api/operator/sign-in"]);
    });
    await waitFor(() =>
      expect(
        screen.queryByRole("status", { name: "Loading sign-in choices" }),
      ).not.toBeInTheDocument(),
    );
  });
  it("explains email sign-in exceptions and first-owner bootstrap", async () => {
    mount(<SettingsPage />);
    await edit("Sign-in");
    expect(screen.getByLabelText("Sign-in email exceptions")).toHaveAttribute(
      "aria-describedby",
      "oidc-admin_emails-help",
    );
    expect(
      screen.getByText(/Once any grant exists, this list grants no role/),
    ).toHaveTextContent("Use Admin groups or Admin roles");
    expect(screen.queryByLabelText("Admin emails")).not.toBeInTheDocument();
  });
  it("preserves identity-provider drafts when sign-in choice saves and refetches", async () => {
    mount(<SettingsPage />);
    await edit("Sign-in");
    await userEvent.clear(screen.getByLabelText("Client ID"));
    await userEvent.type(screen.getByLabelText("Client ID"), "unsaved-client");
    await userEvent.click(
      screen.getByRole("button", { name: "Save sign-in choice" }),
    );
    await act(async () => {
      client.setQueryData(["settings", "oidc"], {
        oidc_config: { ...oidc, client_id: "external-client", enabled: true },
        revision: "8",
      });
    });
    expect(
      screen.queryByText(/Identity provider enabled/),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText("Client ID")).toHaveValue("unsaved-client");
    expect(
      screen.getByRole("switch", { name: "Enable identity provider" }),
    ).not.toBeChecked();
  });
  it("preserves a dirty sign-in choice when the provider saves", async () => {
    mount(<SettingsPage />);
    await edit("Sign-in");
    await userEvent.click(screen.getByLabelText("Available sign-in methods"));
    await userEvent.click(
      screen.getByRole("option", { name: "Built-in accounts and OIDC" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Save sign-in" }));
    await waitFor(() =>
      expect(adminFetch).toHaveBeenCalledWith(
        "/admin/api/oidc-config",
        expect.objectContaining({ method: "PUT" }),
      ),
    );
    expect(
      screen.getByLabelText("Available sign-in methods"),
    ).toHaveTextContent("Built-in accounts and OIDC");
  });
  it("preserves app-option and default-plan drafts across independent saves", async () => {
    mount(<SettingsPage />);
    await screen.findByRole("region", { name: "Defaults" });
    await userEvent.click(
      await screen.findByRole("switch", {
        name: "Let people create their own apps",
      }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Save default plan" }),
    );
    await act(async () => {
      await client.invalidateQueries({ queryKey: ["settings", "defaults"] });
    });
    expect(
      await screen.findByRole("switch", {
        name: "Let people create their own apps",
      }),
    ).not.toBeChecked();
    await userEvent.click(screen.getByLabelText("Default access plan"));
    await userEvent.click(
      screen.getByRole("option", { name: "None — built-in local access" }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Save app defaults" }),
    );
    await act(async () => {
      await client.invalidateQueries({ queryKey: ["settings", "defaults"] });
    });
    expect(screen.getByLabelText("Default access plan")).toHaveTextContent(
      "None — built-in local access",
    );
  });
  it("updates clean forms on refresh while preserving dirty instance drafts", async () => {
    mount(<SettingsPage />);
    await edit("Instance");
    await act(async () => {
      client.setQueryData(["settings", "instance"], {
        instance: { name: "Refreshed", base_url: "", theme_default: "dark" },
        revision: "8",
      });
    });
    await waitFor(() =>
      expect(screen.getByLabelText("Instance name")).toHaveValue("Refreshed"),
    );
    await userEvent.type(screen.getByLabelText("Instance name"), " draft");
    await act(async () => {
      client.setQueryData(["settings", "instance"], {
        instance: { name: "Another", base_url: "", theme_default: "light" },
        revision: "9",
      });
    });
    expect(screen.getByLabelText("Instance name")).toHaveValue(
      "Refreshed draft",
    );
  });
  it.each([
    '{"models":"invalid"}',
    '{"allow":{"intents":false}}',
    '{"models":[4]}',
    '{"allow":null}',
    '{"fallback":{"cloud_allowed":"yes"}}',
    "[]",
  ])(
    "rejects invalid policy shape %s without crashing or losing the draft",
    async (raw) => {
      mount(<PlanEditor plan={plan} close={vi.fn()} saved={vi.fn()} />);
      await screen.findByLabelText("Policy JSON");
      await userEvent.click(screen.getByText("Advanced", { exact: true }));
      await userEvent.click(
        screen.getByText("Complete policy document", { exact: true }),
      );
      const json = screen.getByLabelText("Policy JSON");
      fireEvent.change(json, { target: { value: raw } });
      expect(json).toHaveValue(raw);
      expect(screen.getByRole("alert")).toBeVisible();
      expect(
        screen.getByRole("button", { name: "Save access plan" }),
      ).toBeDisabled();
      fireEvent.change(json, {
        target: { value: '{"models":[],"operator_extension":{"keep":true}}' },
      });
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Save access plan" }),
      ).toBeEnabled();
    },
  );
  it.each([
    ["Restrict capabilities", { allow: { intents: [] } }],
    ["Restrict models", { models: [] }],
    ["Allow cloud fallback", { fallback: { cloud_allowed: true } }],
  ])(
    "recovers from invalid JSON when changing %s",
    async (label, expectedPolicy) => {
      mount(<PlanEditor plan={plan} close={vi.fn()} saved={vi.fn()} />);
      const json = await screen.findByLabelText("Policy JSON");
      await userEvent.click(screen.getByText("Advanced", { exact: true }));
      await userEvent.click(
        screen.getByText("Complete policy document", { exact: true }),
      );
      const save = screen.getByRole("button", { name: "Save access plan" });
      fireEvent.change(json, { target: { value: "{" } });
      expect(save).toBeDisabled();

      await userEvent.click(screen.getByLabelText(label as string));

      expect(json).toHaveValue(JSON.stringify(expectedPolicy, null, 2));
      expect(json).toHaveAttribute("aria-invalid", "false");
      expect(json).not.toHaveAttribute("aria-describedby");
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(screen.queryByText(/Save is blocked/)).not.toBeInTheDocument();
      expect(save).toBeEnabled();
    },
  );
  it("discards invalid JSON back to the latest valid draft without losing saved fields", async () => {
    mount(<PlanEditor plan={plan} close={vi.fn()} saved={vi.fn()} />);
    const json = await screen.findByLabelText("Policy JSON");
    await userEvent.click(screen.getByText("Advanced", { exact: true }));
    await userEvent.click(
      screen.getByText("Complete policy document", { exact: true }),
    );
    const validDraft = {
      models: [],
      operator_extension: { keep: true },
    };
    fireEvent.change(json, {
      target: { value: JSON.stringify(validDraft) },
    });
    const save = screen.getByRole("button", { name: "Save access plan" });

    for (const invalidDraft of ["{", '{"models":"invalid"}']) {
      fireEvent.change(json, { target: { value: invalidDraft } });
      expect(save).toBeDisabled();
      await userEvent.click(
        screen.getByRole("button", { name: "Discard invalid JSON changes" }),
      );

      expect(json).toHaveValue(JSON.stringify(validDraft, null, 2));
      expect(json).toHaveAttribute("aria-invalid", "false");
      expect(json).not.toHaveAttribute("aria-describedby");
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Discard invalid JSON changes" }),
      ).not.toBeInTheDocument();
      expect(save).toBeEnabled();
    }
  });
  it("keeps the Save-block reason visible when Advanced is closed and links JSON errors", async () => {
    mount(<PlanEditor plan={plan} close={vi.fn()} saved={vi.fn()} />);
    const json = await screen.findByLabelText("Policy JSON");
    const advanced = screen.getByText("Advanced", { exact: true });
    await userEvent.click(advanced);
    await userEvent.click(
      screen.getByText("Complete policy document", { exact: true }),
    );
    fireEvent.change(json, { target: { value: "{" } });
    expect(json).toHaveAttribute("aria-invalid", "true");
    expect(json).toHaveAttribute("aria-describedby", "policy-json-error");
    expect(screen.getByRole("alert")).toHaveAttribute(
      "id",
      "policy-json-error",
    );
    expect(json).toHaveAccessibleDescription(
      "Enter a valid JSON object before saving.",
    );

    await userEvent.click(advanced);

    const save = screen.getByRole("button", { name: "Save access plan" });
    const reason =
      "Save is blocked. Fix or discard invalid JSON changes in Advanced.";
    expect(json).not.toBeVisible();
    expect(save).toBeDisabled();
    expect(screen.getByText(reason)).toBeVisible();
    expect(save).toHaveAccessibleDescription(reason);
    expect(save).toHaveAttribute("aria-describedby", "plan-save-error");

    await userEvent.click(advanced);
    expect(json).toHaveValue("{");
    fireEvent.change(json, { target: { value: "{}" } });
    expect(screen.queryByText(reason)).not.toBeInTheDocument();
    expect(save).not.toHaveAttribute("aria-describedby");
    expect(save).toBeEnabled();
  });
  it.each([false, true])(
    "names the person in %s enable/disable confirmations",
    async (disabled) => {
      responses["/admin/api/people"] = {
        people: [{ ...person, disabled }],
        profiles: [plan],
      };
      mount(<PeoplePage />);
      await userEvent.click(
        await screen.findByRole("button", { name: "Actions for Morgan" }),
      );
      const action = disabled ? "Enable access" : "Disable access";
      await userEvent.click(screen.getByRole("menuitem", { name: action }));
      const dialog = screen.getByRole("dialog");
      expect(dialog).toHaveTextContent("Morgan (morgan@example.test)");
      expect(
        within(dialog).getByRole("button", { name: action }),
      ).toBeEnabled();
    },
  );
  it("names invitation recipients in revoke confirmations", async () => {
    mount(<PeoplePage />);
    await userEvent.click(await screen.findByRole("tab", { name: "Invites" }));
    await userEvent.click(
      await screen.findByRole("button", {
        name: "Actions for invitation to sam@example.test",
      }),
    );
    await userEvent.click(
      screen.getByRole("menuitem", { name: "Revoke invitation" }),
    );
    expect(screen.getByRole("dialog")).toHaveTextContent("sam@example.test");
    expect(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: "Revoke invitation",
      }),
    ).toBeEnabled();
  });
  it.each(["https://public.example.test", ""])(
    "resolves reset links with public URL %s and restores focus",
    async (base) => {
      responses["/admin/api/settings/instance"] = {
        instance: { name: "Gateway", base_url: base, theme_default: "system" },
      };
      mount(<PeoplePage />);
      const trigger = await screen.findByRole("button", {
        name: "Actions for Morgan",
      });
      await userEvent.click(trigger);
      await userEvent.click(
        screen.getByRole("menuitem", { name: "Create password reset link" }),
      );
      await screen.findByRole("dialog");
      expect(screen.getByRole("textbox", { name: "Link" })).toHaveValue(
        `${base || window.location.origin}/ui/sign-in/#reset=fixture`,
      );
      await userEvent.click(
        screen.getByRole("button", { name: "I’ve saved the link" }),
      );
      await waitFor(() => expect(trigger).toHaveFocus());
    },
  );
  it("uses absolute server invitation links without waiting for public settings", async () => {
    vi.mocked(adminFetch).mockImplementation(async (path) => {
      if (path === "/admin/api/settings/instance")
        return await new Promise(() => {});
      if (path === "/admin/api/invites/sam/link")
        return {
          url: "https://server.example.test/ui/invite/#token=fixture",
        } as never;
      return responses[path] as never;
    });
    mount(<PeoplePage />);
    await userEvent.click(screen.getByRole("tab", { name: "Invites" }));
    await userEvent.click(
      await screen.findByRole("button", {
        name: "Actions for invitation to sam@example.test",
      }),
    );
    await userEvent.click(
      screen.getByRole("menuitem", { name: "Copy invite link" }),
    );
    await screen.findByRole("dialog");
    expect(screen.getByRole("textbox", { name: "Link" })).toHaveValue(
      "https://server.example.test/ui/invite/#token=fixture",
    );
    expect(screen.getByRole("button", { name: "Copy link" })).toBeEnabled();
  });
  it("keeps relative links unavailable if the public URL cannot be loaded, then recovers", async () => {
    responses["/admin/api/settings/instance"] = new ApiError(
      "unavailable",
      "Unavailable",
      503,
    );
    mount(<PeoplePage />);
    await userEvent.click(
      await screen.findByRole("button", { name: "Actions for Morgan" }),
    );
    await userEvent.click(
      screen.getByRole("menuitem", { name: "Create password reset link" }),
    );
    await screen.findByRole("dialog");
    expect(
      screen.queryByRole("textbox", { name: "Link" }),
    ).not.toBeInTheDocument();
    responses["/admin/api/settings/instance"] = {
      instance: { base_url: "https://public.example.test" },
    };
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("textbox", { name: "Link" })).toHaveValue(
      "https://public.example.test/ui/sign-in/#reset=fixture",
    );
  });
  it("draws Settings on the settings template (craft PR 2)", async () => {
    const { container } = mount(<SettingsPage />);
    await screen.findByText("Gateway");
    const instance = screen.getByRole("region", { name: "Instance" });
    // No jump links while every section fits on the page (ban 22).
    expect(
      screen.queryByRole("navigation", { name: "Settings sections" }),
    ).toBeNull();
    // A 720 column; the header 24 above sections that are 32 apart.
    expect(container.firstElementChild).toHaveClass("page-read");
    expect(instance.parentElement).toHaveClass("page-stack");
    expect(
      within(instance).getByRole("heading", { name: "Instance" }),
    ).toHaveClass("text-heading");
    const row = instance.querySelector("[data-slot=setting-row]")!;
    expect(row).toHaveTextContent("Gateway");
    expect(row).toHaveTextContent(
      "https://public.example.test · Follows device theme",
    );
    const edit = within(instance).getByRole("button", {
      name: "Edit Instance",
    });
    expect(edit).toHaveClass("h-8");
    expect(within(instance).queryByLabelText("Instance name")).toBeNull();
    await userEvent.click(edit);
    // The editor replaces its summary; Close follows the fields.
    expect(
      within(instance).getByLabelText("Instance name"),
    ).toBeInTheDocument();
    expect(
      within(instance).getByRole("button", { name: "Close Instance" }),
    ).toBeInTheDocument();
    expect(
      container.querySelectorAll("[data-slot=settings-section]"),
    ).toHaveLength(9);
    const receipts = screen.getByRole("region", { name: "Receipts" });
    expect(
      within(receipts).getByRole("combobox", { name: "Keep receipts" }),
    ).toHaveTextContent("Forever");
    expect(instance.querySelector("[data-slot=setting-row]")).toBeNull();
    await waitFor(() =>
      expect(within(instance).getByLabelText("Instance name")).toHaveFocus(),
    );
    await userEvent.click(
      within(instance).getByRole("button", { name: "Close Instance" }),
    );
    await waitFor(() =>
      expect(
        within(instance).getByRole("button", { name: "Edit Instance" }),
      ).toHaveFocus(),
    );
  });
  it("wraps long instance names and URLs in the settings summary", async () => {
    responses["/admin/api/settings/instance"] = {
      instance: {
        name: "n".repeat(300),
        base_url: "https://example.test/" + "p".repeat(300),
        theme_default: "system",
      },
    };
    mount(<SettingsPage />);
    // The setting row's label wraps anywhere (one line on a phone); its one
    // detail line truncates.
    const name = await screen.findByText("n".repeat(300));
    expect(name).toHaveClass("sm:[overflow-wrap:anywhere]", "max-sm:truncate");
    expect(name.parentElement).toHaveClass("min-w-0");
    expect(screen.getByText(/https:\/\/example.test\/ppp/)).toHaveClass(
      "truncate",
    );
  });
  it("returns focus to the workspace when a removed row no longer exists", async () => {
    mount(<PeoplePage />);
    await userEvent.click(
      await screen.findByRole("button", { name: "Actions for Morgan" }),
    );
    await userEvent.click(
      screen.getByRole("menuitem", { name: "Disable access" }),
    );
    await act(async () => {
      client.setQueryData(["people", "list"], { people: [], profiles: [plan] });
    });
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() =>
      expect(screen.getByLabelText("People workspace")).toHaveFocus(),
    );
  });
  it("wraps long people names and access-plan labels", async () => {
    responses["/admin/api/people"] = {
      people: [
        { ...person, name: "n".repeat(300), plan_name: "p".repeat(300) },
      ],
      profiles: [plan],
    };
    mount(<PeoplePage />);
    // Craft PR 3: a person row is two lines; a long name or plan is cut with
    // an ellipsis inside its column and keeps its full text in its title.
    const name = await screen.findByText("n".repeat(300));
    expect(name.closest(".truncate")).not.toBeNull();
    expect(name).toHaveAttribute("title", "n".repeat(300));
    // One copy is the desktop column, one joins the phone's second line.
    for (const planName of screen.getAllByText("p".repeat(300))) {
      expect(planName.closest("[title]")).toHaveAttribute(
        "title",
        "p".repeat(300),
      );
      expect(planName.closest(".truncate")).not.toBeNull();
    }
  });

  describe("Invite action without access plans", () => {
    const withoutPlans = (list: () => Promise<Profile[]>) => {
      responses["/admin/api/people"] = { people: [person], profiles: [] };
      responses["/admin/api/invites"] = { invites: [], profiles: [] };
      vi.mocked(api.profiles.list).mockImplementation(list);
    };
    const invite_ = () =>
      screen.getByRole("button", { name: "Invite someone" });

    it("says plans are loading instead of leaving Invite silently disabled", async () => {
      withoutPlans(() => new Promise(() => {}));
      mount(<PeoplePage />);
      await screen.findByText("Morgan");
      expect(screen.getByText("Loading access plans")).toBeInTheDocument();
      expect(invite_()).toBeDisabled();
      await userEvent.click(screen.getByRole("tab", { name: "Invites" }));
      expect(screen.getByText("Loading access plans")).toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Create an access plan" }),
      ).not.toBeInTheDocument();
    });
    it("names a failed plan load and retries it from the People page", async () => {
      withoutPlans(async () => {
        throw new ApiError("unavailable", "Unavailable", 503);
      });
      mount(<PeoplePage />);
      await screen.findByText("Morgan");
      expect(
        await screen.findByText("Access plans couldn’t be loaded"),
      ).toBeInTheDocument();
      expect(invite_()).toBeDisabled();
      vi.mocked(api.profiles.list).mockResolvedValue([plan]);
      await userEvent.click(screen.getByRole("button", { name: "Try again" }));
      await waitFor(() => expect(invite_()).toBeEnabled());
      expect(
        screen.queryByText("Access plans couldn’t be loaded"),
      ).not.toBeInTheDocument();
    });
    it("offers the same retry in the Invites empty state", async () => {
      withoutPlans(async () => {
        throw new ApiError("unavailable", "Unavailable", 503);
      });
      mount(<PeoplePage />);
      await userEvent.click(
        await screen.findByRole("tab", { name: "Invites" }),
      );
      expect(
        await screen.findByText("Access plans couldn’t be loaded"),
      ).toBeInTheDocument();
      const calls = vi.mocked(api.profiles.list).mock.calls.length;
      vi.mocked(api.profiles.list).mockResolvedValue([plan]);
      await userEvent.click(screen.getByRole("button", { name: "Try again" }));
      await waitFor(() =>
        expect(vi.mocked(api.profiles.list).mock.calls.length).toBeGreaterThan(
          calls,
        ),
      );
      expect(await screen.findByText("No invitations yet")).toBeInTheDocument();
      for (const button of screen.getAllByRole("button", {
        name: "Invite someone",
      }))
        expect(button).toBeEnabled();
    });
    it("sends the owner to create a plan from the People page", async () => {
      withoutPlans(async () => []);
      mount(<PeoplePage />);
      await screen.findByText("Morgan");
      expect(
        await screen.findByText(
          "Create an access plan before inviting someone",
        ),
      ).toBeInTheDocument();
      expect(invite_()).toBeDisabled();
      await userEvent.click(
        screen.getByRole("button", { name: "Create an access plan" }),
      );
      expect(await screen.findByRole("dialog")).toBeInTheDocument();
      expect(
        screen.getByRole("tab", { name: "Access plans", hidden: true }),
      ).toHaveAttribute("data-state", "active");
    });
    it("sends the owner to create a plan from the Invites empty state", async () => {
      withoutPlans(async () => []);
      mount(<PeoplePage />);
      await userEvent.click(
        await screen.findByRole("tab", { name: "Invites" }),
      );
      expect(
        await screen.findByText(
          "Create an access plan before inviting someone",
        ),
      ).toBeInTheDocument();
      expect(invite_()).toBeDisabled();
      await userEvent.click(
        screen.getByRole("button", { name: "Create an access plan" }),
      );
      expect(await screen.findByRole("dialog")).toBeInTheDocument();
      expect(
        screen.getByRole("tab", { name: "Access plans", hidden: true }),
      ).toHaveAttribute("data-state", "active");
    });
    it("does not show plan recovery to someone who cannot invite", async () => {
      withoutPlans(async () => []);
      mount(<PeoplePage />, "admin");
      await screen.findByText("Morgan");
      expect(invite_()).toBeDisabled();
      expect(
        screen.queryByText("Create an access plan before inviting someone"),
      ).not.toBeInTheDocument();
    });
  });
});
