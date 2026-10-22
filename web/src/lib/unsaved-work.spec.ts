import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { unsavedWorkLines, unsavedWorkNow, useUnsavedWork } from "@/lib/unsaved-work";

describe("unsavedWorkLines", () => {
  it("names one unsent message in Try in the singular", () => {
    expect(unsavedWorkLines([{ kind: "try-draft", count: 1 }])).toEqual(["Your unsent message in Try"]);
  });

  it("counts Try tabs, and sums the registrations of one kind into one line", () => {
    expect(
      unsavedWorkLines([
        { kind: "try-draft", count: 1 },
        { kind: "try-draft", count: 1 },
      ]),
    ).toEqual(["Your unsent messages in 2 Try tabs"]);
  });

  it("gives each kind one line, in the order DESIGN.md 4.2 lists them, then each changed form", () => {
    expect(
      unsavedWorkLines([
        { kind: "home-key", count: 1 },
        { kind: "form", label: "Sign-in", count: 1 },
        { kind: "try-audio", count: 1 },
        { kind: "try-waiting", count: 1 },
        { kind: "try-draft", count: 2 },
      ]),
    ).toEqual([
      "Your unsent messages in 2 Try tabs",
      "The answer Try is waiting for",
      "The audio being made in Try",
      "The key you pasted on Home",
      "Changes in Sign-in",
    ]);
  });

  it.each([
    ["try-waiting", "The answer Try is waiting for"],
    ["try-picture", "The picture being made in Try"],
    ["try-audio", "The audio being made in Try"],
    ["try-video", "The video being made in Try"],
    ["home-key", "The key you pasted on Home"],
  ] as const)("has the DESIGN.md copy for %s", (kind, line) => {
    expect(unsavedWorkLines([{ kind, count: 1 }])).toEqual([line]);
  });

  it("names each changed form by its title, once per title", () => {
    expect(
      unsavedWorkLines([
        { kind: "form", label: "Local test engine", count: 1 },
        { kind: "form", label: "Sign-in", count: 1 },
        { kind: "form", label: "Sign-in", count: 1 },
      ]),
    ).toEqual(["Changes in Local test engine", "Changes in Sign-in"]);
  });

  it("has no lines when nothing is unsaved", () => {
    expect(unsavedWorkLines([])).toEqual([]);
  });
});

describe("useUnsavedWork", () => {
  it("registers while mounted, with a count of one by default, and unregisters on unmount", () => {
    const { unmount } = renderHook(() => useUnsavedWork({ kind: "home-key" }));
    expect(unsavedWorkNow()).toEqual([expect.objectContaining({ kind: "home-key", count: 1 })]);
    unmount();
    expect(unsavedWorkNow()).toEqual([]);
  });

  it("registers nothing while inactive, and follows the active flag", () => {
    const { rerender, unmount } = renderHook(
      ({ active }) => useUnsavedWork({ kind: "try-draft", count: 2, active }),
      { initialProps: { active: false } },
    );
    expect(unsavedWorkNow()).toEqual([]);
    rerender({ active: true });
    expect(unsavedWorkNow()).toEqual([expect.objectContaining({ kind: "try-draft", count: 2 })]);
    rerender({ active: false });
    expect(unsavedWorkNow()).toEqual([]);
    unmount();
  });

  it("registers a changed form under its title", () => {
    const { unmount } = renderHook(() => useUnsavedWork({ kind: "form", label: "Sign-in" }));
    expect(unsavedWorkNow()).toEqual([expect.objectContaining({ kind: "form", label: "Sign-in" })]);
    unmount();
  });
});
