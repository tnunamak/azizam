import { act, cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const toastMock = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  info: vi.fn(),
  loading: vi.fn(),
}));

vi.mock("@/lib/toast", () => ({ toast: toastMock }));
vi.mock("next-themes", () => ({ useTheme: () => ({ setTheme: vi.fn() }) }));

import DesignPage from "./page";

// Radix primitives call these browser APIs, which jsdom does not provide.
beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
  Element.prototype.hasPointerCapture = vi.fn(() => false);
  Element.prototype.releasePointerCapture = vi.fn();
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function sectionOf(heading: string) {
  const el = screen.getByRole("heading", { name: heading });
  return el.parentElement as HTMLElement;
}

describe("design page overlays", () => {
  it.each([
    ["Open dialog", "Create an app"],
    ["Open sheet", "Request details"],
  ])("%s opens %s, Escape closes it and returns focus", async (trigger, name) => {
    const user = userEvent.setup();
    render(<DesignPage />);
    const button = screen.getByRole("button", { name: trigger });
    await user.click(button);
    expect(
      await screen.findByRole("dialog", { name }),
    ).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(button).toHaveFocus();
  });

  it("app menu shows the unavailable key action disabled", async () => {
    const user = userEvent.setup();
    render(<DesignPage />);
    await user.click(
      screen.getByRole("button", { name: "App actions" }),
    );
    expect(
      await screen.findByRole("menuitem", { name: "Rotate unavailable key" }),
    ).toHaveAttribute("aria-disabled", "true");
  });

  it("popover shows the stable aliases heading", async () => {
    const user = userEvent.setup();
    render(<DesignPage />);
    await user.click(
      screen.getByRole("button", { name: "Open popover" }),
    );
    expect(
      await screen.findByRole("heading", { name: "Stable aliases" }),
    ).toBeInTheDocument();
  });
});

describe("design page vocabulary", () => {
  it("capability demos say Choose a capability, never Choose a model", async () => {
    const user = userEvent.setup();
    render(<DesignPage />);
    for (const state of ["Default", "Disabled", "Error"]) {
      expect(
        screen.getByRole("combobox", {
          name: `${state} capability`,
        }),
      ).toHaveTextContent("Choose a capability");
    }
    expect(screen.queryByText("Choose a model", { exact: true })).toBeNull();
    expect(
      screen.getByRole("combobox", { name: "Disabled capability" }),
    ).toBeDisabled();

    await user.click(
      screen.getByRole("combobox", { name: "Default capability" }),
    );
    await user.click(await screen.findByRole("option", { name: "Chat" }));
    expect(
      screen.getByRole("combobox", { name: "Default capability" }),
    ).toHaveTextContent("Chat");
  });

  it("tables use a Capability column, not a Model column", () => {
    render(<DesignPage />);
    expect(
      screen.getAllByRole("columnheader", { name: "Capability" }),
    ).toHaveLength(2);
    expect(
      screen.queryAllByRole("columnheader", { name: "Model" }),
    ).toHaveLength(0);
  });

  it("the app dialog explains keys and access plans", async () => {
    const user = userEvent.setup();
    render(<DesignPage />);
    await user.click(
      screen.getByRole("button", { name: "Open dialog" }),
    );
    expect(await screen.findByRole("dialog")).toHaveTextContent(
      "Give each app its own key and access plan.",
    );
  });
});

describe("design page error demonstrations", () => {
  it("names the layer for each combobox and copy-field demonstration", () => {
    render(<DesignPage />);
    expect(
      screen.getByRole("heading", { name: "Combobox · error demonstration" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Capabilities could not be loaded from the gateway. Reload this page.",
        { exact: true },
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "The gateway endpoint could not be loaded. Reload this page.",
        { exact: true },
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Copy field · demonstration" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Check your connection/)).toBeNull();
  });

  it("request list error disables Retry for the simulated 1.5 s, then re-enables it", async () => {
    vi.useFakeTimers();
    render(<DesignPage />);
    const demo = sectionOf("Request list error demonstration");
    expect(demo).toHaveTextContent(
      "The request list could not be loaded from the gateway. Try again.",
    );
    const retry = within(demo).getByRole("button");
    expect(retry).toBeEnabled();
    act(() => retry.click());
    expect(retry).toBeDisabled();
    act(() => {
      vi.advanceTimersByTime(1500);
    });
    expect(retry).toBeEnabled();
  });

  it("table error demonstration retries with a labelled demonstration toast", () => {
    render(<DesignPage />);
    const demo = sectionOf("Table error demonstration");
    expect(demo).toHaveTextContent(
      "The request table could not be loaded from the gateway. Try again.",
    );
    act(() =>
      within(demo).getByRole("button", { name: "Try again" }).click(),
    );
    expect(toastMock.info).toHaveBeenCalledWith(
      "Demonstration: retrying the request table.",
    );
  });

  it("Error tab shows the request error copy", async () => {
    const user = userEvent.setup();
    render(<DesignPage />);
    await user.click(screen.getByRole("tab", { name: "Error" }));
    const panel = screen.getByRole("tabpanel", { name: "Error" });
    expect(
      within(panel).getByRole("heading", {
        name: "Error demonstration",
      }),
    ).toBeInTheDocument();
    expect(panel).toHaveTextContent(
      "Requests could not be loaded from the gateway. Try again.",
    );
  });

  it("toast Error and Revoke key announce demonstration failures", async () => {
    const user = userEvent.setup();
    render(<DesignPage />);
    await user.click(screen.getByRole("button", { name: "Error" }));
    expect(toastMock.error).toHaveBeenCalledWith(
      "Demonstration: The gateway could not save these changes. Try again.",
    );
    await user.click(
      screen.getByRole("button", { name: "App actions" }),
    );
    await user.click(
      await screen.findByRole("menuitem", { name: "Revoke key" }),
    );
    expect(toastMock.error).toHaveBeenCalledWith(
      "Demonstration: The gateway could not revoke this key. Try again.",
    );
  });
});
