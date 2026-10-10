import { describe, expect, it } from "vitest";
import { taskNames } from "@/lib/terms";
import { normalizeCapability } from "./usage-capability";

// 7877abb named Usage rows by task; speech then fell into "audio" and read
// "Make music or sounds". Text read aloud is its own task.
describe("normalizeCapability", () => {
  it.each(["voice", "speech", "audio.speech", "tts", "Voice"])(
    "puts %s under Read text aloud",
    (key) => {
      expect(normalizeCapability(key)).toBe("voice");
      expect(taskNames.speech).toBe("Read text aloud");
    },
  );

  it.each(["audio", "audio.music", "audio.sfx"])(
    "keeps %s under music or sounds",
    (key) => {
      expect(normalizeCapability(key)).toBe("audio");
    },
  );

  it("still sends transcription and the rest to their own tasks", () => {
    expect(normalizeCapability("audio.transcriptions")).toBe("transcribe");
    expect(normalizeCapability("stt")).toBe("transcribe");
    expect(normalizeCapability("default")).toBe("chat");
    expect(normalizeCapability("image.generate")).toBe("image");
    expect(normalizeCapability("something-new")).toBe("other");
  });
});
