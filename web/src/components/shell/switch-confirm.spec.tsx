import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, expect, it, vi } from "vitest";
import { SwitchConfirm, type LeaveRequest } from "@/components/shell/switch-confirm";
import { navigate } from "@/lib/navigate";
import type { UnsavedWork } from "@/lib/unsaved-work";

const lost: UnsavedWork[] = [
  { kind: "try-draft", count: 1 },
  { kind: "home-key", count: 1 },
];

/** An opener that asks the confirm, as the account menu does, and reports when it closes. */
function Fixture({ end, onClose = () => {} }: { end: LeaveRequest["end"]; onClose?: () => void }) {
  const [request, setRequest] = useState<LeaveRequest | null>(null);
  return (
    <>
      <button type="button" onClick={(event) => setRequest({ end, lost, opener: event.currentTarget })}>
        Open
      </button>
      <SwitchConfirm
        request={request}
        onClose={() => {
          setRequest(null);
          onClose();
        }}
      />
    </>
  );
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

beforeEach(() => {
  vi.spyOn(navigate, "replace").mockImplementation(() => {});
  vi.stubGlobal("fetch", vi.fn());
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
});

it("Switch person lists what would be lost, and focus opens on Keep working", async () => {
  const user = userEvent.setup();
  render(<Fixture end="switch" />);
  await user.click(screen.getByRole("button", { name: "Open" }));

  const dialog = await screen.findByRole("dialog", { name: "Switch person?" });
  expect(within(dialog).getByText("Switching signs you out. This will be lost:")).toBeInTheDocument();
  expect(within(dialog).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
    "Your unsent message in Try",
    "The key you pasted on Home",
  ]);
  await waitFor(() => expect(within(dialog).getByRole("button", { name: "Keep working" })).toHaveFocus());
  expect(within(dialog).getByRole("button", { name: "Discard and switch" })).toBeInTheDocument();
});

it("Escape is Keep working: it closes the confirm, returns focus to the opener, and ends nothing", async () => {
  const user = userEvent.setup();
  const onClose = vi.fn();
  render(<Fixture end="switch" onClose={onClose} />);
  await user.click(screen.getByRole("button", { name: "Open" }));
  await screen.findByRole("dialog", { name: "Switch person?" });

  await user.keyboard("{Escape}");

  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(onClose).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("button", { name: "Open" })).toHaveFocus();
  expect(fetch).not.toHaveBeenCalled();
  expect(navigate.replace).not.toHaveBeenCalled();
});

it("Discard and switch ends the session and sends the person to the sign-in page", async () => {
  const user = userEvent.setup();
  vi.mocked(fetch).mockImplementation(async (input) => {
    if (String(input) === "/me/api/session") return json({ authenticated: true, csrf_token: "fixture-csrf" });
    if (String(input) === "/logout") return json({ next: "/sign-in/" });
    throw new Error("Unexpected URL: " + String(input));
  });
  render(<Fixture end="switch" />);
  await user.click(screen.getByRole("button", { name: "Open" }));

  await user.click(await screen.findByRole("button", { name: "Discard and switch" }));

  await waitFor(() => expect(navigate.replace).toHaveBeenCalledWith("/sign-in/"));
  expect(fetch).toHaveBeenCalledWith("/logout", expect.objectContaining({ method: "POST" }));
});

it("Sign out asks the same way, with its own title, lead line and discard button", async () => {
  const user = userEvent.setup();
  render(<Fixture end="sign-out" />);
  await user.click(screen.getByRole("button", { name: "Open" }));

  const dialog = await screen.findByRole("dialog", { name: "Sign out?" });
  expect(within(dialog).getByText("This will be lost:")).toBeInTheDocument();
  expect(within(dialog).queryByText(/Switching signs you out/)).toBeNull();
  expect(within(dialog).getByRole("button", { name: "Discard and sign out" })).toBeInTheDocument();
});
