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

test("scrolling table without a name stays focusable without a region", () => {
  const view = render(<Table />);
  const container = view.container.querySelector('[data-slot="table-container"]')!;
  expect(container.getAttribute("tabindex")).toBe("0");
  expect(container.hasAttribute("role")).toBe(false);
  expect(screen.queryByRole("region")).toBeNull();
});

test("scrolling table with a plain caption stays focusable without a region", () => {
  const view = render(<Table><caption>Plain caption</caption></Table>);
  const container = view.container.querySelector('[data-slot="table-container"]')!;
  expect(container.getAttribute("tabindex")).toBe("0");
  expect(container.hasAttribute("role")).toBe(false);
  expect(screen.queryByRole("region")).toBeNull();
  expect(screen.getByRole("table", { name: "Plain caption" })).toBeTruthy();
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
  expect(region.getAttribute("tabindex")).toBe("0");
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
  expect(screen.getByRole("region", { name: "Outside heading" })).toBe(region);
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
  expect(region.hasAttribute("role")).toBe(false);
  expect(screen.queryByRole("region")).toBeNull();
  expect(region.getAttribute("tabindex")).toBe("0");
});

test("caption outside Table keeps working without an injected ID", () => {
  render(<table><TableCaption>Standalone caption</TableCaption></table>);
  expect(screen.getByText("Standalone caption").hasAttribute("id")).toBe(false);
});

test("whitespace aria-label does not name the scrolling region", () => {
  const view = render(<Table aria-label="   " />);
  const container = view.container.querySelector('[data-slot="table-container"]')!;
  expect(screen.queryByRole("region")).toBeNull();
  expect(container.hasAttribute("aria-label")).toBe(false);
  expect(container.getAttribute("tabindex")).toBe("0");
  expect(view.container.querySelector("table")!.getAttribute("aria-label")).toBe("   ");
});

test("whitespace aria-labelledby falls back to the trimmed aria-label", () => {
  render(<Table aria-labelledby="   " aria-label="  Usage  " />);
  const region = screen.getByRole("region", { name: "Usage" });
  expect(region.getAttribute("aria-label")).toBe("Usage");
  expect(region.hasAttribute("aria-labelledby")).toBe(false);
  expect(region.querySelector("table")!.getAttribute("aria-labelledby")).toBe("   ");
});

test("mounted caption names the region before its own state supplies text", () => {
  let updateText!: React.Dispatch<React.SetStateAction<string>>;
  function CaptionText() {
    const [text, setText] = React.useState("");
    updateText = setText;
    return <span>{text}</span>;
  }
  const view = render(<Table><TableCaption><CaptionText /></TableCaption></Table>);
  const region = screen.getByRole("region");
  const caption = view.container.querySelector("caption")!;
  expect(region.getAttribute("aria-labelledby")).toBe(caption.id);
  expect(region.getAttribute("tabindex")).toBe("0");
  act(() => updateText("Requests"));
  expect(screen.getByRole("region", { name: "Requests" })).toBe(region);
  act(() => updateText(""));
  expect(screen.getByRole("region")).toBe(region);
  expect(region.getAttribute("aria-labelledby")).toBe(caption.id);
});

test("an image-only caption names the scrolling region", () => {
  render(<Table><TableCaption><img src="/requests.png" alt="Requests" /></TableCaption></Table>);
  const region = screen.getByRole("region", { name: "Requests" });
  expect(region.getAttribute("aria-labelledby")).toBe(screen.getByAltText("Requests").closest("caption")!.id);
  expect(region.getAttribute("tabindex")).toBe("0");
});

test("aria-labelledby resolves dynamically with a trimmed aria-label fallback", () => {
  const table = <Table aria-labelledby="  missing-heading  " aria-label="  Fallback  " />;
  const view = render(<>{table}</>);
  const region = screen.getByRole("region", { name: "Fallback" });
  expect(region.getAttribute("aria-labelledby")).toBe("missing-heading");
  expect(region.getAttribute("aria-label")).toBe("Fallback");
  expect(region.getAttribute("tabindex")).toBe("0");
  view.rerender(<>
    {table}
    <h2 id="missing-heading"><img src="/usage.png" alt="Usage" /></h2>
  </>);
  expect(screen.getByRole("region", { name: "Usage" })).toBe(region);
  expect(region.getAttribute("aria-label")).toBe("Fallback");
  view.rerender(<>{table}</>);
  expect(screen.getByRole("region", { name: "Fallback" })).toBe(region);
  expect(region.getAttribute("aria-labelledby")).toBe("missing-heading");
  expect(region.getAttribute("tabindex")).toBe("0");
});
