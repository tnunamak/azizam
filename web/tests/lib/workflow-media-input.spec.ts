import { describe, expect, it } from "vitest";
import {
  isMediaInput,
  mediaKindOf,
  mediaKindsOf,
} from "@/lib/workflow-media-input";
import { isMediaBodyInput } from "@/app/try/workflow-inputs";

describe("workflow catalog media inputs", () => {
  it("reads the catalog's asset flag and declared media type", () => {
    const face = { type: "string", asset: true, media_type: "image/*" };
    expect(isMediaInput(face)).toBe(true);
    expect(mediaKindOf(face)).toBe("image");
  });

  it("never infers a picture from an input's name", () => {
    expect(isMediaInput({ type: "string" })).toBe(false);
    expect(mediaKindOf({ type: "string" })).toBeNull();
  });

  it("takes accepted media types and file types as media", () => {
    expect(
      mediaKindsOf({ type: "string", accepted_media_types: ["image/png", "video/mp4"] }),
    ).toEqual(["image", "video"]);
    expect(mediaKindOf({ type: "audio" })).toBe("audio");
    expect(isMediaInput({ type: "file" })).toBe(true);
    expect(mediaKindOf({ type: "file" })).toBeNull();
  });

  it("does not count a text media type as media", () => {
    expect(isMediaInput({ type: "string", media_type: "text/plain" })).toBe(false);
  });

  it("gives Try's picker the same answer as the Workflows summaries", () => {
    const accepted = {
      name: "reference",
      type: "string",
      accepted_media_types: ["image/png"],
      applies: "request body",
    };
    expect(isMediaInput(accepted)).toBe(true);
    expect(isMediaBodyInput(accepted)).toBe(true);
    const text = { name: "image", type: "string", applies: "request body" };
    expect(isMediaInput(text)).toBe(false);
    expect(isMediaBodyInput(text)).toBe(false);
  });
});
