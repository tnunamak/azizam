import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, expect, it, vi } from "vitest";
import { CategoryList } from "@/app/categories/category-list";
import { api } from "@/lib/api";
import PeoplePage from "@/app/people/page";
import { ViewerContext, type Viewer } from "@/components/shell/session";
import type { AccessPlan, AccessPlanDocument } from "@/lib/api";

beforeAll(() => {
  Element.prototype.hasPointerCapture ??= () => false;
  Element.prototype.setPointerCapture ??= () => undefined;
  Element.prototype.releasePointerCapture ??= () => undefined;
  Element.prototype.scrollIntoView ??= () => undefined;
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const document = (name: string): AccessPlanDocument => ({
  name,
  can_use: { chat: { items: "all" }, image: { items: [{ model: "demo-image" }] } },
  direct_engine_access: { engines: [], operations: [] },
  paid_providers: [],
  provider_fallback: false,
  caps: { usd_per_request: null, usd_per_day: null, usd_per_month: null },
  can_load_models: true,
  receipt_detail: "basic",
  media_metadata: "none",
});
// The shape GET /admin/api/access-plans returns; the server writes the summary.
const plans: AccessPlan[] = ["Family", "Creative", "Builders"].map((name, index) => ({
  id: `plan-${index}`,
  name,
  created_at: 1720000000,
  updated_at: 1720000000,
  plan: { ...document(name), id: `plan-${index}` },
  summary: "Chat and images. Local models only. No paid providers.",
  used_by: { people: index === 0 ? ["person-1", "person-2"] : [], apps: index === 0 ? ["app-1"] : [], invites: 0 },
}));

it("lists each access plan as one row with its summary and counts on the second line", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      return Response.json(
        url.endsWith("/people")
          ? { people: [] }
          : url.endsWith("/invites")
            ? { invites: [] }
            : { access_plans: plans },
      );
    }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <PeoplePage />
    </QueryClientProvider>,
  );
  await userEvent.setup().click(await screen.findByRole("tab", { name: "Access plans" }));
  expect(await screen.findByRole("heading", { name: "Family" })).toBeVisible();
  const rows = within(screen.getByRole("tabpanel")).getAllByRole("listitem");
  expect(rows).toHaveLength(3);
  expect(rows.map((row) => within(row).getByRole("heading").textContent)).toEqual([
    "Family",
    "Creative",
    "Builders",
  ]);
  expect(rows[0]).toHaveTextContent("Chat and images. Local models only. No paid providers.");
  expect(rows[0]).toHaveTextContent("Used by 2 people and 1 app");
  expect(rows[1]).toHaveTextContent("No one is on this plan yet");
});

const viewer = (role: Viewer["role"]): Viewer => ({
  authenticated: true,
  name: "Someone",
  email: null,
  role,
  admin: true,
});

async function openPlansTab(role: Viewer["role"], fixture = plans) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      return Response.json(
        url.endsWith("/people")
          ? { people: [], access_plans: fixture }
          : url.endsWith("/invites")
            ? { invites: [], access_plans: fixture, invite_access_plan: null }
            : { access_plans: fixture },
      );
    }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <ViewerContext.Provider value={viewer(role)}>
      <QueryClientProvider client={client}>
        <PeoplePage />
      </QueryClientProvider>
    </ViewerContext.Provider>,
  );
  const user = userEvent.setup();
  await user.click(await screen.findByRole("tab", { name: "Access plans" }));
  await screen.findByRole("heading", { name: "Family" });
  return user;
}

it("shows access plans read-only to an administrator, with a note that only the owner changes them", async () => {
  const user = await openPlansTab("admin");
  expect(screen.getByText("Only the owner can change access plans.")).toBeVisible();
  expect(screen.queryByRole("button", { name: "New access plan" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Edit plan" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "Actions for Family" }));
  expect(await screen.findByRole("menuitem", { name: "Check access" })).toBeVisible();
  expect(screen.queryByRole("menuitem", { name: "Edit access plan" })).toBeNull();
  expect(screen.queryByRole("menuitem", { name: "Delete access plan" })).toBeNull();
});

it("offers the owner New, Edit and Delete for access plans", async () => {
  const user = await openPlansTab("owner");
  expect(screen.queryByText("Only the owner can change access plans.")).toBeNull();
  expect(screen.getByRole("button", { name: "New access plan" })).toBeVisible();
  expect(screen.getAllByRole("button", { name: "Edit plan" })).toHaveLength(3);
  await user.click(screen.getByRole("button", { name: "Actions for Family" }));
  expect(await screen.findByRole("menuitem", { name: "Edit access plan" })).toBeVisible();
  expect(screen.getByRole("menuitem", { name: "Delete access plan" })).toBeVisible();
  await user.keyboard("{Escape}");
  const unused = screen.getByRole("heading", { name: "Creative" }).closest("li")!;
  await user.click(within(unused).getByRole("button", { name: "Assign people" }));
  expect(screen.getByRole("tab", { name: /^People$/ })).toHaveAttribute("aria-selected", "true");
});


it("shows pending invitations and sign-in rules without counting them as holders", async () => {
  await openPlansTab("owner", [{ ...plans[1], used_by: { people: [], apps: [], invites: 1, signin_rules: 1 } }]);
  const row = screen.getByRole("heading", { name: "Creative" }).closest("li")!;
  expect(row).toHaveTextContent("No one is on this plan yet. 1 invitation pending.");
  expect(row).toHaveTextContent("Assigned by a sign-in rule");
  expect(within(row).getByRole("button", { name: "Assign people" })).toBeVisible();
});

it("Categories and People count the same direct Apps without counting personal Apps", async () => {
  const fixture = [{ ...plans[0], used_by: { people: ["Member"], apps: ["Direct"], invites: 0 } }];
  await openPlansTab("owner", fixture);
  expect(screen.getByRole("heading", { name: "Family" }).closest("li")).toHaveTextContent("Used by 1 person and 1 app");
  cleanup();
  vi.spyOn(api.accessPlans, "list").mockResolvedValue(fixture);
  vi.spyOn(api.apps, "list").mockResolvedValue([
    { id: "personal", effective_access_plan_id: fixture[0].id },
    { id: "direct", effective_access_plan_id: fixture[0].id },
    { id: "disabled", effective_access_plan_id: fixture[0].id, disabled_at: 1 },
  ] as never);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><CategoryList
    active={{ revision: 1, snapshot: { id: "rules", completeness_gate: true, people: {}, apps: {}, retained: {}, services: {}, categories: {
      labs: { id: "labs", label: "Labs", kind: "set", values: ["included"], applies_to: ["release"], visible_to: [fixture[0].id] },
    } } }} catalog={[]} presets={[]} catalogLoading={false} catalogError={false} onCatalogRetry={() => {}}
    onSaved={() => {}} adding={false} onAddingChange={() => {}} picking={false} onPickingChange={() => {}}
  /></QueryClientProvider>);
  expect(await screen.findByText("You; allowed plans: Family (1 person, 1 app)")).toBeVisible();
  vi.restoreAllMocks();
});
