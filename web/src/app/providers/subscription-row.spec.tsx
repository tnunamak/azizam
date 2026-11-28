import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ConnectionStatus } from "@/lib/api";
import { SubscriptionRow } from "./subscription-row";

afterEach(cleanup);

const NOW = 1_800_000_000_000;
const connected: ConnectionStatus = {
  kind: "fixture", display_name: "Example", status: "connected", account_email: "owner@example.invalid", models: ["sample"],
};

function mount(sub: ConnectionStatus, busy = false, expanded = false) {
  const onAction = vi.fn();
  const { container } = render(
    <ul><SubscriptionRow sub={sub} now={NOW} busy={busy} expanded={expanded} onAction={onAction} menu={<span>menu slot</span>} /></ul>,
  );
  return { onAction, container };
}

describe("subscription row", () => {
  const cases = [
    { sub: { ...connected, status: "not_connected" }, word: "Off", detail: "Let people and apps you choose use your Example models.", button: "Connect", action: "connect" },
    { sub: connected, word: "Ready", detail: "owner@example.invalid · 1 model", button: "Open", action: "open" },
    { sub: { ...connected, status: "needs_reconnect", status_changed_at: NOW / 1000 - 120 }, word: "Down",
      detail: "Example ended this sign-in 2m ago. Requests to its models fail until you connect again.",
      button: "Reconnect", action: "reconnect" },
    { sub: { ...connected, models_checked_at: NOW / 1000 - 600, last_error: { code: "check_failed", at: NOW / 1000 - 60 } },
      word: "Degraded", detail: "The last request to Example failed 1m ago. Showing the list from 10m ago.",
      button: "Check now", action: "check" },
  ] as const;

  it.each(cases)("shows $word with one $button action and a two-line row", ({ sub, word, detail, button, action }) => {
    const { onAction, container } = mount(sub);
    expect(screen.getByText("Example subscription")).toBeTruthy();
    expect(screen.getAllByText(word).length).toBeGreaterThan(0);
    expect(screen.getByTitle(detail).textContent).toContain(detail);
    expect(container.querySelector("[data-slot=list-row]")?.getAttribute("data-lines")).toBe("2");
    expect(screen.getByText("menu slot")).toBeTruthy();
    const buttons = screen.getAllByRole("button");
    expect(buttons).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: `${button} Example subscription` }));
    expect(onAction).toHaveBeenCalledTimes(1);
    expect(onAction).toHaveBeenCalledWith(action);
  });

  it.each([
    { expanded: false, label: "Open" },
    { expanded: true, label: "Close" },
  ])("uses $label for the connected row when expanded is $expanded", ({ expanded, label }) => {
    const { onAction } = mount(connected, false, expanded);
    const action = screen.getByRole("button", { name: `${label} Example subscription` });
    expect(action.textContent).toBe(label);
    expect(action.getAttribute("aria-expanded")).toBe(String(expanded));
    fireEvent.click(action);
    expect(onAction).toHaveBeenCalledTimes(1);
    expect(onAction).toHaveBeenCalledWith("open");
  });

  it("disables the action while busy", () => {
    const { onAction } = mount(connected, true);
    const action = screen.getByRole<HTMLButtonElement>("button", { name: "Open Example subscription" });
    expect(action.disabled).toBe(true);
    fireEvent.click(action);
    expect(onAction).not.toHaveBeenCalled();
  });
});

describe("subscription setup note", () => {
  const off: ConnectionStatus = { ...connected, status: "not_connected" };

  it.each([
    [{ state: "missing" }, "Setup is needed before you connect."],
    [{ state: "installing", percent: 42 }, "Setting up… 42%"],
    [{ state: "installing" }, "Setting up…"],
    [{ state: "installing", percent: 42.5 }, "Setting up…"],
    [{ state: "installing", percent: 101 }, "Setting up…"],
    [{ state: "failed" }, "Setup didn't finish. Choose Connect to try again."],
    [{ state: "ready" }, ""],
    [undefined, ""],
  ] as const)("presents setup %j without changing Connect", (runner, note) => {
    const { onAction } = mount({ ...off, runner });
    expect(screen.getByRole("status").textContent).toBe(note);
    fireEvent.click(screen.getByRole("button", { name: "Connect Example subscription" }));
    expect(onAction).toHaveBeenCalledExactlyOnceWith("connect");
  });

  it("keeps one live region from missing through installing and ready", () => {
    const onAction = vi.fn();
    const row = (runner: ConnectionStatus["runner"]) => (
      <ul><SubscriptionRow sub={{ ...off, runner }} now={NOW} onAction={onAction} /></ul>
    );
    const { rerender } = render(row({ state: "missing" }));
    const region = screen.getByRole("status");
    rerender(row({ state: "installing", percent: 42 }));
    expect(screen.getAllByRole("status")).toEqual([region]);
    expect(region.textContent).toBe("Setting up… 42%");
    rerender(row({ state: "ready" }));
    expect(screen.getAllByRole("status")).toEqual([region]);
    expect(region.textContent).toBe("");
  });
});
