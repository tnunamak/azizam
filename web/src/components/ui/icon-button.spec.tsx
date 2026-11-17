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
    expect(button.classList.contains("aria-disabled:pointer-events-none")).toBe(true);
    expect(button.classList.contains("aria-disabled:opacity-50")).toBe(true);

    // Direct dispatch exercises the handler even when pointer-events blocks a mouse.
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
