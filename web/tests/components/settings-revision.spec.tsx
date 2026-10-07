import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";
import SettingsPage from "@/app/settings/page";

const { adminFetch } = vi.hoisted(() => ({ adminFetch: vi.fn() }));
vi.mock("@/lib/api", async (original) => ({ ...(await original<typeof import("@/lib/api")>()), adminFetch }));
beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); });

describe("settings revision wiring", () => {
  it("sends the loaded instance revision and preserves the draft on conflict", async () => {
    let latestRevision = "7";
    adminFetch.mockImplementation(async (path, init) => {
      if (path === "/admin/api/settings/instance") {
        if (init?.method === "PUT") throw new ApiError("settings_conflict", "Conflict", 409);
        return { instance: { name: latestRevision === "7" ? "Original" : "Changed elsewhere", base_url: "", theme_default: "system" }, revision: latestRevision };
      }
      // Other settings cards exercise their normal error states.
      throw new Error("Fixture card unavailable");
    });
    const user = userEvent.setup();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(<QueryClientProvider client={queryClient}><SettingsPage /></QueryClientProvider>);
    await user.click(await screen.findByRole("button", { name: "Edit Instance" }));
    const field = screen.getByLabelText("Instance name");
    await user.clear(field);
    await user.type(field, "My unsaved draft");
    await user.click(screen.getByRole("button", { name: "Save instance" }));
    await waitFor(() => expect(screen.getByText("Settings changed elsewhere; reload to see the latest")).toBeVisible());
    latestRevision = "8";
    await queryClient.invalidateQueries({ queryKey: ["settings", "instance"] });
    expect(field).toBeInTheDocument();
    expect(field).toHaveValue("My unsaved draft");
    await user.click(screen.getByRole("button", { name: "Save instance" }));
    await waitFor(() => expect(adminFetch.mock.calls.filter(([, init]) => init?.method === "PUT")).toHaveLength(2));
    expect(adminFetch.mock.calls.filter(([, init]) => init?.method === "PUT").every(([, init]) => init.headers["If-Match"] === "7")).toBe(true);
    expect(adminFetch).toHaveBeenCalledWith("/admin/api/settings/instance", expect.objectContaining({ method: "PUT", headers: { "If-Match": "7" }, body: expect.objectContaining({ name: "My unsaved draft" }) }));
  });
  it.each([
    { section: "Sign-in", save: "Save sign-in", field: "Issuer URL", path: "/admin/api/oidc-config", draft: "https://draft.example.test" },
    { section: "Media admission", save: "Save media admission", field: "Maximum queued jobs", path: "/admin/api/operator/media-job-admission", draft: "12" },
  ])("sends the loaded $section revision and keeps a conflicting draft", async ({ section, save, field, path, draft }) => {
    let latestRevision = "7";
    adminFetch.mockImplementation(async (requestPath, init) => {
      if (requestPath === path && init?.method === "PUT") throw new ApiError("settings_conflict", "Conflict", 409);
      if (requestPath === "/admin/api/oidc-config") return { oidc_config: { enabled: false, issuer: latestRevision === "7" ? "" : "https://changed.example.test" }, revision: latestRevision };
      if (requestPath === "/admin/api/operator/sign-in") return { mode: "both", built_in: true, oidc: false };
      if (requestPath === "/admin/api/operator/overrides") return { overrides: { media_job_admission: { max_queue_depth: latestRevision === "7" ? null : 99 }, revisions: { media_job_admission_overrides: latestRevision } } };
      throw new Error("Fixture card unavailable");
    });
    const user = userEvent.setup();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(<QueryClientProvider client={queryClient}><SettingsPage /></QueryClientProvider>);
    await user.click(await screen.findByRole("button", { name: `Edit ${section}` }));
    const input = screen.getByLabelText(field);
    await user.clear(input);
    await user.type(input, draft);
    await user.click(screen.getByRole("button", { name: save }));
    await screen.findByText("Settings changed elsewhere; reload to see the latest");
    latestRevision = "8";
    await queryClient.invalidateQueries({ queryKey: ["settings", section === "Sign-in" ? "oidc" : "media"] });
    expect(input).toBeInTheDocument();
    expect(input).toHaveValue(field === "Maximum queued jobs" ? Number(draft) : draft);
    await user.click(screen.getByRole("button", { name: save }));
    await waitFor(() => expect(adminFetch.mock.calls.filter(([, init]) => init?.method === "PUT")).toHaveLength(2));
    expect(adminFetch.mock.calls.filter(([, init]) => init?.method === "PUT").every(([, init]) => init.headers["If-Match"] === "7")).toBe(true);
    expect(adminFetch).toHaveBeenCalledWith(path, expect.objectContaining({ method: "PUT", headers: { "If-Match": "7" } }));
  });

  it("advances the pinned revision after a successful save", async () => {
    adminFetch.mockImplementation(async (path, init) => {
      if (path === "/admin/api/settings/instance") return { instance: init?.body ?? { name: "Original", base_url: "", theme_default: "system" }, revision: init?.method === "PUT" ? "8" : "7" };
      throw new Error("Fixture card unavailable");
    });
    const user = userEvent.setup();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(<QueryClientProvider client={queryClient}><SettingsPage /></QueryClientProvider>);
    await user.click(await screen.findByRole("button", { name: "Edit Instance" }));
    await user.click(screen.getByRole("button", { name: "Save instance" }));
    await waitFor(() => expect(queryClient.getQueryData<{ revision: string }>(["settings", "instance"])?.revision).toBe("8"));
    await user.click(screen.getByRole("button", { name: "Save instance" }));
    await waitFor(() => expect(adminFetch.mock.calls.filter(([, init]) => init?.method === "PUT")).toHaveLength(2));
    expect(adminFetch.mock.calls.filter(([, init]) => init?.method === "PUT").map(([, init]) => init.headers["If-Match"])).toEqual(["7", "8"]);
  });

});
