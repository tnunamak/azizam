import { afterEach, expect, it, vi } from "vitest";
import { copyText } from "@/components/ui/copy-button";

afterEach(() => vi.unstubAllGlobals());

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
