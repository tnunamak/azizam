import { describe, expect, it } from "vitest";
import { libraryTitle } from "./library-file";
import { formatDuration, typeWords } from "./media-pick";

describe("typeWords", () => {
  it.each([
    ["image/png", "PNG"],
    ["image/jpeg", "JPEG"],
    ["audio/mpeg", "MP3"],
    ["audio/x-wav", "WAV"],
    ["video/quicktime", "MOV"],
    ["image/svg+xml", "SVG"],
    ["model/gltf-binary", "GLTF-BINARY"],
  ])("names %s %s", (type, words) => expect(typeWords(type)).toBe(words));

  it("says nothing for a type it cannot read", () => expect(typeWords("")).toBeNull());
});

describe("formatDuration", () => {
  it.each([
    [0, "0:00"],
    [3.4, "0:03"],
    [59.6, "1:00"],
    [727, "12:07"],
  ])("writes %s s as %s", (seconds, text) => expect(formatDuration(seconds)).toBe(text));
});

describe("libraryTitle", () => {
  it("is the prompt, else the derived name, else a plain name for the kind", () => {
    expect(libraryTitle({ prompt: "  A teal fish " }, "picture")).toBe("A teal fish");
    expect(libraryTitle({ prompt: " ", derived_title: "fish → 3D model" }, "picture")).toBe("fish → 3D model");
    expect(libraryTitle({}, "audio clip")).toBe("Audio clip from your Library");
  });

  it("is short enough for a line", () => {
    expect(libraryTitle({ prompt: "x".repeat(200) }, "picture")).toHaveLength(80);
  });
});
