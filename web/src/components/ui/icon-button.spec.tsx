// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { IconButton } from "./icon-button";

afterEach(cleanup);

describe("IconButton loading", () => {
  it("keeps focus and its accessible name across loading and re-enables clicks", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    const { rerender } = render(
      <IconButton aria-label="Refresh" onClick={onClick}>R</IconButton>,
    );
    const button = screen.getByRole("button", { name: "Refresh" });
    await user.tab();
    expect(document.activeElement).toBe(button);

    rerender(<IconButton aria-label="Refresh" loading onClick={onClick}>R</IconButton>);
    expect(document.activeElement).toBe(button);
    expect(button.hasAttribute("disabled")).toBe(false);
    expect(button.getAttribute("aria-busy")).toBe("true");
    expect(button.getAttribute("aria-disabled")).toBe("true");
    expect(screen.getByRole("button", { name: "Refresh" })).toBe(button);
    expect(button.querySelector("svg")?.getAttribute("aria-hidden")).toBe("true");
    expect(button.classList.contains("disabled:pointer-events-none")).toBe(true);
    expect(button.classList.contains("disabled:opacity-50")).toBe(true);
    expect(button.classList.contains("aria-disabled:pointer-events-none")).toBe(false);
    expect(button.classList.contains("aria-disabled:opacity-50")).toBe(true);

    // Direct dispatch exercises the loading handler independently of browser hit testing.
    fireEvent.click(button);
    await user.keyboard("{Enter}");
    expect(onClick).not.toHaveBeenCalled();

    rerender(<IconButton aria-label="Refresh" onClick={onClick}>R</IconButton>);
    expect(document.activeElement).toBe(button);
    expect(button.hasAttribute("aria-disabled")).toBe(false);
    expect(button.hasAttribute("aria-busy")).toBe(false);
    await user.click(button);
    expect(onClick).toHaveBeenCalledTimes(1);
  });


  it.each(["ghost", "secondary", "danger"] as const)(
    "keeps loading %s pointer hits on the button rather than its clickable parent",
    async (variant) => {
      const user = userEvent.setup();
      const onClick = vi.fn();
      const onParentClick = vi.fn();
      render(
        <div onClick={onParentClick}>
          <IconButton aria-label="Refresh" variant={variant} loading onClick={onClick}>R</IconButton>
        </div>,
      );
      const button = screen.getByRole("button", { name: "Refresh" });
      expect(button.getAttribute("aria-disabled")).toBe("true");
      expect(button.hasAttribute("disabled")).toBe(false);
      expect(button.classList.contains("aria-disabled:opacity-50")).toBe(true);
      // Native :disabled is inactive here. No other built-in token may redirect a pointer hit.
      expect(Array.from(button.classList).filter((token) =>
        token.endsWith("pointer-events-none") && token !== "disabled:pointer-events-none",
      )).toEqual([]);
      // jsdom verifies dispatch isolation; the class oracle above verifies its hit-target precondition.
      await user.click(button);
      expect(onClick).not.toHaveBeenCalled();
      expect(onParentClick).not.toHaveBeenCalled();
    },
  );

  it.each(["direct click", "Enter", "Space"] as const)(
    "blocks button and ancestor clicks while loading via %s",
    async (activation) => {
      const user = userEvent.setup();
      const onClick = vi.fn();
      const onParentClick = vi.fn();
      const onClickCapture = vi.fn();
      render(
        <div onClick={onParentClick} onClickCapture={onClickCapture}>
          <IconButton aria-label="Refresh" loading onClick={onClick}>R</IconButton>
        </div>,
      );
      const button = screen.getByRole("button", { name: "Refresh" });
      await user.tab();
      expect(document.activeElement).toBe(button);
      expect(button.hasAttribute("disabled")).toBe(false);

      if (activation === "direct click") {
        fireEvent.click(button);
      } else {
        await user.keyboard(activation === "Enter" ? "{Enter}" : " ");
      }

      // Capture proves each activation produced a click, not a vacuous no-op.
      expect(onClickCapture).toHaveBeenCalledTimes(1);
      expect(onClickCapture.mock.calls[0][0].target).toBe(button);
      expect(onClick).not.toHaveBeenCalled();
      expect(onParentClick).not.toHaveBeenCalled();
      expect(document.activeElement).toBe(button);
    },
  );

  it("allows button and ancestor clicks when not loading", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    const onParentClick = vi.fn();
    render(
      <div onClick={onParentClick}>
        <IconButton aria-label="Refresh" onClick={onClick}>R</IconButton>
      </div>,
    );

    await user.click(screen.getByRole("button", { name: "Refresh" }));
    expect(onClick).toHaveBeenCalledTimes(1);
    expect(onParentClick).toHaveBeenCalledTimes(1);
  });

  it("prevents submit default actions while loading", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn((event) => event.preventDefault());
    const onClick = vi.fn();
    render(
      <form onSubmit={onSubmit}>
        <IconButton aria-label="Save" type="submit" loading onClick={onClick}>S</IconButton>
      </form>,
    );
    const button = screen.getByRole("button", { name: "Save" });
    expect(button.hasAttribute("disabled")).toBe(false);
    expect(fireEvent.click(button)).toBe(false);
    await user.tab();
    await user.keyboard("{Enter}");
    expect(onClick).not.toHaveBeenCalled();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("keeps the explicit disabled prop native with or without loading", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    const { rerender } = render(
      <IconButton aria-label="Remove" disabled onClick={onClick}>X</IconButton>,
    );
    const button = screen.getByRole("button", { name: "Remove" });
    expect(button.hasAttribute("disabled")).toBe(true);
    await user.click(button);
    expect(onClick).not.toHaveBeenCalled();
    rerender(<IconButton aria-label="Remove" disabled loading onClick={onClick}>X</IconButton>);
    expect(button.hasAttribute("disabled")).toBe(true);
  });
});

describe.each([
  { value: true, label: "boolean true" },
  { value: "true", label: "string true" },
] as const)("IconButton caller aria-disabled $label", ({ value }) => {
  it.each(["pointer click", "Enter", "Space"] as const)(
    "blocks activation, bubbling and submission via %s without loading",
    async (activation) => {
      const user = userEvent.setup();
      const onClick = vi.fn();
      const onParentClick = vi.fn();
      const onClickCapture = vi.fn();
      const onSubmit = vi.fn((event) => event.preventDefault());
      render(
        <div onClick={onParentClick} onClickCapture={onClickCapture}>
          <form onSubmit={onSubmit}>
            <IconButton aria-label="Save" type="submit" aria-disabled={value} onClick={onClick}>S</IconButton>
          </form>
        </div>,
      );
      const button = screen.getByRole("button", { name: "Save" });
      expect(button.hasAttribute("disabled")).toBe(false);
      expect(button.hasAttribute("aria-busy")).toBe(false);
      expect(button.getAttribute("aria-disabled")).toBe("true");
      await user.tab();
      expect(document.activeElement).toBe(button);

      if (activation === "pointer click") {
        await user.click(button);
      } else {
        await user.keyboard(activation === "Enter" ? "{Enter}" : " ");
      }

      // Capture verifies a real click reached the guard for every activation.
      expect(onClickCapture).toHaveBeenCalledTimes(1);
      expect(onClickCapture.mock.calls[0][0].target).toBe(button);
      expect(onClickCapture.mock.calls[0][0].nativeEvent.defaultPrevented).toBe(true);
      expect(onClick).not.toHaveBeenCalled();
      expect(onParentClick).not.toHaveBeenCalled();
      expect(onSubmit).not.toHaveBeenCalled();
      expect(document.activeElement).toBe(button);
    },
  );
});

describe.each([
  { value: false, label: "boolean false" },
  { value: "false", label: "string false" },
] as const)("IconButton caller aria-disabled $label", ({ value }) => {
  it("allows clicks, bubbling and submission without loading", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    const onParentClick = vi.fn();
    const onSubmit = vi.fn((event) => event.preventDefault());
    render(
      <div onClick={onParentClick}>
        <form onSubmit={onSubmit}>
          <IconButton aria-label="Save" type="submit" aria-disabled={value} onClick={onClick}>S</IconButton>
        </form>
      </div>,
    );
    const button = screen.getByRole("button", { name: "Save" });
    expect(button.getAttribute("aria-disabled")).toBe("false");
    await user.click(button);
    expect(onClick).toHaveBeenCalledTimes(1);
    expect(onParentClick).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });
});
