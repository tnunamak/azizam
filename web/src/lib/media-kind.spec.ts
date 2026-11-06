import { describe, expect, it } from "vitest";
import {
  kindFilterOptions,
  kindFromMediaType,
  kindIcon,
  kindLabel,
  kindPluralLabel,
  kindUntitledName,
  rememberKindNames,
} from "./media-kind";

describe("kindFromMediaType", () => {
  it.each([
    ["image/png", "image"],
    ["video/mp4", "video"],
    ["audio/mpeg", "audio"],
    ["model/gltf-binary", "model"],
    ["text/plain; charset=utf-8", "text"],
    ["MODEL/GLTF+JSON", "model"],
    ["smell/aroma-bundle", "smell"],
    ["application/octet-stream", "file"],
    ["nonsense", "file"],
    ["", "file"],
    [null, "file"],
    [undefined, "file"],
  ])("%s is %s", (mediaType, kind) => {
    expect(kindFromMediaType(mediaType)).toBe(kind);
  });
});

describe("labels", () => {
  it("keeps the words the Library already used for image, video and audio", () => {
    expect([kindLabel("image"), kindPluralLabel("image"), kindUntitledName("image")]).toEqual(["Image", "Images", "Picture"]);
    expect([kindLabel("video"), kindPluralLabel("video"), kindUntitledName("video")]).toEqual(["Video", "Video", "Video"]);
    expect([kindLabel("audio"), kindPluralLabel("audio"), kindUntitledName("audio")]).toEqual(["Audio", "Audio", "Audio"]);
  });

  it("names a model, and falls back to the kind's own name with a file icon for an unknown kind", () => {
    expect(kindLabel("model")).toBe("3D model");
    expect(kindLabel("smell")).toBe("Smell");
    expect(kindPluralLabel("smell")).toBe("Smell");
    expect(kindUntitledName("smell")).toBe("Smell");
    expect(kindIcon("smell")).toBe(kindIcon("file"));
    expect(kindIcon("smell")).not.toBe(kindIcon("image"));
  });
});

describe("the words come from the gateway", () => {
  it("names a kind the gateway names, and ignores rows that are not well-formed", () => {
    rememberKindNames([
      { kind: "scent", label: "Scent", plural: "Scents", untitled: "A scent" },
      { kind: "", label: "Nothing" },
      null,
      "text",
      { kind: "taste" },
    ]);
    expect([kindLabel("scent"), kindPluralLabel("scent"), kindUntitledName("scent")]).toEqual(["Scent", "Scents", "A scent"]);
    expect(kindLabel("taste")).toBe("Taste");
    expect(kindFilterOptions(["scent"]).map((option) => option.label)).toEqual(["All", "Scents"]);
  });

  it("uses the label for a plural or untitled name the gateway left out", () => {
    rememberKindNames([{ kind: "vapor", label: "Vapor" }]);
    expect([kindPluralLabel("vapor"), kindUntitledName("vapor")]).toEqual(["Vapor", "Vapor"]);
  });
});

describe("kindFilterOptions", () => {
  it("lists All and only the kinds that occur, the familiar three first", () => {
    const options = kindFilterOptions(["smell", "model", "audio", "image", "image"]);
    expect(options.map((option) => option.value)).toEqual(["all", "image", "audio", "model", "smell"]);
    expect(options.map((option) => option.label)).toEqual(["All", "Images", "Audio", "3D models", "Smell"]);
  });

  it("is only All when nothing occurs", () => {
    expect(kindFilterOptions([]).map((option) => option.value)).toEqual(["all"]);
  });
});
