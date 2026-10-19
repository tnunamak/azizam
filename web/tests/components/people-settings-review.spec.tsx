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
import { api, adminFetch, ApiError, type AccessPlan } from "@/lib/api";
import { ViewerContext } from "@/components/shell/session";
import PeoplePage from "@/app/people/page";
import SettingsPage from "@/app/settings/page";
import { PlanEditor } from "@/app/people/plans";
import { signInKeys } from "@/lib/signin-api";
import { emptyPreview, liveSnapshot } from "@/app/settings/sign-in/test-support";

vi.mock("@/lib/api", async (original) => ({
  ...(await original<typeof import("@/lib/api")>()),
  adminFetch: vi.fn(),
}));
const plan: AccessPlan = {
  id: "local",
  name: "Local plan",
  created_at: 1,
  updated_at: 1,
  summary: "Everything on this gateway. No paid models.",
  plan: {
    name: "Local plan",
    can_use: { chat: { items: "all" } },
    direct_engine_access: { engines: [], operations: [] },
    paid_providers: [],
    provider_fallback: false,
    caps: { usd_per_request: null, usd_per_day: null, usd_per_month: null },
    can_load_models: true,
    receipt_detail: "full",
    media_metadata: "none",
  },
};
const person = {
  id: "morgan",
  name: "Morgan",
  email: "morgan@example.test",
  plan_id: "local",
  plan_name: "Local plan",
  disabled: false,
  last_active: null,
  requests_this_month: null,
};
const invite = {
  id: "sam",
  email: "sam@example.test",
  plan_id: "local",
  plan_name: "Local plan",
  expires_at: Date.now() / 1000 + 86400,
  created_at: 1,
  used_at: null,
  revoked_at: null,
};
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
    "/admin/api/people": { people: [person], access_plans: [plan] },
    "/admin/api/invites": { invites: [invite], access_plans: [plan], invite_access_plan: null },
    "/admin/api/settings/instance": {
      instance: {
        name: "Gateway",
        base_url: "https://public.example.test",
        theme_default: "system",
      },
      revision: "7",
    },
    "/admin/api/signin": liveSnapshot(),
    "/admin/api/signin/preview": emptyPreview(),
    "/admin/api/settings/defaults": {
      defaults: { invite_access_plan: null },
      aliases: {},
    },
    "/admin/api/operator/overrides": {
      overrides: { revisions: { media_job_admission_overrides: "7" } },
    },
    "/admin/api/extensions": { extension_packages: { packages: {} } },
    "/admin/api/operator/host-commands": { enabled: false, revision: "0" },
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
      url: "/sign-in/#reset=fixture",
      expires_at: invite.expires_at,
    },
    "/admin/api/invites/sam/link": { url: "/invite/#token=fixture" },
  };
  vi.mocked(adminFetch).mockImplementation(async (path) => {
    const result = responses[path];
    if (result instanceof Error) throw result;
    return result as never;
  });
  vi.spyOn(api.accessPlans, "list").mockResolvedValue([plan]);
  vi.spyOn(api.accessPlans, "editor").mockResolvedValue({
    schema_version: 2,
    capabilities: [{ id: "chat", label: "Chat", items: [] }],
    engines: [],
    paid_providers: [],
    receipt_detail: [{ id: "full", label: "Full", description: null }],
    media_metadata: [{ id: "none", label: "Strip", description: null }],
    new_plan: { ...plan.plan, name: "", can_use: {} },
  });
});
afterEach(() => {
  cleanup();
  client?.clear();
  vi.useRealTimers();
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
  it("shows owner-only states without retry to administrators", async () => {
    for (const path of [
      "/admin/api/settings/instance",
      "/admin/api/settings/defaults",
      "/admin/api/signin",
      "/admin/api/settings/prompt-memory",
    ])
      responses[path] = new ApiError("owner_required", "Owner required", 403);
    mount(<SettingsPage />, "admin");
    await screen.findAllByText("Only the owner can change this.");
    expect(
      screen.queryByRole("button", { name: "Try again" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("switch", { name: "Let people create their own apps" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Save changes" }),
    ).not.toBeInTheDocument();
  });
  it("hides owner-only settings forms even when administrator queries contain cached success data", async () => {
    responses["/admin/api/signin"] = liveSnapshot({
      viewer: { role: "admin", can_edit: false },
    });
    mount(<SettingsPage />, "admin");
    expect(
      await screen.findByText("Only the owner can change sign-in."),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Save changes" }),
    ).not.toBeInTheDocument();
    await edit("Instance");
    expect(
      screen.queryByRole("button", { name: "Save instance" }),
    ).not.toBeInTheDocument();
    await screen.findByRole("region", { name: "People and access" });
    expect(
      screen.queryByLabelText("Access plan for new invitations"),
    ).not.toBeInTheDocument();
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
        "https://public.example.test/sign-in/#reset=fixture",
      ),
    );
  });
  it("names the loading sign-in section for assistive technology", async () => {
    let resolveSignIn!: (value: unknown) => void;
    const pending = new Promise((resolve) => {
      resolveSignIn = resolve;
    });
    vi.mocked(adminFetch).mockImplementation(async (path) =>
      path === "/admin/api/signin"
        ? ((await pending) as never)
        : (responses[path] as never),
    );
    mount(<SettingsPage />);
    expect(
      await screen.findByRole("status", { name: "Loading sign-in" }),
    ).toHaveAttribute("aria-busy", "true");
    expect(
      screen.getAllByRole("status", { name: "Loading sign-in" }),
    ).toHaveLength(1);
    await act(async () => {
      resolveSignIn(responses["/admin/api/signin"]);
    });
    await waitFor(() =>
      expect(
        screen.queryByRole("status", { name: "Loading sign-in" }),
      ).not.toBeInTheDocument(),
    );
  });
  it("keeps a typed connection draft when the saved sign-in settings refresh", async () => {
    mount(<SettingsPage />);
    await userEvent.click(
      await screen.findByRole("button", { name: /Keycloak · id\.vivid\.fish/ }),
    );
    const clientId = screen.getByLabelText("Client ID");
    await userEvent.clear(clientId);
    await userEvent.type(clientId, "unsaved-client");
    const refreshed = liveSnapshot({ revision: "rev-2" });
    refreshed.provider.client_id = "external-client";
    await act(async () => {
      client.setQueryData(signInKeys.snapshot, refreshed);
    });
    expect(screen.getByLabelText("Client ID")).toHaveValue("unsaved-client");
  });
  it("adopts refreshed sign-in settings while the form is untouched", async () => {
    mount(<SettingsPage />);
    await screen.findByRole("radio", { name: /Keep them out unless I invite them/ });
    const refreshed = liveSnapshot({ revision: "rev-2" });
    refreshed.rules = { ...refreshed.rules, fallback: "open" };
    await act(async () => {
      client.setQueryData(signInKeys.snapshot, refreshed);
    });
    await waitFor(() =>
      expect(
        screen.getByRole("radio", { name: /Let them in, but with no access/ }),
      ).toBeChecked(),
    );
  });
  it("keeps a dirty invitation-plan draft across a refresh", async () => {
    mount(<SettingsPage />);
    await userEvent.click(await screen.findByLabelText("Access plan for new invitations"));
    await userEvent.click(screen.getByRole("option", { name: "Local plan" }));
    await act(async () => {
      await client.invalidateQueries({ queryKey: ["settings", "defaults"] });
    });
    expect(screen.getByLabelText("Access plan for new invitations")).toHaveTextContent(
      "Local plan",
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
  const openDocument = async () => {
    await screen.findByLabelText("Plan JSON");
    await userEvent.click(screen.getByText("Details", { exact: true }));
    await userEvent.click(
      screen.getByText("Complete plan document", { exact: true }),
    );
    return screen.getByLabelText("Plan JSON");
  };
  it.each(["[]", '{"name":"x"}', "null", "{"])(
    "rejects invalid plan document %s without crashing or losing the draft",
    async (raw) => {
      mount(<PlanEditor plan={plan} close={vi.fn()} saved={vi.fn()} />);
      const json = await openDocument();
      fireEvent.change(json, { target: { value: raw } });
      expect(json).toHaveValue(raw);
      expect(screen.getByRole("alert")).toBeVisible();
      expect(
        screen.getByRole("button", { name: "Save access plan" }),
      ).toBeDisabled();
      fireEvent.change(json, { target: { value: JSON.stringify(plan.plan) } });
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Save access plan" }),
      ).toBeEnabled();
    },
  );
  it.each([
    ["Chat", { can_use: {} }],
    ["May load models that aren't running", { can_load_models: false }],
    ["Use a paid provider when the engine is unavailable", { provider_fallback: true }],
  ])(
    "recovers from invalid JSON when changing %s",
    async (label, change) => {
      mount(<PlanEditor plan={plan} close={vi.fn()} saved={vi.fn()} />);
      const json = await openDocument();
      const save = screen.getByRole("button", { name: "Save access plan" });
      fireEvent.change(json, { target: { value: "{" } });
      expect(save).toBeDisabled();

      await userEvent.click(screen.getByRole("checkbox", { name: label as string }));

      expect(JSON.parse((json as HTMLTextAreaElement).value)).toEqual({
        ...plan.plan,
        ...(change as object),
      });
      expect(json).toHaveAttribute("aria-invalid", "false");
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(screen.queryByText(/Save is blocked/)).not.toBeInTheDocument();
      expect(save).toBeEnabled();
    },
  );
  it("discards invalid JSON back to the latest valid draft", async () => {
    mount(<PlanEditor plan={plan} close={vi.fn()} saved={vi.fn()} />);
    const json = await openDocument();
    const validDraft = { ...plan.plan, name: "Renamed" };
    fireEvent.change(json, { target: { value: JSON.stringify(validDraft) } });
    const save = screen.getByRole("button", { name: "Save access plan" });
    for (const invalidDraft of ["{", '{"name":"x"}']) {
      fireEvent.change(json, { target: { value: invalidDraft } });
      expect(save).toBeDisabled();
      await userEvent.click(
        screen.getByRole("button", { name: "Discard invalid JSON changes" }),
      );
      expect(JSON.parse((json as HTMLTextAreaElement).value)).toEqual(validDraft);
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(save).toBeEnabled();
    }
  });
  it("keeps the Save-block reason visible when Details is closed", async () => {
    mount(<PlanEditor plan={plan} close={vi.fn()} saved={vi.fn()} />);
    const json = await openDocument();
    fireEvent.change(json, { target: { value: "{" } });
    expect(json).toHaveAttribute("aria-describedby", "policy-json-error");
    await userEvent.click(screen.getByText("Details", { exact: true }));
    const save = screen.getByRole("button", { name: "Save access plan" });
    const reason = "Save is blocked. Fix or discard invalid JSON changes in Details.";
    expect(json).not.toBeVisible();
    expect(save).toBeDisabled();
    expect(screen.getByText(reason)).toBeVisible();
    expect(save).toHaveAccessibleDescription(reason);
  });
  it.each([false, true])(
    "names the person in %s enable/disable confirmations",
    async (disabled) => {
      responses["/admin/api/people"] = {
        people: [{ ...person, disabled }],
        access_plans: [plan],
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
    const trigger = await screen.findByRole("button", {
      name: "Actions for invitation to sam@example.test",
    });
    vi.stubGlobal("PointerEvent", MouseEvent);
    vi.useFakeTimers();
    // The dialog's focus trap can swallow the dropdown's return-focus attempt.
    // Remembering the opener must not depend on that intermediate focus event.
    const triggerFocus = vi.spyOn(trigger, "focus").mockImplementation(() => {});
    fireEvent.pointerDown(trigger.firstElementChild!, {
      button: 0,
      ctrlKey: false,
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    fireEvent.click(
      screen.getByRole("menuitem", { name: "Revoke invitation" }),
    );
    expect(screen.getByRole("dialog")).toHaveTextContent("sam@example.test");
    expect(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: "Revoke invitation",
      }),
    ).toBeEnabled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(triggerFocus).toHaveBeenCalled();
    triggerFocus.mockRestore();
    fireEvent.keyDown(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }),
      { key: "Escape" },
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(trigger).toHaveFocus();
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
        `${base || window.location.origin}/sign-in/#reset=fixture`,
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
          url: "https://server.example.test/invite/#token=fixture",
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
      "https://server.example.test/invite/#token=fixture",
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
      "https://public.example.test/sign-in/#reset=fixture",
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
      client.setQueryData(["people", "list"], { people: [], access_plans: [plan] });
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
      access_plans: [plan],
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
    const withoutPlans = (list: () => Promise<AccessPlan[]>) => {
      responses["/admin/api/people"] = { people: [person], access_plans: [] };
      responses["/admin/api/invites"] = { invites: [], access_plans: [], invite_access_plan: null };
      vi.mocked(api.accessPlans.list).mockImplementation(list);
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
      vi.mocked(api.accessPlans.list).mockResolvedValue([plan]);
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
      const calls = vi.mocked(api.accessPlans.list).mock.calls.length;
      vi.mocked(api.accessPlans.list).mockResolvedValue([plan]);
      await userEvent.click(screen.getByRole("button", { name: "Try again" }));
      await waitFor(() =>
        expect(vi.mocked(api.accessPlans.list).mock.calls.length).toBeGreaterThan(
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
