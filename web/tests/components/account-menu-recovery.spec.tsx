import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AccountMenuContent } from "@/components/shell/account-menu-content";
import { ViewerContext, type Viewer } from "@/components/shell/session";
import { api } from "@/lib/api";
import { toast } from "@/lib/toast";

vi.mock("@/lib/toast", () => ({ toast: { error: vi.fn() } }));

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
beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal("fetch", vi.fn());
  vi.spyOn(api, "session").mockRejectedValue(new Error("Unexpected fallback"));
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("unlocks after failure and reads a new CSRF for the retry", async () => {
  const user = userEvent.setup();
  let rejectLogout!: (error: Error) => void;
  const pending = new Promise<Response>((_resolve, reject) => {
    rejectLogout = reject;
  });
  let sessionReads = 0;
  const logout = vi.fn()
    .mockReturnValueOnce(pending)
    .mockResolvedValueOnce(json({}, 403));
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    if (String(input) === "/me/api/session") {
      sessionReads += 1;
      return json({ csrf_token: "fixture-csrf-" + sessionReads });
    }
    if (String(input) === "/logout") return logout(init);
    throw new Error("Unexpected URL: " + String(input));
  });
  mount();
  await user.click(await screen.findByRole("menuitem", { name: "Sign out" }));
  const busy = await screen.findByRole("menuitem", { name: "Signing out…" });
  expect(busy).toHaveAttribute("aria-disabled", "true");
  fireEvent.click(busy);
  expect(logout).toHaveBeenCalledTimes(1);
  await act(async () => {
    rejectLogout(new Error("fixture offline"));
    await pending.catch(() => undefined);
  });
  const retry = await screen.findByRole("menuitem", { name: "Sign out" });
  expect(retry).not.toHaveAttribute("aria-disabled", "true");
  await user.click(retry);
  await waitFor(() => expect(toast.error).toHaveBeenCalledTimes(2));
  expect(sessionReads).toBe(2);
  expect(logout).toHaveBeenCalledTimes(2);
  expect(logout).toHaveBeenNthCalledWith(1, {
    method: "POST",
    credentials: "same-origin",
    headers: { "x-azizam-csrf": "fixture-csrf-1", accept: "application/json" },
  });
  expect(logout).toHaveBeenNthCalledWith(2, {
    method: "POST",
    credentials: "same-origin",
    headers: { "x-azizam-csrf": "fixture-csrf-2", accept: "application/json" },
  });
  expect(api.session).not.toHaveBeenCalled();
  expect(toast.error).toHaveBeenLastCalledWith(
    "We couldn't confirm you were signed out. Refresh this page and try again.",
  );
});

it("uses the admin fallback token when the member snapshot has none", async () => {
  const user = userEvent.setup();
  vi.mocked(api.session).mockResolvedValue({
    actor: {
      subject: "fixture-service",
      kind: "service_account",
      label: "Fixture service",
      email: null,
      roles: ["admin"],
      csrf_token: "fixture-admin-csrf",
    },
  } as Awaited<ReturnType<typeof api.session>>);
  vi.mocked(fetch).mockImplementation(async (input) => {
    if (String(input) === "/me/api/session") return json({});
    if (String(input) === "/logout") return json({}, 403);
    throw new Error("Unexpected URL: " + String(input));
  });
  mount();
  await user.click(await screen.findByRole("menuitem", { name: "Sign out" }));
  await waitFor(() => expect(toast.error).toHaveBeenCalledOnce());
  expect(api.session).toHaveBeenCalledOnce();
  expect(fetch).toHaveBeenCalledWith("/logout", {
    method: "POST",
    credentials: "same-origin",
    headers: { "x-azizam-csrf": "fixture-admin-csrf", accept: "application/json" },
  });
  expect(screen.getByRole("menuitem", { name: "Sign out" }))
    .not.toHaveAttribute("aria-disabled", "true");
});

it("does not submit logout after fallback failure and still permits retry", async () => {
  const user = userEvent.setup();
  vi.mocked(fetch).mockResolvedValue(json({}));
  mount();
  await user.click(await screen.findByRole("menuitem", { name: "Sign out" }));
  await waitFor(() => expect(toast.error).toHaveBeenCalledOnce());
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(api.session).toHaveBeenCalledOnce();
  await user.click(screen.getByRole("menuitem", { name: "Sign out" }));
  await waitFor(() => expect(toast.error).toHaveBeenCalledTimes(2));
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(api.session).toHaveBeenCalledTimes(2);
  expect(vi.mocked(fetch).mock.calls.every(([url]) =>
    String(url) === "/me/api/session",
  )).toBe(true);
});
