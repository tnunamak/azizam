import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import InvitePage from "./page";

let client: QueryClient;
const json = (body: unknown, status = 200) =>
  Promise.resolve(new Response(JSON.stringify(body), { status }));
beforeEach(() => {
  sessionStorage.clear();
  client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
});
afterEach(() => {
  cleanup();
  client.clear();
  vi.unstubAllGlobals();
});
function mount() {
  return render(
    <QueryClientProvider client={client}>
      <InvitePage />
    </QueryClientProvider>,
  );
}

it("asks for a new invitation, without a retry, when the link has expired", async () => {
  window.history.replaceState(null, "", "/invite/#token=expired-test-token");
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) =>
      String(input).startsWith("/auth/api/options")
        ? json({
            built_in: true,
            oidc: false,
            oidc_label: "",
            oidc_url: "/login?return_to=/",
            instance_name: "Test gateway",
            owner_name: "Gateway owner",
          })
        : json(
            {
              error: {
                code: "link_unavailable",
                message: "This link has expired or is no longer available.",
              },
            },
            404,
          ),
    ),
  );
  mount();
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("Couldn’t use this invitation");
  expect(alert).toHaveTextContent(
    "This link can no longer be used. Ask the gateway owner for a new link.",
  );
  expect(screen.queryByRole("button", { name: /try again/i })).toBeNull();
});
