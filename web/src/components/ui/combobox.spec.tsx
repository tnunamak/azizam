import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { Combobox } from "./combobox";

let originalScroll: PropertyDescriptor | undefined;
beforeEach(() => {
  originalScroll = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollIntoView");
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
    configurable: true, writable: true, value: vi.fn(),
  });
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
});
afterEach(() => {
  try {
    cleanup();
  } finally {
    if (originalScroll) Object.defineProperty(HTMLElement.prototype, "scrollIntoView", originalScroll);
    else Reflect.deleteProperty(HTMLElement.prototype, "scrollIntoView");
    vi.unstubAllGlobals();
  }
});

it("keeps duplicate labels searchable with distinct values and keyboard selection", async () => {
  const user = userEvent.setup();
  const onValueChange = vi.fn();
  render(<Combobox label="Category" options={[
    { value: "a", label: "Work" },
    { value: "b", label: "Work" },
  ]} onValueChange={onValueChange} />);
  await user.click(screen.getByRole("combobox", { name: "Category" }));
  const items = await screen.findAllByRole("option");
  expect(items.map((item) => item.getAttribute("data-value"))).toEqual(["a", "b"]);
  const search = screen.getByRole("combobox", { name: "Search category" });
  await user.type(search, "Work");
  await waitFor(() => {
    const filtered = screen.getAllByRole("option");
    expect(filtered.map((item) => item.getAttribute("data-value"))).toEqual(["a", "b"]);
    for (const item of filtered) expect(item).toBeVisible();
  });
  // End chooses the second result independently of its identical label.
  await user.keyboard("{End}{Enter}");
  expect(onValueChange).toHaveBeenCalledExactlyOnceWith("b");
});
