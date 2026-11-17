// @vitest-environment jsdom
import * as React from "react";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { Table, TableCaption } from "./table";

let scrollWidth = 400;
let notifyResize = () => {};

beforeEach(() => {
  scrollWidth = 400;
  vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockImplementation(() => scrollWidth);
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(200);
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: ResizeObserverCallback) {
      notifyResize = () => callback([], this as unknown as ResizeObserver);
    }
    observe() {}
    unobserve() {}
    disconnect() {}
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("scrolling table copies its aria-label to a focusable named region", () => {
  render(<Table aria-label="Usage" />);
  const region = screen.getByRole("region", { name: "Usage" });
  expect(region.getAttribute("tabindex")).toBe("0");
  expect(screen.getByRole("table", { name: "Usage" })).toBeTruthy();
});

test("scrolling table uses its rendered caption as the region name", () => {
  render(<Table><TableCaption>Requests</TableCaption></Table>);
  const region = screen.getByRole("region", { name: "Requests" });
  const caption = screen.getByText("Requests");
  expect(caption.id).not.toBe("");
  expect(region.getAttribute("aria-labelledby")).toBe(caption.id);
});

test("aria-labelledby takes priority over aria-label and caption", () => {
  render(<>
    <h2 id="usage-heading">Outside heading</h2>
    <Table aria-labelledby="usage-heading" aria-label="Ignored label">
      <TableCaption>Ignored caption</TableCaption>
    </Table>
  </>);
  const region = screen.getByRole("region", { name: "Outside heading" });
  expect(region.getAttribute("aria-labelledby")).toBe("usage-heading");
  expect(region.hasAttribute("aria-label")).toBe(false);
});

test("aria-label takes priority over the caption", () => {
  render(<Table aria-label="Usage"><TableCaption>Requests</TableCaption></Table>);
  expect(screen.getByRole("region", { name: "Usage" }).hasAttribute("aria-labelledby")).toBe(false);
});

test("non-scrolling table has no region, name or tabIndex, including after resize", () => {
  scrollWidth = 200;
  const view = render(<Table aria-label="Usage" />);
  const container = view.container.querySelector('[data-slot="table-container"]')!;
  expect(screen.queryByRole("region")).toBeNull();
  for (const attribute of ["role", "aria-label", "aria-labelledby", "tabindex"]) {
    expect(container.hasAttribute(attribute)).toBe(false);
  }
  act(() => { scrollWidth = 400; notifyResize(); });
  expect(screen.getByRole("region", { name: "Usage" })).toBe(container);
  act(() => { scrollWidth = 200; notifyResize(); });
  for (const attribute of ["role", "aria-label", "aria-labelledby", "tabindex"]) {
    expect(container.hasAttribute(attribute)).toBe(false);
  }
});

test("caption registration respects an explicit ID and cleans up when removed", () => {
  const view = render(<Table><TableCaption id="custom-caption">Requests</TableCaption></Table>);
  const region = screen.getByRole("region", { name: "Requests" });
  expect(region.getAttribute("aria-labelledby")).toBe("custom-caption");
  view.rerender(<Table />);
  expect(region.hasAttribute("aria-labelledby")).toBe(false);
});

test("caption outside Table keeps working without an injected ID", () => {
  render(<table><TableCaption>Standalone caption</TableCaption></table>);
  expect(screen.getByText("Standalone caption").hasAttribute("id")).toBe(false);
});
