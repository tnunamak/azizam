import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, expect, it, vi } from "vitest";
import { ViewerContext, type Viewer } from "@/components/shell/session";
import PeoplePage from "./page";

// Moved down from the consistency-polish2 browser spec: a legacy API that
// puts an email in `name` still shows it as a contact detail, once.

const owner: Viewer = {
  authenticated: true,
  name: "Tim",
  email: null,
  role: "owner",
  admin: true,
};

const person = {
  id: "email-person",
  name: "person@example.test",
  email: "person@example.test",
  profile_id: null,
  plan_name: null,
  disabled: false,
  last_active: null,
  requests_this_month: null,
};

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

it("lists a person whose legacy name is an email once, as a contact detail", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/admin/api/people"))
        return Response.json({ people: [person], profiles: [] });
      if (path.endsWith("/admin/api/invites"))
        return Response.json({ invites: [], profiles: [] });
      if (path.endsWith("/admin/api/profiles")) return Response.json({ profiles: [] });
      return Response.json({});
    }),
  );
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ViewerContext.Provider value={owner}>
        <PeoplePage />
      </ViewerContext.Provider>
    </QueryClientProvider>,
  );
  expect(await screen.findAllByText(person.email, { exact: true })).toHaveLength(1);
});
