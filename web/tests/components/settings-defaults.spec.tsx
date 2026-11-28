import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { adminFetch, api } from "@/lib/api";
import { ViewerContext } from "@/components/shell/session";
import SettingsPage from "@/app/settings/page";
vi.mock("@/lib/api", async (original) => ({
  ...(await original<typeof import("@/lib/api")>()),
  adminFetch: vi.fn(),
}));
// Design v4 §2.9, §3.4: no default plan anywhere; the only "default" is the
// plan preselected for new invitations, initially "Ask each time".
let client: QueryClient;
let invitePlan: string | null;
beforeEach(() => {
  invitePlan = null;
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
      invitePlan = (init.body as { invite_access_plan: string | null })
        .invite_access_plan;
    return { defaults: { invite_access_plan: invitePlan }, aliases: {} } as never;
  });
  vi.spyOn(api.accessPlans, "list").mockResolvedValue([
    { id: "plan_chat", name: "Household chat" },
  ] as never);
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
        value={{ authenticated: true, name: "Owner", email: null, role: "owner", admin: true }}
      >
        <SettingsPage />
      </ViewerContext.Provider>
    </QueryClientProvider>,
  );
  await screen.findByLabelText("Access plan for new invitations");
}
const section = () =>
  within(screen.getByRole("region", { name: "People and access" }));
it("starts at Ask each time and offers no built-in or None plan", async () => {
  await mount();
  await userEvent.click(screen.getByLabelText("Access plan for new invitations"));
  const options = screen.getAllByRole("option").map((option) => option.textContent);
  expect(options).toEqual(["Ask each time", "Household chat"]);
  expect(screen.queryByText(/None — built-in local access/)).not.toBeInTheDocument();
  expect(screen.queryByText(/Default local \(built-in\)/)).not.toBeInTheDocument();
  expect(screen.queryByText(/does not block AI access/)).not.toBeInTheDocument();
  expect(screen.queryByText(/Let people create their own apps/)).not.toBeInTheDocument();
});
it("saves the chosen plan for future invitations, and null for Ask each time", async () => {
  await mount();
  await userEvent.click(screen.getByLabelText("Access plan for new invitations"));
  await userEvent.click(screen.getByRole("option", { name: "Household chat" }));
  await userEvent.click(section().getByRole("button", { name: "Save" }));
  expect(
    await section().findByText("Access plan for new invitations saved"),
  ).toBeVisible();
  expect(invitePlan).toBe("plan_chat");
  await userEvent.click(screen.getByLabelText("Access plan for new invitations"));
  await userEvent.click(screen.getByRole("option", { name: "Ask each time" }));
  await userEvent.click(section().getByRole("button", { name: "Save" }));
  await vi.waitFor(() => expect(invitePlan).toBeNull());
});
it("links to the access plans", async () => {
  await mount();
  expect(
    section().getByRole("link", { name: "View and edit access plans" }),
  ).toHaveAttribute("href", "/people?tab=plans");
});
