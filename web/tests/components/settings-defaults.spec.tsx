import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { adminFetch, api, ApiError } from "@/lib/api";
import { ViewerContext } from "@/components/shell/session";
import SettingsPage from "@/app/settings/page";
vi.mock("@/lib/api", async (original) => ({
  ...(await original<typeof import("@/lib/api")>()),
  adminFetch: vi.fn(),
}));
let client: QueryClient;
let plan: string | null;
let failRead: boolean;
const access = { self_service_apps: true, auto_create_personal_app: true };
beforeEach(() => {
  plan = null;
  failRead = false;
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
  vi.mocked(adminFetch).mockImplementation(async (path, init) => {
    if (path !== "/admin/api/settings/defaults")
      throw new Error("Fixture unavailable");
    if (init?.method === "PUT")
      plan = (init.body as { profile_id: string | null }).profile_id;
    else if (failRead) throw new ApiError("unavailable", "Unavailable", 503);
    return { defaults: { profile_id: plan }, aliases: {} } as never;
  });
  vi.spyOn(api.profiles, "list").mockResolvedValue([
    { id: "chat", name: "Household chat" },
  ] as never);
  vi.spyOn(api.userAccessDefaults, "get").mockResolvedValue(access);
  vi.spyOn(api.userAccessDefaults, "update").mockImplementation(
    async (data) => ({ ...access, ...data }),
  );
});
afterEach(() => {
  cleanup();
  client.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
async function mount() {
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
  await screen.findByLabelText("Default access plan");
}
async function choose(name: string) {
  await userEvent.click(screen.getByLabelText("Default access plan"));
  await userEvent.click(screen.getByRole("option", { name }));
}
const defaults = () => within(screen.getByRole("region", { name: "Defaults" }));
const save = () =>
  userEvent.click(screen.getByRole("button", { name: "Save default plan" }));
const refresh = () =>
  act(async () => {
    await client.invalidateQueries({ queryKey: ["settings", "defaults"] });
  });
it("explains none without a repeated summary, and links directly to access plans", async () => {
  await mount();
  expect(
    defaults().getAllByText("None — built-in local access", {
      selector: "span",
    }),
  ).toHaveLength(1);
  expect(
    defaults().getByText(/Choosing none does not block AI access/),
  ).toBeVisible();
  expect(defaults().getByText(/new and existing people/)).toBeVisible();
  expect(
    defaults().getByRole("link", { name: "View and edit access plans" }),
  ).toHaveAttribute("href", "/people?tab=plans");
  expect(
    defaults().queryByRole("button", { name: /Edit|Close/ }),
  ).not.toBeInTheDocument();
});
it("explains the selected plan and keeps the same saved profile id", async () => {
  await mount();
  await choose("Household chat");
  expect(
    defaults().getByText(/capabilities allowed by this plan/),
  ).toBeVisible();
  await save();
  expect(
    await defaults().findByText("Default access plan saved"),
  ).toBeVisible();
  expect(adminFetch).toHaveBeenCalledWith("/admin/api/settings/defaults", {
    method: "PUT",
    body: { profile_id: "chat" },
  });
  await choose("None — built-in local access");
  expect(
    defaults().queryByText("Default access plan saved"),
  ).not.toBeInTheDocument();
  await save();
  await waitFor(() =>
    expect(adminFetch).toHaveBeenCalledWith("/admin/api/settings/defaults", {
      method: "PUT",
      body: { profile_id: null },
    }),
  );
});
it("shows saving inline and prevents changing the submitted choice until it finishes", async () => {
  await mount();
  await choose("Household chat");
  const read = vi.mocked(adminFetch).getMockImplementation()!;
  let finish!: () => void;
  const pending = new Promise<void>((resolve) => {
    finish = resolve;
  });
  vi.mocked(adminFetch).mockImplementation(async (path, init) => {
    if (init?.method === "PUT") await pending;
    return read(path, init);
  });
  await save();
  expect(defaults().getByRole("status")).toHaveTextContent(
    "Saving default access…",
  );
  expect(defaults().getByRole("button", { name: "Saving…" })).toBeDisabled();
  expect(screen.getByLabelText("Default access plan")).toBeDisabled();
  await act(async () => finish());
  await defaults().findByText("Default access plan saved");
});
it("retains the selection on a save error and allows retry", async () => {
  await mount();
  await choose("Household chat");
  const read = vi.mocked(adminFetch).getMockImplementation()!;
  vi.mocked(adminFetch).mockImplementation(async (path, init) => {
    if (init?.method === "PUT") throw new Error("Could not save. Try again.");
    return read(path, init);
  });
  await save();
  expect(await defaults().findByRole("alert")).toHaveTextContent(
    "Unsaved changes. Could not save. Try again.",
  );
  expect(screen.getByLabelText("Default access plan")).toHaveTextContent(
    "Household chat",
  );
  vi.mocked(adminFetch).mockImplementation(read);
  await save();
  await defaults().findByText("Default access plan saved");
});
it("keeps dirty drafts visible after a failed refresh and retries without resetting them", async () => {
  await mount();
  await choose("Household chat");
  failRead = true;
  await refresh();
  expect(
    await defaults().findByText(/values below may be out of date/),
  ).toBeVisible();
  expect(screen.getByLabelText("Default access plan")).toHaveTextContent(
    "Household chat",
  );
  failRead = false;
  await userEvent.click(defaults().getByRole("button", { name: "Try again" }));
  await waitFor(() =>
    expect(
      defaults().queryByText(/values below may be out of date/),
    ).not.toBeInTheDocument(),
  );
  expect(screen.getByLabelText("Default access plan")).toHaveTextContent(
    "Household chat",
  );
});
it("adopts a refreshed choice when clean and keeps an edited choice when dirty", async () => {
  await mount();
  plan = "chat";
  await refresh();
  await waitFor(() =>
    expect(screen.getByLabelText("Default access plan")).toHaveTextContent(
      "Household chat",
    ),
  );
  await choose("None — built-in local access");
  plan = "default-local";
  await refresh();
  expect(screen.getByLabelText("Default access plan")).toHaveTextContent(
    "None — built-in local access",
  );
});
it("explains dependent app options without silently changing their values", async () => {
  await mount();
  const region = within(screen.getByRole("region", { name: "App creation" }));
  await userEvent.click(
    await region.findByRole("switch", {
      name: "Let people create their own apps",
    }),
  );
  expect(
    region.getByRole("switch", { name: "Create a personal app automatically" }),
  ).toBeChecked();
  expect(region.getByText(/Automatic creation is inactive/)).toBeVisible();
  expect(
    region.getByText(/apps without their own plan use built-in local access/),
  ).toBeVisible();
  await userEvent.click(
    region.getByRole("button", { name: "Save app defaults" }),
  );
  await region.findByText("Personal app defaults saved");
  expect(api.userAccessDefaults.update).toHaveBeenCalledWith({
    self_service_apps: false,
    auto_create_personal_app: true,
  });
});
it("keeps app edits visible when their refresh fails", async () => {
  await mount();
  const field = await screen.findByRole("switch", {
    name: "Create a personal app automatically",
  });
  await userEvent.click(field);
  vi.mocked(api.userAccessDefaults.get).mockRejectedValue(
    new Error("Unavailable"),
  );
  await act(async () => {
    await client.invalidateQueries({
      queryKey: ["settings", "access-defaults"],
    });
  });
  expect(
    await screen.findByText(/Couldn’t refresh app creation settings/),
  ).toBeVisible();
  expect(
    screen.getByRole("switch", { name: "Create a personal app automatically" }),
  ).not.toBeChecked();
});
it("keeps app options disabled during save and retains them after an error", async () => {
  await mount();
  const region = within(screen.getByRole("region", { name: "App creation" }));
  const toggle = await region.findByRole("switch", {
    name: "Create a personal app automatically",
  });
  await userEvent.click(toggle);
  let reject!: (error: Error) => void;
  vi.mocked(api.userAccessDefaults.update).mockReturnValue(
    new Promise((_resolve, fail) => {
      reject = fail;
    }),
  );
  await userEvent.click(
    region.getByRole("button", { name: "Save app defaults" }),
  );
  expect(region.getByRole("status")).toHaveTextContent("Saving app creation…");
  expect(toggle).toBeDisabled();
  await act(async () =>
    reject(new Error("Could not save app creation. Try again.")),
  );
  expect(await region.findByRole("alert")).toHaveTextContent(
    "Could not save app creation. Try again.",
  );
  expect(toggle).not.toBeChecked();
  expect(toggle).toBeEnabled();
});
