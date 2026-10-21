import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

it("requires an invite password and sends a one-character password", async () => {
  window.history.replaceState(null, "", "/invite/#token=valid-test-token");
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.startsWith("/auth/api/options")) return json({ built_in: true, oidc: false, oidc_label: "", oidc_url: "/login", instance_name: "Test", owner_name: "Owner" });
    if (url === "/auth/api/invite") return json({ plan: null, email: "person@example.test", expires_at: Date.now() / 1000 + 3600, owner_name: "Owner", instance_name: "Test" });
    if (url === "/auth/api/invite/accept") return json({ next: "/", credential: { api_key: "secret" } });
    return json({});
  }));
  const user = userEvent.setup();
  mount();
  await user.type(await screen.findByLabelText("Your name"), "Person");
  const password = screen.getByLabelText("Password");
  await user.type(screen.getByLabelText("Confirm password"), "x");
  await user.click(screen.getByRole("button", { name: "Create account" }));
  expect(fetch).not.toHaveBeenCalledWith("/auth/api/invite/accept", expect.anything());
  expect(password).toBeRequired();
  await user.type(password, "x");
  await user.click(screen.getByRole("button", { name: "Create account" }));
  await screen.findByRole("button", { name: "I’ve saved it" });
  expect(fetch).toHaveBeenCalledWith("/auth/api/invite/accept", expect.objectContaining({ body: JSON.stringify({ token: "valid-test-token", name: "Person", email: "person@example.test", password: "x" }) }));
});
