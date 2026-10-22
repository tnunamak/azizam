import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SessionCover } from "./session-cover";

const header = <header>Azizam</header>;
const noop = () => {};

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("the sign-out cover", () => {
  it("is blank for the first 300 ms, then says Signing out…", () => {
    vi.useFakeTimers();
    render(<SessionCover screen={{ kind: "signing-out" }} header={header} onRetry={noop} onContinue={noop} />);
    expect(screen.queryByText("Signing out…")).toBeNull();
    act(() => {
      vi.advanceTimersByTime(299);
    });
    expect(screen.queryByText("Signing out…")).toBeNull();
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(screen.getByText("Signing out…")).toBeVisible();
    expect(document.title).toBe("Signing out · Azizam");
  });

  it("replaces the app: the header is the only page content it keeps", () => {
    render(<SessionCover screen={{ kind: "signing-out" }} header={header} onRetry={noop} onContinue={noop} />);
    expect(screen.getByText("Azizam")).toBeVisible();
    expect(screen.queryByRole("navigation")).toBeNull();
  });
});

describe("the failure screen", () => {
  it("names the layer that did not answer, and moves focus to its heading", () => {
    render(
      <SessionCover
        screen={{ kind: "failed", reason: "no-answer", tries: 1, trying: false }}
        header={header}
        onRetry={noop}
        onContinue={noop}
      />,
    );
    const heading = screen.getByRole("heading", { name: "Couldn’t finish signing out" });
    expect(heading).toHaveFocus();
    expect(screen.getByText(/This browser couldn’t reach the gateway/)).toBeVisible();
    expect(screen.queryByText(/use Sign out everywhere on another device/)).toBeNull();
  });

  it("a gateway error says the gateway did not finish", () => {
    render(
      <SessionCover
        screen={{ kind: "failed", reason: "gateway-error", tries: 1, trying: false }}
        header={header}
        onRetry={noop}
        onContinue={noop}
      />,
    );
    expect(screen.getByText(/The gateway didn’t finish signing you out/)).toBeVisible();
  });

  it("the second failure adds the way out", () => {
    render(
      <SessionCover
        screen={{ kind: "failed", reason: "no-answer", tries: 2, trying: false }}
        header={header}
        onRetry={noop}
        onContinue={noop}
      />,
    );
    expect(screen.getByText(/use Sign out everywhere on another device, or ask the person who runs Azizam/)).toBeVisible();
  });

  it("Try again retries; while the try runs the button says so and does not retry twice", async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();
    const { rerender } = render(
      <SessionCover
        screen={{ kind: "failed", reason: "no-answer", tries: 1, trying: false }}
        header={header}
        onRetry={onRetry}
        onContinue={noop}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    rerender(
      <SessionCover
        screen={{ kind: "failed", reason: "no-answer", tries: 1, trying: true }}
        header={header}
        onRetry={onRetry}
        onContinue={noop}
      />,
    );
    const busy = screen.getByRole("button", { name: "Trying again…" });
    expect(busy).toHaveAttribute("aria-disabled", "true");
    await user.click(busy);
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});

describe("the other-tab screen", () => {
  it.each([
    ["switch", "You switched to someone else in another tab."],
    ["sign-out", "You signed out in another tab."],
    ["server", "Azizam was signed out in another tab."],
  ] as const)("names the cause (%s) and offers Continue", async (cause, copy) => {
    const user = userEvent.setup();
    const onContinue = vi.fn();
    render(<SessionCover screen={{ kind: "other-tab", cause }} header={header} onRetry={noop} onContinue={onContinue} />);
    expect(screen.getByRole("heading", { name: "This tab is signed out" })).toHaveFocus();
    expect(screen.getByText(copy)).toBeVisible();
    expect(document.title).toBe("This tab is signed out · Azizam");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(onContinue).toHaveBeenCalledTimes(1);
  });
});
