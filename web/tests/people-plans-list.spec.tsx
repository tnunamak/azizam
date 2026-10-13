import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import PeoplePage from "@/app/people/page";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

// Same shape the catalog-polish browser spec intercepted.
const plans = ["Family", "Creative", "Builders"].map((name, index) => ({
  id: `plan-${index}`,
  name,
  description: null,
  created_at: 1720000000,
  protocols: [],
  workflows: [],
  status_visibility: "full",
  profile: {
    allow: { intents: ["chat", "images"], exposure: ["local"] },
    models: ["demo-chat"],
    fallback: { cloud_allowed: false },
  },
}));

it("lists each access plan as one row with its summary and counts on the second line", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      return Response.json(
        url.endsWith("/people")
          ? { people: [], profiles: plans }
          : url.endsWith("/invites")
            ? { invites: [], profiles: plans }
            : { profiles: plans },
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
  expect(rows[0]).toHaveTextContent(
    "Chat and images. Local models only. No paid providers. Selected models only.",
  );
  expect(rows[0]).toHaveTextContent("1 selected model");
});
