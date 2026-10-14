import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen } from "@testing-library/react";
import { toast } from "@/lib/toast";

vi.mock("@/lib/toast", () => ({
  toast: { success: vi.fn(), info: vi.fn(), error: vi.fn(), warning: vi.fn() },
}));
vi.mock("@/lib/signin-api", async (original) => {
  const real = await original<typeof import("@/lib/signin-api")>();
  return {
    ...real,
    getSignIn: vi.fn(),
    previewSignIn: vi.fn(),
    discoverProvider: vi.fn(),
    startSignInTest: vi.fn(),
    getSignInTest: vi.fn(),
    cancelSignInTest: vi.fn(),
    linkOwner: vi.fn(),
    saveSignIn: vi.fn(),
    dismissNotice: vi.fn(),
    setOwnerPassword: vi.fn(),
  };
});

import {
  fakePopup,
  liveSnapshot,
  mocked,
  mountSignIn,
  passedStatus,
  runTest,
  stubPopup,
} from "./test-support";

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const testButton = () => screen.getByRole("button", { name: "Test sign-in" });

describe("Test sign-in: the pop-up flow", () => {
  it("opens the window in the click, sends the user there, and polls until it passes", async () => {
    const { user } = await mountSignIn(liveSnapshot());
    mocked.start.mockResolvedValue({
      test_id: "t1",
      start_url: "https://id.vivid.fish/auth?state=abc",
    });
    mocked.poll
      .mockResolvedValueOnce({ status: "waiting" })
      .mockResolvedValue(passedStatus());
    const popup = stubPopup();
    await user.click(testButton());
    // The window opened empty, in the click, before any request.
    expect(window.open).toHaveBeenCalledWith("", "_blank");
    expect(mocked.start).toHaveBeenCalledTimes(1);
    expect(mocked.start.mock.calls[0][0]).toMatchObject({
      address: "https://id.vivid.fish/realms/home",
      client_id: "azizam",
      client_secret: "",
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(popup.location.href).toBe("https://id.vivid.fish/auth?state=abc");
    // Waiting: the button is off, Cancel is there, the caption says how long.
    expect(
      screen.getByRole("button", { name: "Waiting for Keycloak..." }),
    ).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled();
    expect(
      screen.getByText(
        "Finish signing in at Keycloak in the new window. Waiting up to 5 minutes.",
      ),
    ).toBeInTheDocument();
    expect(mocked.poll).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1500);
    expect(mocked.poll).toHaveBeenCalledTimes(1);
    expect(mocked.poll).toHaveBeenCalledWith("t1");
    await vi.advanceTimersByTimeAsync(1500);
    expect(mocked.poll).toHaveBeenCalledTimes(2);
    expect(await screen.findByText("Keycloak works.")).toBeInTheDocument();
    expect(popup.close).toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
    // It stops asking.
    await vi.advanceTimersByTimeAsync(10_000);
    expect(mocked.poll).toHaveBeenCalledTimes(2);
  });

  it("says so at once when the browser blocks the window, and makes no request", async () => {
    const { user } = await mountSignIn(liveSnapshot());
    vi.spyOn(window, "open").mockReturnValue(null);
    await user.click(testButton());
    expect(
      await screen.findByText("Test failed: Pop-up blocked"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Your browser blocked the new window. Allow pop-ups for this page, then test again.",
      ),
    ).toBeInTheDocument();
    expect(mocked.start).not.toHaveBeenCalled();
    expect(testButton()).toBeEnabled();
  });

  it("Cancel stops the test, closes the window, restores the earlier result and says so", async () => {
    const { user } = await mountSignIn(liveSnapshot());
    await runTest(user);
    expect(await screen.findByText("Keycloak works.")).toBeInTheDocument();

    mocked.start.mockResolvedValue({ test_id: "t2", start_url: "https://x.test/a" });
    mocked.poll.mockResolvedValue({ status: "waiting" });
    const popup = stubPopup();
    await user.click(testButton());
    await vi.advanceTimersByTimeAsync(1600);
    // While waiting the old result is out of the way.
    expect(screen.queryByText("Keycloak works.")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(mocked.cancel).toHaveBeenCalledWith("t2");
    expect(popup.close).toHaveBeenCalled();
    expect(await screen.findByText("Keycloak works.")).toBeInTheDocument();
    expect(toast.info).toHaveBeenCalledWith("Test canceled.");
    // And it no longer asks.
    const polls = mocked.poll.mock.calls.length;
    await vi.advanceTimersByTimeAsync(10_000);
    expect(mocked.poll.mock.calls.length).toBe(polls);
  });

  it("gives up after five minutes with no answer", async () => {
    const { user } = await mountSignIn(liveSnapshot());
    mocked.start.mockResolvedValue({ test_id: "t3", start_url: "https://x.test/a" });
    mocked.poll.mockResolvedValue({ status: "waiting" });
    const popup = stubPopup();
    await user.click(testButton());
    await vi.advanceTimersByTimeAsync(299_000);
    expect(screen.queryByText(/^Test failed/)).toBeNull();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
    await vi.advanceTimersByTimeAsync(3_000);
    expect(
      await screen.findByText("Test failed: No answer from Keycloak"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Nothing came back after 5 minutes. Test again and finish signing in at Keycloak.",
      ),
    ).toBeInTheDocument();
    expect(popup.close).toHaveBeenCalled();
    expect(mocked.cancel).toHaveBeenCalledWith("t3");
    expect(testButton()).toBeEnabled();
  });

  it("reports a window closed before the answer, after one more look", async () => {
    const { user } = await mountSignIn(liveSnapshot());
    mocked.start.mockResolvedValue({ test_id: "t4", start_url: "https://x.test/a" });
    mocked.poll.mockResolvedValue({ status: "waiting" });
    const popup = fakePopup();
    vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
    await user.click(testButton());
    await vi.advanceTimersByTimeAsync(1600);
    expect(mocked.poll).toHaveBeenCalledTimes(1);
    popup.closed = true;
    await vi.advanceTimersByTimeAsync(1500);
    // It looked again straight away before calling it closed.
    expect(mocked.poll).toHaveBeenCalledTimes(3);
    expect(
      await screen.findByText("Test failed: Sign-in window closed"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "The sign-in window closed before it finished. Test again and finish signing in at Keycloak.",
      ),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show details" }));
    expect(
      screen.getByText("window closed before the redirect came back"),
    ).toBeInTheDocument();
  });

  it("still takes the answer that arrived as the window closed", async () => {
    const { user } = await mountSignIn(liveSnapshot());
    mocked.start.mockResolvedValue({ test_id: "t5", start_url: "https://x.test/a" });
    mocked.poll
      .mockResolvedValueOnce({ status: "waiting" })
      .mockResolvedValue(passedStatus());
    const popup = fakePopup({ closed: true });
    vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
    await user.click(testButton());
    await vi.advanceTimersByTimeAsync(1600);
    expect(await screen.findByText("Keycloak works.")).toBeInTheDocument();
    expect(screen.queryByText(/^Test failed/)).toBeNull();
  });

  it("reports a test that could not start, and closes the window", async () => {
    const { user } = await mountSignIn(liveSnapshot());
    mocked.start.mockRejectedValue(new Error("The provider address is not valid."));
    const popup = stubPopup();
    await user.click(testButton());
    expect(await screen.findByText("Test failed: Test could not start")).toBeInTheDocument();
    expect(screen.getByText("The provider address is not valid.")).toBeInTheDocument();
    expect(popup.close).toHaveBeenCalled();
  });

  it("keeps Test sign-in off until the fields are filled", async () => {
    const { user } = await mountSignIn(liveSnapshot());
    await user.click(screen.getByRole("button", { name: /Keycloak · id\.vivid\.fish/ }));
    const id = screen.getByLabelText("Client ID");
    await user.clear(id);
    expect(testButton()).toBeDisabled();
    expect(screen.getByText("Fill in the fields above first.")).toBeInTheDocument();
  });
});
