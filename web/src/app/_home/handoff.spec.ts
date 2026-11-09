import { afterEach, describe, expect, it, vi } from "vitest";
import { storeHomeHandoff, takeHomeHandoff } from "./handoff";

const picture = new File(["p"], "fox.png", { type: "image/png" });

afterEach(() => {
  vi.useRealTimers();
  window.sessionStorage.clear();
  takeHomeHandoff("mesh");
});

describe("the Home to Try hand-off", () => {
  it("carries words in storage and files in memory, and gives both back once", () => {
    storeHomeHandoff({ capability: "mesh", model: "m", prompt: "a note", files: [picture] });
    expect(window.sessionStorage.getItem("azizam.home.handoff")).not.toContain("fox.png");
    const taken = takeHomeHandoff("mesh");
    expect(taken).toMatchObject({ capability: "mesh", model: "m", prompt: "a note" });
    expect(taken?.files).toEqual([picture]);
    expect(takeHomeHandoff("mesh")).toBeNull();
  });

  it("gives no files for another kind of work, and drops them", () => {
    storeHomeHandoff({ capability: "mesh", model: "m", prompt: "", files: [picture] });
    expect(takeHomeHandoff("image")).toBeNull();
    expect(takeHomeHandoff("mesh")).toBeNull();
  });

  it("leaves files out when none were chosen", () => {
    storeHomeHandoff({ capability: "image", model: "m", prompt: "words" });
    expect(takeHomeHandoff("image")?.files).toBeUndefined();
  });

  it("drops files held longer than five minutes", () => {
    vi.useFakeTimers();
    storeHomeHandoff({ capability: "mesh", model: "m", prompt: "", files: [picture] });
    vi.advanceTimersByTime(6 * 60_000);
    expect(takeHomeHandoff("mesh")).toBeNull();
  });
});

it("keeps ordered input names with their in-memory files and consumes them once", () => {
  const ending = new File(["e"], "end.png", { type: "image/png" });
  storeHomeHandoff({ capability: "video", model: "m", prompt: "", files: [picture, ending], file_inputs: ["image", "last_frame"] });
  expect(window.sessionStorage.getItem("azizam.home.handoff")).not.toContain("last_frame");
  expect(takeHomeHandoff("video")).toMatchObject({ files: [picture, ending], file_inputs: ["image", "last_frame"] });
  expect(takeHomeHandoff("video")).toBeNull();
});

it("never returns input names without matching files", () => {
  storeHomeHandoff({ capability: "video", model: "m", prompt: "", file_inputs: ["image"] });
  expect(takeHomeHandoff("video")?.file_inputs).toBeUndefined();
  storeHomeHandoff({ capability: "video", model: "m", prompt: "", files: [picture], file_inputs: ["image", "last_frame"] });
  expect(takeHomeHandoff("video")?.file_inputs).toBeUndefined();
});
