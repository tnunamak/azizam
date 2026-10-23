import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { LeaveGuard } from "./leave-guard";
import { resetSessionGuardForTests, closeSession } from "@/lib/session-guard";
import { unsavedSecretEntry, unsavedWorkNow, useUnsavedSecret } from "@/lib/unsaved-work";

const router = vi.hoisted(() => ({ push: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => router,
}));

/** A page that shows a new key until its person saves it. `held` is the key being on screen and unsaved. */
function KeyPage({ held = true }: { held?: boolean }) {
  const release = useUnsavedSecret(held);
  return (
    <main>
      <a href="/library/">Library</a>
      <a href="/connect/" target="_blank" rel="noopener">
        Connect
      </a>
      <a href="#top">Top</a>
      <button type="button" onClick={release}>
        I’ve saved the key
      </button>
    </main>
  );
}

/**
 * Clicks a link and reports whether the guard had held it. The guard acts before the link's own listeners, so the
 * default is already prevented when this one reads it. Then the default is prevented here too, so jsdom does not follow
 * the link: a followed link (a fragment, or a same-page navigation) changes jsdom's history for the tests after it.
 */
function clickAndReportHeld(link: HTMLElement): boolean {
  let heldBeforeLink = false;
  link.addEventListener(
    "click",
    (event) => {
      heldBeforeLink = event.defaultPrevented;
      event.preventDefault();
    },
    { once: true },
  );
  fireEvent.click(link);
  return heldBeforeLink;
}

/** Whether the page would ask before closing: the closing prompt is shown when the event's default is prevented. */
function closingAsked(): boolean {
  const closing = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(closing);
  return closing.defaultPrevented;
}

beforeEach(() => {
  window.history.replaceState(null, "", "/apps/");
});

afterEach(() => {
  cleanup();
  resetSessionGuardForTests();
  window.history.replaceState(null, "", "/");
});

it("holds a same-tab link while a key is unsaved, and asks before following it", async () => {
  render(
    <>
      <LeaveGuard />
      <KeyPage />
    </>,
  );
  // A prevented click is one the browser does not follow, so the page stays where it is.
  expect(fireEvent.click(screen.getByRole("link", { name: "Library" }))).toBe(false);
  const dialog = await screen.findByRole("dialog", { name: "Leave anyway?" });
  expect(within(dialog).getByText("You haven’t saved this key yet. It won’t be shown again.")).toBeVisible();
  expect(router.push).not.toHaveBeenCalled();
});

it("Stay here closes the question and keeps the key held", async () => {
  render(
    <>
      <LeaveGuard />
      <KeyPage />
    </>,
  );
  fireEvent.click(screen.getByRole("link", { name: "Library" }));
  const dialog = await screen.findByRole("dialog", { name: "Leave anyway?" });
  fireEvent.click(within(dialog).getByRole("button", { name: "Stay here" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(router.push).not.toHaveBeenCalled();
  expect(unsavedSecretEntry()).not.toBeNull();
});

it("Leave anyway follows the link through the router once, and does not ask again", async () => {
  render(
    <>
      <LeaveGuard />
      <KeyPage />
    </>,
  );
  fireEvent.click(screen.getByRole("link", { name: "Library" }));
  const dialog = await screen.findByRole("dialog", { name: "Leave anyway?" });
  fireEvent.click(within(dialog).getByRole("button", { name: "Leave anyway" }));
  expect(router.push).toHaveBeenCalledTimes(1);
  expect(router.push).toHaveBeenCalledWith("/library/");
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
});

it("a link is not held once the key is saved", () => {
  render(
    <>
      <LeaveGuard />
      <KeyPage />
    </>,
  );
  fireEvent.click(screen.getByRole("button", { name: "I’ve saved the key" }));
  expect(unsavedWorkNow()).toEqual([]);
  expect(clickAndReportHeld(screen.getByRole("link", { name: "Library" }))).toBe(false);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(router.push).not.toHaveBeenCalled();
});

it("a new-tab link and a jump within the page are never held", () => {
  render(
    <>
      <LeaveGuard />
      <KeyPage />
    </>,
  );
  expect(clickAndReportHeld(screen.getByRole("link", { name: "Connect" }))).toBe(false);
  expect(clickAndReportHeld(screen.getByRole("link", { name: "Top" }))).toBe(false);
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("closing the tab asks while a key is unsaved, and stops asking once it is saved", () => {
  render(
    <>
      <LeaveGuard />
      <KeyPage />
    </>,
  );
  expect(closingAsked()).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "I’ve saved the key" }));
  expect(closingAsked()).toBe(false);
});

it("closing does not ask a second time while the session is ending, since the switch confirm already did", () => {
  render(
    <>
      <LeaveGuard />
      <KeyPage />
    </>,
  );
  closeSession();
  expect(unsavedSecretEntry()).not.toBeNull();
  expect(closingAsked()).toBe(false);
});

it("Back while a key is unsaved is undone on its page, and asks; Stay here keeps the page", async () => {
  window.history.pushState(null, "", "/library/");
  window.history.pushState(null, "", "/apps/");
  render(
    <>
      <LeaveGuard />
      <KeyPage />
    </>,
  );
  window.history.back();
  const dialog = await screen.findByRole("dialog", { name: "Leave anyway?" });
  expect(window.location.pathname).toBe("/apps/");
  fireEvent.click(within(dialog).getByRole("button", { name: "Stay here" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(window.location.pathname).toBe("/apps/");
  expect(unsavedSecretEntry()).not.toBeNull();
});

it("Leave anyway lets the Back press through to the earlier page", async () => {
  window.history.pushState(null, "", "/library/");
  window.history.pushState(null, "", "/apps/");
  render(
    <>
      <LeaveGuard />
      <KeyPage />
    </>,
  );
  window.history.back();
  const dialog = await screen.findByRole("dialog", { name: "Leave anyway?" });
  fireEvent.click(within(dialog).getByRole("button", { name: "Leave anyway" }));
  await waitFor(() => expect(window.location.pathname).toBe("/library/"));
});

it("a Back press that only moves within the page is let through", async () => {
  render(
    <>
      <LeaveGuard />
      <KeyPage />
    </>,
  );
  window.history.pushState(null, "", "/apps/#top");
  window.history.back();
  await waitFor(() => expect(window.location.hash).toBe(""));
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("a Back press after the key is saved is not held", async () => {
  window.history.pushState(null, "", "/library/");
  window.history.pushState(null, "", "/apps/");
  render(
    <>
      <LeaveGuard />
      <KeyPage />
    </>,
  );
  fireEvent.click(screen.getByRole("button", { name: "I’ve saved the key" }));
  window.history.back();
  await waitFor(() => expect(window.location.pathname).toBe("/library/"));
  expect(screen.queryByRole("dialog")).toBeNull();
});
