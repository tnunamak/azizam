import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import InvitePage from "@/app/invite/page";

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  window.history.replaceState(null, "", "/");
  vi.unstubAllGlobals();
});

it("captures the fragment, clears it, and inspects using a POST JSON body", async () => {
  const token = "private-invitation-bearer";
  window.history.replaceState(null, "", `/invite/?view=join#token=${token}`);
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => new Response(JSON.stringify(
    String(input) === "/auth/api/options"
      ? { built_in: true, oidc: false }
      : { error: { code: "link_unavailable", message: "Expired invitation" } },
  ), { status: String(input) === "/auth/api/options" ? 200 : 404 }));
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><InvitePage /></QueryClientProvider>);
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/auth/api/invite", expect.objectContaining({
    method: "POST",
    body: JSON.stringify({ token }),
    headers: { "Content-Type": "application/json" },
  })));
  expect(window.location.hash).toBe("");
  expect(window.location.search).toBe("?view=join");
  expect(fetchMock.mock.calls.every(([input]) => !String(input).includes(token))).toBe(true);
});
