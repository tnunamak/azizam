import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ navigate: vi.fn(), error: vi.fn() }));
vi.mock("@/lib/navigate", () => ({ navigate: { to: mocks.navigate } }));
vi.mock("@/lib/toast", () => ({ toast: { success: vi.fn(), error: mocks.error } }));

import { resetSessionCache } from "@/lib/api";
import { SignOutEverywhereSection } from "./sign-out-everywhere";

let signOut: () => Response;
let sent: { path: string; method: string; csrf: string | null }[];

beforeEach(() => {
  sent = [];
  signOut = () => Response.json({ next: "/sign-in/" });
  resetSessionCache();
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    // A member has no admin session; the sign-out token comes from their own snapshot.
    if (path === "/me/api/session")
      return Response.json({ authenticated: true, csrf_token: "csrf-token" });
    sent.push({ path, method: init?.method ?? "GET", csrf: new Headers(init?.headers).get("x-azizam-csrf") });
    return signOut();
  });
});
afterEach(() => {
  cleanup();
  resetSessionCache();
});

describe("sign out everywhere", () => {
  it("asks first, then ends every sign-in and opens the sign-in page", async () => {
    const user = userEvent.setup();
    render(<SignOutEverywhereSection />);
    await user.click(screen.getByRole("button", { name: "Sign out everywhere" }));
    const dialog = await screen.findByRole("dialog", { name: "Sign out everywhere?" });
    expect(dialog).toHaveTextContent("including this one");
    expect(sent).toEqual([]);
    await user.click(within(dialog).getByRole("button", { name: "Sign out everywhere" }));
    await waitFor(() => expect(mocks.navigate).toHaveBeenCalledWith("/sign-in/"));
    expect(sent).toEqual([
      { path: "/auth/api/sign-out-everywhere", method: "POST", csrf: "csrf-token" },
    ]);
  });

  it("does nothing when the person cancels", async () => {
    const user = userEvent.setup();
    render(<SignOutEverywhereSection />);
    await user.click(screen.getByRole("button", { name: "Sign out everywhere" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(sent).toEqual([]);
    expect(mocks.navigate).not.toHaveBeenCalled();
  });

  it("says so and stays on the page when the gateway cannot do it", async () => {
    signOut = () =>
      Response.json({ error: { code: "csrf_required", message: "Refresh this page and try again." } }, { status: 403 });
    const user = userEvent.setup();
    render(<SignOutEverywhereSection />);
    await user.click(screen.getByRole("button", { name: "Sign out everywhere" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Sign out everywhere" }));
    await waitFor(() => expect(mocks.error).toHaveBeenCalledWith("Refresh this page and try again."));
    expect(mocks.navigate).not.toHaveBeenCalled();
    // The dialog stays so the person can try again.
    expect(screen.getByRole("dialog")).toBeVisible();
  });
});
