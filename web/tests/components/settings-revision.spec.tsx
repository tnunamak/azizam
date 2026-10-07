import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";
import { ViewerContext } from "@/components/shell/session";
import SettingsPage from "@/app/settings/page";

const { adminFetch } = vi.hoisted(() => ({ adminFetch: vi.fn() }));
vi.mock("@/lib/api", async (original) => ({
  ...(await original<typeof import("@/lib/api")>()),
  adminFetch,
}));
let client: QueryClient;
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
});
afterEach(() => {
  cleanup();
  client?.clear();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

const resources = [
  {
    name: "instance",
    section: "Instance",
    save: "Save instance",
    field: "Instance name",
    path: "/admin/api/settings/instance",
    key: "instance",
    original: "Original",
    refreshed: "Refreshed",
    draft: "My unsaved draft",
    saved: "Server normalized name",
  },
  {
    name: "provider",
    section: "Sign-in",
    save: "Save sign-in",
    field: "Client ID",
    path: "/admin/api/oidc-config",
    key: "oidc",
    original: "original-client",
    refreshed: "refreshed-client",
    draft: "draft-client",
    saved: "normalized-client",
  },
  {
    name: "sign-in choice",
    section: "Sign-in",
    save: "Save sign-in choice",
    field: "Available sign-in methods",
    path: "/admin/api/operator/sign-in",
    key: "sign-in",
    original: "Built-in accounts only",
    refreshed: "Built-in accounts and OIDC",
    draft: "OIDC only",
    saved: "Built-in accounts and OIDC",
  },
  {
    name: "media admission",
    section: "Media admission",
    save: "Save media admission",
    field: "Maximum queued jobs",
    path: "/admin/api/operator/media-job-admission",
    key: "media",
    original: "5",
    refreshed: "9",
    draft: "12",
    saved: "10",
  },
] as const;
type Resource = (typeof resources)[number];
function payload(resource: Resource, value: string, revision: string) {
  if (resource.name === "instance")
    return {
      instance: { name: value, base_url: "", theme_default: "system" },
      revision,
    };
  if (resource.name === "provider")
    return {
      oidc_config: {
        enabled: true,
        issuer: "https://id.example.test",
        redirect_uri: "https://gateway.example.test/auth/callback",
        client_id: value,
      },
      revision,
    };
  if (resource.name === "sign-in choice")
    return {
      mode:
        value === "Built-in accounts only"
          ? "built-in"
          : value === "OIDC only"
            ? "oidc"
            : "both",
      built_in: value !== "OIDC only",
      oidc: true,
      revision,
    };
  return {
    overrides: {
      media_job_admission: { max_queue_depth: Number(value) },
      revisions: { media_job_admission_overrides: revision },
    },
  };
}
function fixture(resource: Resource) {
  const state = { value: resource.original as string, revision: "7" };
  adminFetch.mockImplementation(async (path, init) => {
    if (init?.method === "PUT") throw new Error("Unexpected save");
    const match = resources.find(
      (r) =>
        r.path === path ||
        (r.name === "media admission" &&
          path === "/admin/api/operator/overrides"),
    );
    if (match)
      return payload(
        match,
        match === resource ? state.value : match.original,
        match === resource ? state.revision : "7",
      );
    throw new Error("Fixture card unavailable");
  });
  return state;
}
function mount() {
  client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ViewerContext.Provider
        value={{
          authenticated: true,
          name: "Owner",
          email: null,
          role: "owner",
          admin: true,
        }}
      >
        <SettingsPage />
      </ViewerContext.Provider>
    </QueryClientProvider>,
  );
}
async function open(resource: Resource) {
  await userEvent.click(
    await screen.findByRole("button", { name: `Edit ${resource.section}` }),
  );
  await screen.findByLabelText(resource.field);
}
async function change(resource: Resource, value: string) {
  const field = screen.getByLabelText(resource.field);
  if (resource.name === "sign-in choice") {
    await userEvent.click(field);
    await userEvent.click(screen.getByRole("option", { name: value }));
  } else {
    await userEvent.clear(field);
    await userEvent.type(field, value);
  }
}
function assertValue(resource: Resource, value: string) {
  const field = screen.getByLabelText(resource.field);
  if (resource.name === "sign-in choice")
    expect(field).toHaveTextContent(value);
  else
    expect(field).toHaveValue(
      resource.name === "media admission" ? Number(value) : value,
    );
}
function puts() {
  return adminFetch.mock.calls.filter(([, init]) => init?.method === "PUT");
}
async function refresh(resource: Resource) {
  await act(async () => {
    await client.invalidateQueries({ queryKey: ["settings", resource.key] });
  });
}

describe.each(resources)("$name revision wiring", (resource) => {
  it("retains the dirty draft and its original revision after refresh and conflict", async () => {
    const state = fixture(resource);
    const read = adminFetch.getMockImplementation()!;
    adminFetch.mockImplementation(async (path, init) => {
      if (path === resource.path && init?.method === "PUT")
        throw new ApiError("settings_conflict", "Conflict", 409);
      return read(path, init);
    });
    mount();
    await open(resource);
    await change(resource, resource.draft);
    state.value = resource.refreshed;
    state.revision = "8";
    await refresh(resource);
    assertValue(resource, resource.draft);
    await userEvent.click(screen.getByRole("button", { name: resource.save }));
    await screen.findByText(
      "Settings changed elsewhere; reload to see the latest",
    );
    assertValue(resource, resource.draft);
    expect(puts()[0][1].headers).toEqual({ "If-Match": "7" });
    await userEvent.click(screen.getByRole("button", { name: resource.save }));
    await waitFor(() => expect(puts()).toHaveLength(2));
    expect(puts()[1][1].headers).toEqual({ "If-Match": "7" });
  });
  it("adopts the value and revision from a refresh while clean", async () => {
    const state = fixture(resource);
    const read = adminFetch.getMockImplementation()!;
    adminFetch.mockImplementation(async (path, init) => {
      if (path === resource.path && init?.method === "PUT")
        return resource.name === "media admission"
          ? {
              media_job_admission: { max_queue_depth: Number(state.value) },
              revision: state.revision,
            }
          : payload(resource, state.value, state.revision);
      return read(path, init);
    });
    mount();
    await open(resource);
    state.value = resource.refreshed;
    state.revision = "8";
    await refresh(resource);
    await waitFor(() => assertValue(resource, resource.refreshed));
    await userEvent.click(screen.getByRole("button", { name: resource.save }));
    await waitFor(() => expect(puts()).toHaveLength(1));
    expect(puts()[0][1].headers).toEqual({ "If-Match": "8" });
  });
  it("rebases the saved value and revision onto the server response", async () => {
    const state = fixture(resource);
    const read = adminFetch.getMockImplementation()!;
    adminFetch.mockImplementation(async (path, init) => {
      if (path === resource.path && init?.method === "PUT") {
        state.value = resource.saved;
        state.revision = "8";
        return resource.name === "media admission"
          ? {
              media_job_admission: { max_queue_depth: Number(state.value) },
              revision: state.revision,
            }
          : payload(resource, state.value, state.revision);
      }
      return read(path, init);
    });
    mount();
    await open(resource);
    await change(resource, resource.draft);
    await userEvent.click(screen.getByRole("button", { name: resource.save }));
    await waitFor(() => assertValue(resource, resource.saved));
    await userEvent.click(screen.getByRole("button", { name: resource.save }));
    await waitFor(() => expect(puts()).toHaveLength(2));
    expect(puts().map(([, init]) => init.headers["If-Match"])).toEqual([
      "7",
      "8",
    ]);
    expect(puts()[1][1].body).toEqual(
      expect.objectContaining(
        resource.name === "instance"
          ? { name: resource.saved }
          : resource.name === "provider"
            ? { client_id: resource.saved }
            : resource.name === "sign-in choice"
              ? { mode: "both" }
              : { max_queue_depth: Number(resource.saved) },
      ),
    );
  });
  it("explains save-time owner denial without discarding the draft", async () => {
    fixture(resource);
    const read = adminFetch.getMockImplementation()!;
    adminFetch.mockImplementation(async (path, init) => {
      if (path === resource.path && init?.method === "PUT")
        throw new ApiError("owner_required", "Owner required", 403);
      return read(path, init);
    });
    mount();
    await open(resource);
    await change(resource, resource.draft);
    await userEvent.click(screen.getByRole("button", { name: resource.save }));
    await screen.findByText("Only the owner can change this.");
    assertValue(resource, resource.draft);
  });
});

it("saving a sign-in mode keeps an edited provider revision valid for its own save", async () => {
  const provider = resources[1];
  fixture(provider);
  const read = adminFetch.getMockImplementation()!;
  adminFetch.mockImplementation(async (path, init) => {
    if (init?.method === "PUT" && path === resources[2].path)
      return { mode: "both", built_in: true, oidc: true, revision: "8" };
    if (init?.method === "PUT" && path === provider.path)
      return payload(provider, provider.draft, "8");
    return read(path, init);
  });
  mount();
  await open(provider);
  await change(provider, provider.draft);
  await change(resources[2], resources[2].refreshed);
  await userEvent.click(
    screen.getByRole("button", { name: "Save sign-in choice" }),
  );
  await waitFor(() => expect(puts()).toHaveLength(1));
  assertValue(provider, provider.draft);
  await userEvent.click(screen.getByRole("button", { name: "Save sign-in" }));
  await waitFor(() => expect(puts()).toHaveLength(2));
  expect(
    puts().map(([path, init]) => [path, init.headers["If-Match"]]),
  ).toEqual([
    [resources[2].path, "7"],
    [provider.path, "7"],
  ]);
  expect(
    screen.queryByText("Settings changed elsewhere; reload to see the latest"),
  ).not.toBeInTheDocument();
});
