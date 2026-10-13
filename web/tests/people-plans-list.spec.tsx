import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import PeoplePage from "@/app/people/page";
import type { AccessPlan, AccessPlanDocument } from "@/lib/api";

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
  expect(rows[1]).toHaveTextContent("Not used yet");
});
