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

it("account menu has Switch person and Sign out as separate items, in that order, after a separator", async () => {
  render(<Menu />);
  await screen.findByRole("menuitem", { name: "Switch person" });

  const names = screen.getAllByRole("menuitem").map((item) => item.textContent?.trim());
  expect(names.slice(-2)).toEqual(["Switch person", "Sign out"]);
  expect(screen.getByRole("menuitem", { name: "Switch person" }).previousElementSibling).toHaveAttribute(
    "role",
    "separator",
  );
});

it("Switch person with nothing unsaved ends the session and shows no confirm", async () => {
  const user = userEvent.setup();
  render(<Menu />);

  await user.click(await screen.findByRole("menuitem", { name: "Switch person" }));

  await waitFor(() => expect(navigate.replace).toHaveBeenCalledWith("/sign-in/"));
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("Switch person with an unsent message asks first, and Keep working ends nothing", async () => {
  const user = userEvent.setup();
  render(<Menu unsaved />);

  await user.click(await screen.findByRole("menuitem", { name: "Switch person" }));

  const dialog = await screen.findByRole("dialog", { name: "Switch person?" });
  expect(within(dialog).getByRole("listitem")).toHaveTextContent("Your unsent message in Try");
  await user.click(within(dialog).getByRole("button", { name: "Keep working" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(fetch).not.toHaveBeenCalled();
  expect(navigate.replace).not.toHaveBeenCalled();
});

it("Sign out with an unsent message asks first, and its discard button signs out", async () => {
  const user = userEvent.setup();
  render(<Menu unsaved />);

  await user.click(await screen.findByRole("menuitem", { name: "Sign out" }));

  const dialog = await screen.findByRole("dialog", { name: "Sign out?" });
  expect(fetch).not.toHaveBeenCalled();
  await user.click(within(dialog).getByRole("button", { name: "Discard and sign out" }));
  await waitFor(() => expect(navigate.replace).toHaveBeenCalledWith("/sign-in/"));
  expect(fetch).toHaveBeenCalledWith("/logout", expect.objectContaining({ method: "POST" }));
});
