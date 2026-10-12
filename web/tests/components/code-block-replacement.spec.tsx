import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { CodeBlock } from "@/components/product/code-block";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("copies the chosen language and falls back when that language is removed", async () => {
  const user = userEvent.setup();
  const write = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
  const { rerender } = render(
    <CodeBlock snippets={[
      { language: "curl", code: "curl fixture-one" },
      { language: "Python", code: "print('fixture-two')" },
    ]} />,
  );
  await user.click(screen.getByRole("tab", { name: "Python" }));
  expect(screen.getByRole("tab", { name: "Python" }))
    .toHaveAttribute("aria-selected", "true");
  await user.click(screen.getByRole("button", { name: "Copy Python code" }));
  expect(write).toHaveBeenLastCalledWith("print('fixture-two')");
  expect(screen.getByRole("status")).toHaveTextContent("Code copied");

  rerender(<CodeBlock snippets={[{ language: "curl", code: "curl replacement" }]} />);
  expect(screen.queryByRole("tab", { name: "Python" })).toBeNull();
  expect(screen.getByRole("tab", { name: "curl" }))
    .toHaveAttribute("aria-selected", "true");
  expect(screen.getByRole("status")).toBeEmptyDOMElement();
  await user.click(screen.getByRole("button", { name: "Copy curl code" }));
  expect(write).toHaveBeenLastCalledWith("curl replacement");
});

it("does not announce an old pending copy for replacement content", async () => {
  const user = userEvent.setup();
  let resolve!: () => void;
  const pending = new Promise<void>((yes) => { resolve = yes; });
  const write = vi.spyOn(navigator.clipboard, "writeText")
    .mockReturnValueOnce(pending)
    .mockResolvedValueOnce();
  const { rerender } = render(<CodeBlock code="old fixture" language="Text" />);
  await user.click(screen.getByRole("button", { name: "Copy Text code" }));
  rerender(<CodeBlock code="new fixture" language="Text" />);
  await act(async () => {
    resolve();
    await pending;
  });
  expect(screen.getByRole("status")).toBeEmptyDOMElement();
  expect(screen.queryByRole("alert")).toBeNull();
  await user.click(screen.getByRole("button", { name: "Copy Text code" }));
  expect(write).toHaveBeenLastCalledWith("new fixture");
  expect(screen.getByRole("status")).toHaveTextContent("Code copied");
});

it("invalidates a pending copy when only the active language changes", async () => {
  const user = userEvent.setup();
  let resolve!: () => void;
  const pending = new Promise<void>((yes) => { resolve = yes; });
  vi.spyOn(navigator.clipboard, "writeText").mockReturnValue(pending);
  render(<CodeBlock snippets={[
    { language: "curl", code: "same fixture" },
    { language: "Python", code: "same fixture" },
  ]} />);
  await user.click(screen.getByRole("button", { name: "Copy curl code" }));
  await user.click(screen.getByRole("tab", { name: "Python" }));
  await act(async () => {
    resolve();
    await pending;
  });
  expect(screen.getByRole("status")).toBeEmptyDOMElement();
  expect(screen.queryByRole("alert")).toBeNull();
});

it("keeps empty examples explicit and never writes to the clipboard", async () => {
  const user = userEvent.setup();
  const write = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
  render(<CodeBlock snippets={[]} code="" language="Text" />);
  expect(screen.getByText("No example available yet.")).toBeVisible();
  const copy = screen.getByRole("button", { name: "Copy Text code" });
  expect(copy).toBeDisabled();
  await user.click(copy);
  expect(write).not.toHaveBeenCalled();
});

it("tells the user to copy by hand when the clipboard rejects", async () => {
  const user = userEvent.setup();
  vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("denied"));
  render(<CodeBlock code="fixture" language="Text" />);
  await user.click(screen.getByRole("button", { name: "Copy Text code" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Couldn't copy. Select the text and copy it yourself.",
  );
  expect(screen.getByRole("status")).toBeEmptyDOMElement();
});
