import { expect, it, vi } from "vitest";

// What one test puts on the browser must be gone for the next one. The two
// tests prove it only in file order, which is how vitest runs a file without
// --sequence.shuffle; under a shuffle the second passes either way.

it("a test replaces browser properties, storage and a global", () => {
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: vi.fn() } });
  Object.defineProperty(document, "execCommand", { configurable: true, value: vi.fn(() => false) });
  Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia: vi.fn() } });
  sessionStorage.setItem("leak", "1");
  localStorage.setItem("leak", "1");
  vi.stubGlobal("fetch", vi.fn());
  expect(Object.getOwnPropertyDescriptor(navigator, "clipboard")).toBeDefined();
});

it("the next test sees none of it", () => {
  expect(Object.getOwnPropertyDescriptor(navigator, "clipboard")).toBeUndefined();
  expect(Object.getOwnPropertyDescriptor(document, "execCommand")).toBeUndefined();
  expect(Object.getOwnPropertyDescriptor(navigator, "mediaDevices")).toBeUndefined();
  expect(sessionStorage.getItem("leak")).toBeNull();
  expect(localStorage.getItem("leak")).toBeNull();
  expect(vi.isMockFunction(fetch)).toBe(false);
});
