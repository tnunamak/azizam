import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CopyButton, copyText } from "@/components/ui/copy-button";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("reports failure if the fallback element cannot be created", async () => {
  vi.stubGlobal("navigator", { clipboard: undefined });
  vi.spyOn(document, "createElement").mockImplementationOnce(() => {
    throw new Error("Document unavailable");
  });
  expect(await copyText("fixture-copy-text")).toBe(false);
});

it.each([false, "throw"])(
  "announces failure when both clipboard routes fail (fallback=%s)",
  async (result) => {
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(
      new Error("Copy denied"),
    );
    const fallback = vi.fn(() => {
      if (result === "throw") throw new Error("Fallback denied");
      return false;
    });
    const original = Object.getOwnPropertyDescriptor(document, "execCommand");
    Object.defineProperty(document, "execCommand", {
      configurable: true,
      value: fallback,
    });
    try {
      render(<CopyButton value="fixture-copy-text" />);
      await user.click(screen.getByRole("button", { name: "Copy" }));
      expect(fallback).toHaveBeenCalledWith("copy");
      expect(screen.getByRole("status")).toHaveTextContent(
        "Couldn't copy. Select the text and copy it yourself.",
      );
      expect(screen.queryByText("Copied to clipboard")).toBeNull();
      expect(document.querySelector("textarea")).toBeNull();
    } finally {
      if (original) Object.defineProperty(document, "execCommand", original);
      else Reflect.deleteProperty(document, "execCommand");
    }
  },
);

it("clears the prior success check and announcement when a repeat copy fails", async () => {
  const user = userEvent.setup();
  vi.spyOn(navigator.clipboard, "writeText")
    .mockResolvedValueOnce()
    .mockRejectedValueOnce(new Error("Repeat denied"));
  const original = Object.getOwnPropertyDescriptor(document, "execCommand");
  Object.defineProperty(document, "execCommand", {
    configurable: true,
    value: () => false,
  });
  try {
    const { container } = render(<CopyButton value="fixture-copy-text" />);
    const button = screen.getByRole("button", { name: "Copy" });
    const copyIcon = button.querySelector("svg")!.outerHTML;
    await user.click(button);
    expect(screen.getByText("Copied to clipboard")).toBeInTheDocument();
    expect(button.querySelector("svg")!.outerHTML).not.toBe(copyIcon);
    await user.click(button);
    expect(button.querySelector("svg")!.outerHTML).toBe(copyIcon);
    expect(screen.getByRole("status")).toHaveTextContent(
      "Couldn't copy. Select the text and copy it yourself.",
    );
    expect(container).not.toHaveTextContent("Copied to clipboard");
  } finally {
    if (original) Object.defineProperty(document, "execCommand", original);
    else Reflect.deleteProperty(document, "execCommand");
  }
});

it("removes the fallback textarea even when the browser throws", async () => {
  vi.stubGlobal("navigator", { clipboard: undefined });
  const original = Object.getOwnPropertyDescriptor(document, "execCommand");
  Object.defineProperty(document, "execCommand", {
    configurable: true,
    value: () => {
      throw new Error("Copy is blocked");
    },
  });
  try {
    expect(await copyText("fixture-copy-text")).toBe(false);
    expect(document.querySelector("textarea")).toBeNull();
  } finally {
    document
      .querySelectorAll("textarea")
      .forEach((element) => element.remove());
    if (original) Object.defineProperty(document, "execCommand", original);
    else Reflect.deleteProperty(document, "execCommand");
  }
});
