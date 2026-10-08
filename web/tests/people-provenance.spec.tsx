import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import PeoplePage from "@/app/people/page";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("renders the effective mapped plan and manual/OIDC grant provenance from People", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const body = url.endsWith("/people") ? { people: [{
      id: "person", name: "Member", email: "person@example.test",
      profile_id: "mapped", plan_name: "Mapped plan", plan_source: "group",
      disabled: false, protected: true, builtin: false, role: "owner", last_active: null,
      requests_this_month: null, grants: [
        { role: "owner", source: "manual", scope_app_id: null },
        { role: "admin", source: "oidc-claim", scope_app_id: null },
      ],
    }], profiles: [] } : url.endsWith("/invites") ? { invites: [], profiles: [] } : { profiles: [] };
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><PeoplePage /></QueryClientProvider>);
  expect(await screen.findByText("Mapped plan")).toBeInTheDocument();
  expect(screen.getByText("Group mapping")).toBeInTheDocument();
  expect(screen.getByText("owner · Manual")).toBeInTheDocument();
  expect(screen.getByText("admin · OIDC claim")).toBeInTheDocument();
});

it("opens on the person named in ?person= and leaves other rows unmarked", async () => {
  const person = (id: string, name: string) => ({
    id, name, email: `${id}@example.test`, profile_id: null, plan_name: null, plan_source: null,
    disabled: false, protected: false, builtin: false, role: "member", last_active: null,
    requests_this_month: null, grants: [],
  });
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const body = url.endsWith("/people")
      ? { people: [person("robin", "Robin"), person("zoe", "Zoë")], profiles: [] }
      : url.endsWith("/invites") ? { invites: [], profiles: [] } : { profiles: [] };
    return Response.json(body);
  }));
  window.history.replaceState(null, "", "/people?tab=invites&person=zoe");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  try {
    render(<QueryClientProvider client={client}><PeoplePage /></QueryClientProvider>);
    const zoe = (await screen.findByText("Zoë")).closest("[aria-current]");
    expect(zoe).not.toBeNull();
    expect(zoe).toHaveFocus();
    expect(screen.getByText("Robin").closest("[aria-current]")).toBeNull();
  } finally {
    window.history.replaceState(null, "", "/");
    client.clear();
  }
});
