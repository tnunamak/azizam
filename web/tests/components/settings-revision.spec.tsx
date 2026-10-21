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
  await userEvent.clear(field);
  await userEvent.type(field, value);
}
function assertValue(resource: Resource, value: string) {
  const field = screen.getByLabelText(resource.field);
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
