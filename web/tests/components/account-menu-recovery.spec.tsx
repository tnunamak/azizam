import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AccountMenuContent } from "@/components/shell/account-menu-content";
import { ViewerContext, type Viewer } from "@/components/shell/session";
import { navigate } from "@/lib/navigate";
import { getSessionScreen, retrySignOut } from "@/lib/session-end";

const viewer: Viewer = {
  authenticated: true,
  id: "fixture-person",
  name: "River",
  email: "river@example.test",
  role: "member",
  admin: false,
};
function mount() {
  return render(
    <ViewerContext.Provider value={viewer}>
      <AccountMenuContent open onOpenChange={() => {}} />
    </ViewerContext.Provider>,
  );
}
function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}
let replace: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  vi.clearAllMocks();
  replace = vi.spyOn(navigate, "replace").mockImplementation(() => {});
  vi.stubGlobal("fetch", vi.fn());
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("a sign-out that gets no answer leaves the failure screen; Try again reads a new CSRF token each time", async () => {
  const user = userEvent.setup();
  let sessionReads = 0;
  const logout = vi.fn()
    .mockRejectedValueOnce(new TypeError("Failed to fetch"))
    .mockResolvedValueOnce(json({ error: { code: "csrf_required", message: "Refresh" } }, 403))
    .mockResolvedValueOnce(json({ next: "/sign-in/" }));
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    if (String(input) === "/me/api/session") {
      sessionReads += 1;
      return json({ authenticated: true, csrf_token: "fixture-csrf-" + sessionReads });
    }
    if (String(input) === "/logout") return logout(init);
    throw new Error("Unexpected URL: " + String(input));
  });
  mount();
  await user.click(await screen.findByRole("menuitem", { name: "Sign out" }));
  await waitFor(() => expect(getSessionScreen()).toMatchObject({ kind: "failed", reason: "no-answer" }));
  expect(replace).not.toHaveBeenCalled();

  // The second and third sends happen inside one Try again: a 403, then a fresh token, then success.
  await waitFor(() => expect(getSessionScreen()).toMatchObject({ kind: "failed", trying: false, tries: 1 }));
  await retrySignOut();
  expect(logout).toHaveBeenCalledTimes(3);
  const tokens = logout.mock.calls.map(([init]) => new Headers((init as RequestInit).headers).get("x-azizam-csrf"));
  expect(tokens).toEqual(["fixture-csrf-1", "fixture-csrf-2", "fixture-csrf-3"]);
  expect(replace).toHaveBeenCalledWith("/sign-in/");
});

it("uses the admin session's token when the member snapshot has none", async () => {
  const user = userEvent.setup();
  vi.mocked(fetch).mockImplementation(async (input) => {
    if (String(input) === "/me/api/session") return json({ authenticated: true });
    if (String(input) === "/admin/api/session") return json({ actor: { csrf_token: "fixture-admin-csrf" } });
    if (String(input) === "/logout") return json({ next: "/sign-in/" });
    throw new Error("Unexpected URL: " + String(input));
  });
  mount();
  await user.click(await screen.findByRole("menuitem", { name: "Sign out" }));
  await waitFor(() => expect(replace).toHaveBeenCalledWith("/sign-in/"));
  expect(fetch).toHaveBeenCalledWith("/logout", expect.objectContaining({
    method: "POST",
    credentials: "same-origin",
    headers: { "x-azizam-csrf": "fixture-admin-csrf", accept: "application/json" },
  }));
});

it("a snapshot that cannot be read sends no sign-out and leaves the gateway-error screen", async () => {
  const user = userEvent.setup();
  vi.mocked(fetch).mockImplementation(async () => json({}, 500));
  mount();
  await user.click(await screen.findByRole("menuitem", { name: "Sign out" }));
  await waitFor(() => expect(getSessionScreen()).toMatchObject({ kind: "failed", reason: "gateway-error" }));
  expect(fetch).not.toHaveBeenCalledWith("/logout", expect.anything());
  expect(replace).not.toHaveBeenCalled();
});
