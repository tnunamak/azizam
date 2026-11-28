import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AccountMenuContent } from "@/components/shell/account-menu-content";
import { ViewerContext, type Viewer } from "@/components/shell/session";
import { navigate } from "@/lib/navigate";
import { useUnsavedWork } from "@/lib/unsaved-work";

const viewer: Viewer = {
  authenticated: true,
  id: "fixture-person",
  name: "River",
  email: "river@example.test",
  role: "member",
  admin: false,
};

/** Work the page holds while the menu is open: an unsent Try message. */
function UnsentTryMessage() {
  useUnsavedWork({ kind: "try-draft", count: 1 });
  return null;
}

/** The menu closes when an item is chosen, as it does in the shell. */
function Menu({ unsaved = false }: { unsaved?: boolean }) {
  const [open, setOpen] = useState(true);
  return (
    <ViewerContext.Provider value={viewer}>
      <AccountMenuContent open={open} onOpenChange={setOpen} />
      {unsaved && <UnsentTryMessage />}
    </ViewerContext.Provider>
  );
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** A signed-in session that accepts its own sign-out. */
async function answerSessionAndLogout(input: RequestInfo | URL): Promise<Response> {
  if (String(input) === "/auth/api/browser-people") return json({ people: [] });
  if (String(input) === "/me/api/session/end-token") return json({ authenticated: true, csrf_token: "fixture-csrf" });
  if (String(input) === "/me/api/session") return json({ authenticated: true, csrf_token: "fixture-csrf" });
  if (String(input) === "/logout") return json({ next: "/sign-in/" });
  throw new Error("Unexpected URL: " + String(input));
}

beforeEach(() => {
  vi.spyOn(navigate, "replace").mockImplementation(() => {});
  vi.stubGlobal("fetch", vi.fn(answerSessionAndLogout));
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

it("account menu separates adding a person, signing out, and signing out everyone", async () => {
  render(<Menu />);
  await screen.findByRole("menuitem", { name: "Add another person" });

  const names = screen.getAllByRole("menuitem").map((item) => item.textContent?.trim());
  expect(names.slice(-3)).toEqual(["Add another person", "Sign out", "Sign out of everyone on this browser"]);
  expect(screen.getByRole("menuitem", { name: "Add another person" }).previousElementSibling).toHaveAttribute(
    "role",
    "separator",
  );
});

it("Add another person starts provider sign-out without a discard confirm when work is saved", async () => {
  const user = userEvent.setup();
  render(<Menu />);

  await user.click(await screen.findByRole("menuitem", { name: "Add another person" }));

  await waitFor(() => expect(navigate.replace).toHaveBeenCalledWith("/login/provider-sign-out?add=1"));
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("Add another person with an unsent message asks first, and Keep working preserves it", async () => {
  const user = userEvent.setup();
  render(<Menu unsaved />);

  await user.click(await screen.findByRole("menuitem", { name: "Add another person" }));

  const dialog = await screen.findByRole("dialog", { name: "Switch person?" });
  expect(within(dialog).getByRole("listitem")).toHaveTextContent("Your unsent message in Try");
  await user.click(within(dialog).getByRole("button", { name: "Keep working" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(vi.mocked(fetch).mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0);
  expect(navigate.replace).not.toHaveBeenCalled();
});

it("Sign out with an unsent message asks first, and its discard button signs out", async () => {
  const user = userEvent.setup();
  render(<Menu unsaved />);

  await user.click(await screen.findByRole("menuitem", { name: "Sign out" }));

  const dialog = await screen.findByRole("dialog", { name: "Sign out?" });
  expect(vi.mocked(fetch).mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0);
  await user.click(within(dialog).getByRole("button", { name: "Discard and sign out" }));
  await waitFor(() => expect(navigate.replace).toHaveBeenCalledWith("/sign-in/?after=sign-out"));
  expect(fetch).toHaveBeenCalledWith("/logout", expect.objectContaining({ method: "POST" }));
});

it("lists current, expired and privileged people with separate remove actions", async () => {
  vi.mocked(fetch).mockImplementation(async (input) => String(input) === "/auth/api/browser-people"
    ? json({ people: [
      { id: "one", name: "First", current: true, live: true, privileged: false },
      { id: "two", name: "Second", current: false, live: false, privileged: true },
    ] }) : answerSessionAndLogout(input));
  render(<Menu />);
  expect(await screen.findByRole("menuitem", { name: "First · Current" })).toHaveAttribute("aria-disabled", "true");
  expect(screen.getByRole("menuitem", { name: /Second.*Sign in again.*Asks to sign in again/ })).toHaveClass("text-muted-foreground");
  expect(screen.getByRole("menuitem", { name: "Remove First" })).toBeInTheDocument();
  expect(screen.getByRole("menuitem", { name: "Remove Second" })).toBeInTheDocument();
});
