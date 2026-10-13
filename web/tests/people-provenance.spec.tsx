import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import PeoplePage from "@/app/people/page";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("renders the assigned plan and manual/OIDC grant provenance from People", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const body = url.endsWith("/people") ? { people: [{
      id: "person", name: "Member", email: "person@example.test",
      plan_id: "plan_household", plan_name: "Household plan",
      disabled: false, protected: true, builtin: false, role: "owner", last_active: null,
      requests_this_month: null, grants: [
        { role: "owner", source: "manual", scope_app_id: null },
        { role: "admin", source: "oidc-claim", scope_app_id: null },
      ],
    }], access_plans: [] } : url.endsWith("/invites") ? { invites: [], access_plans: [], invite_access_plan: null } : { access_plans: [] };
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><PeoplePage /></QueryClientProvider>);
  // The plan is a column on wider screens and joins the second line on a
  // phone; CSS shows one copy, jsdom has both.
  expect(await screen.findAllByText("Household plan")).toHaveLength(2);
  // There are no plan mappings: a plan is assigned, or there is none.
  expect(screen.queryByText(/mapping/)).toBeNull();
  // Craft PR 3: roles and their source are words on the second line, not chips.
  expect(screen.getByText("Owner (manual)")).toBeInTheDocument();
  expect(screen.getByText("Administrator (OIDC claim)")).toBeInTheDocument();
});

it("opens on the person named in ?person= and leaves other rows unmarked", async () => {
  const person = (id: string, name: string) => ({
    id, name, email: `${id}@example.test`, plan_id: null, plan_name: null,
    disabled: false, protected: false, builtin: false, role: "member", last_active: null,
    requests_this_month: null, grants: [],
  });
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const body = url.endsWith("/people")
      ? { people: [person("robin", "Robin"), person("zoe", "Zoë")], access_plans: [] }
      : url.endsWith("/invites") ? { invites: [], access_plans: [], invite_access_plan: null } : { access_plans: [] };
    return Response.json(body);
  }));
  window.history.replaceState(null, "", "/people?tab=invites&person=zoe");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  try {
    render(<QueryClientProvider client={client}><PeoplePage /></QueryClientProvider>);
    const zoe = (await screen.findByText("Zoë")).closest("[aria-current]");
    expect(zoe).not.toBeNull();
    expect(zoe).toHaveFocus();
    expect(zoe).toHaveAttribute("data-slot", "list-row");
    expect(screen.getByText("Robin").closest("[aria-current]")).toBeNull();
  } finally {
    window.history.replaceState(null, "", "/");
    client.clear();
  }
});

it("marks a person without an access plan and offers no built-in plan", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const body = url.endsWith("/people") ? { people: [{
      id: "new", name: "New person", email: "new@example.test", plan_id: null, plan_name: null,
      disabled: false, protected: false, builtin: false, role: "member", last_active: null,
      requests_this_month: null, grants: [],
    }], access_plans: [] } : url.endsWith("/invites") ? { invites: [], access_plans: [], invite_access_plan: null } : { access_plans: [] };
    return Response.json(body);
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><PeoplePage /></QueryClientProvider>);
  expect(await screen.findByText("No access plan")).toBeInTheDocument();
  expect(screen.getAllByText("Choose a plan").length).toBeGreaterThan(0);
  expect(screen.queryByText(/Gateway default|built-in/)).toBeNull();
  client.clear();
});
