import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Specs replace these browser properties with Object.defineProperty, which
// neither vi.unstubAllGlobals nor restoreMocks undoes. Snapshot each one per
// file and put it back (or delete it) after every test, so no test depends on
// which test ran before it.
const replaceable = (
  [
    [navigator, "clipboard"],
    [navigator, "mediaDevices"],
    [document, "execCommand"],
  ] as const
).map(
  ([target, key]) => [target, key, Object.getOwnPropertyDescriptor(target, key)] as const,
);

afterEach(() => {
  // The test files run without vitest globals, so Testing Library cannot
  // register its own cleanup; do it here for every spec.
  cleanup();
  sessionStorage.clear();
  localStorage.clear();
  for (const [target, key, original] of replaceable) {
    if (original) Object.defineProperty(target, key, original);
    else Reflect.deleteProperty(target, key);
  }
});
