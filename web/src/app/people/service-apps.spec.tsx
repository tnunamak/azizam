import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, expect, it, vi } from "vitest";
import { ViewerContext, type Viewer } from "@/components/shell/session";
import PeoplePage from "./page";

const owner: Viewer = { authenticated: true, name: "Avery", email: null, role: "owner", admin: true };
const people = [
  { id: "avery", name: "Avery", email: "avery@example.test", plan_id: "all", plan_name: "Everything", disabled: false,
    last_active: null, requests_this_month: null, protected: true, role: "owner" },
  { id: "sam", name: "Sam", email: "sam@example.test", plan_id: "chat", plan_name: "Chat only", disabled: false,
    last_active: null, requests_this_month: null, role: "member" },
  { id: "kim", name: "Kim", email: "kim@example.test", plan_id: null, plan_name: null, disabled: false,
    last_active: null, requests_this_month: null, role: "member" },
];
const app = (over: Record<string, unknown>) => ({
  kind: "service", owner_user_id: "sam", accountable_person: { id: "sam", name: "Sam" }, access_plan: "person",
  same_as_person: true, access_plan_name: null, effective_access_plan_id: "chat", created_at: 1, disabled_at: null,
  ...over,
});

beforeAll(() => {
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  Element.prototype.hasPointerCapture ??= () => false;
  Element.prototype.setPointerCapture ??= () => undefined;
  Element.prototype.releasePointerCapture ??= () => undefined;
  Element.prototype.scrollIntoView ??= () => undefined;
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function serve(apps: Array<Record<string, unknown>>) {
  const calls: Array<{ method: string; path: string; body: unknown }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      const method = init?.method ?? "GET";
      if (method !== "GET") {
        calls.push({ method, path, body: init?.body ? JSON.parse(String(init.body)) : null });
        if (method === "PATCH" && path.includes("/admin/api/apps/")) {
          const moved = apps.find((item) => path.endsWith(String(item.id)));
          if (moved) moved.owner_user_id = "kim";
          return Response.json({ app: moved });
        }
        return Response.json({ ok: true });
      }
      if (path.endsWith("/me/api/session") || path.endsWith("/admin/api/session"))
        return Response.json({
          authenticated: true,
          apps: [],
          actor: { subject: "avery", email: null, roles: ["owner"], csrf_token: "test-token" },
        });
      if (path.endsWith("/admin/api/people")) return Response.json({ people, access_plans: [] });
      if (path.endsWith("/admin/api/apps")) return Response.json({ apps });
      if (path.endsWith("/admin/api/invites"))
        return Response.json({ invites: [], access_plans: [], invite_access_plan: null });
      if (path.endsWith("/admin/api/access-plans")) return Response.json({ access_plans: [] });
      return Response.json({});
    }),
  );
  return calls;
}

function renderPeople() {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ViewerContext.Provider value={owner}>
        <PeoplePage />
      </ViewerContext.Provider>
    </QueryClientProvider>,
  );
}

async function openMenu(user: ReturnType<typeof userEvent.setup>, item: string) {
  await user.click(await screen.findByRole("button", { name: "Actions for Sam" }));
  await user.click(await screen.findByRole("menuitem", { name: item }));
}

it("lists the service apps a removal will stop, each with a way to move it first, and not personal apps", async () => {
  serve([
    app({ id: "door", name: "Door camera" }),
    app({ id: "laptop", name: "Laptop", kind: "personal" }),
    app({ id: "elsewhere", name: "Kim's bot", owner_user_id: "kim" }),
  ]);
  renderPeople();
  const user = userEvent.setup();
  await openMenu(user, "Remove person");
  const dialog = within(await screen.findByRole("dialog", { name: "Remove person?" }));
  const list = await dialog.findByRole("list", { name: "Service apps that will stop" });
  expect(within(list).getByText("Door camera")).toBeVisible();
  expect(within(list).queryByText("Laptop")).toBeNull();
  expect(within(list).queryByText("Kim's bot")).toBeNull();
  expect(within(list).getByRole("button", { name: "Move to another person first" })).toBeVisible();
});

it("names the service apps a suspension stops", async () => {
  serve([app({ id: "door", name: "Door camera" })]);
  renderPeople();
  const user = userEvent.setup();
  await openMenu(user, "Disable access");
  const dialog = within(await screen.findByRole("dialog", { name: "Disable access?" }));
  expect(await dialog.findByText("Door camera")).toBeVisible();
});

it("shows no list when the person answers for no service app", async () => {
  serve([app({ id: "elsewhere", name: "Kim's bot", owner_user_id: "kim" })]);
  renderPeople();
  const user = userEvent.setup();
  await openMenu(user, "Remove person");
  const dialog = within(await screen.findByRole("dialog", { name: "Remove person?" }));
  await waitFor(() => expect(dialog.queryByText("These service apps will stop")).toBeNull());
});

it("moves a service app to another person after showing the plan it will have, then drops it from the list", async () => {
  const calls = serve([app({ id: "door", name: "Door camera" })]);
  renderPeople();
  const user = userEvent.setup();
  await openMenu(user, "Remove person");
  const removal = within(await screen.findByRole("dialog", { name: "Remove person?" }));
  await user.click(await removal.findByRole("button", { name: "Move to another person first" }));
  const move = within(await screen.findByRole("dialog", { name: "Who does Door camera answer to?" }));
  // The leaving person and anyone without access are not choices.
  await user.click(move.getByRole("combobox", { name: "Answers to" }));
  expect(screen.queryByRole("option", { name: "Sam" })).toBeNull();
  await user.click(await screen.findByRole("option", { name: "Kim" }));
  expect(move.getByText(/Kim has no access plan/)).toBeVisible();
  await user.click(move.getByRole("button", { name: "Move app" }));
  await waitFor(() =>
    expect(calls).toContainEqual({ method: "PATCH", path: "/admin/api/apps/door", body: { owner_user_id: "kim" } }),
  );
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Who does Door camera answer to?" })).toBeNull());
  await waitFor(() => expect(screen.queryByRole("list", { name: "Service apps that will stop" })).toBeNull());
});
