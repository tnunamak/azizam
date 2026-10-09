import { describe, expect, test } from "vitest";

import {
  inProgressMedia,
  modelDisplayName,
  prettifyModelId,
  tileDate,
} from "./library-format";

// Thursday 8 October 2026, mid-afternoon local time.
const now = new Date(2026, 9, 8, 15, 0);

describe("tileDate", () => {
  test.each([
    [new Date(2026, 9, 8, 9, 0), "Today"],
    [new Date(2026, 9, 7, 23, 59), "Yesterday"],
    [new Date(2026, 9, 5, 12, 0), "Monday"],
    [new Date(2026, 9, 2, 12, 0), "Friday"],
    [new Date(2026, 9, 1, 12, 0), "Oct 1"],
    [new Date(2026, 8, 20, 12, 0), "Sep 20"],
    [new Date(2025, 4, 14, 12, 0), "May 14, 2025"],
  ])("dates %s as %s", (date, label) => {
    expect(tileDate(date, now)).toBe(label);
  });

  test("keeps undated creations apart", () => {
    expect(tileDate(null, now)).toBe("Undated");
  });
});

describe("model display names", () => {
  test.each([
    ["krea2-turbo", "Krea2 Turbo"],
    ["tts-1", "TTS 1"],
    ["gpt-image-1", "GPT Image 1"],
    ["ace_step.xl", "ACE Step XL"],
  ])("prettifies %s as %s", (id, name) => {
    expect(prettifyModelId(id)).toBe(name);
  });

  test("prefers the catalog title, then a capability's recommended name, then the prettified id", () => {
    const catalog = {
      aliases: [],
      models: [{ id: "flux-dev", title: "Flux Dev", display_name: "flux-dev" }],
    };
    const capabilities = [
      {
        recommended_model: "ACE-Step XL Turbo",
        use_name: "music",
        use_names: ["song"],
      },
    ];
    expect(modelDisplayName("flux-dev", catalog, capabilities)).toBe(
      "Flux Dev",
    );
    expect(modelDisplayName("song", catalog, capabilities)).toBe(
      "ACE-Step XL Turbo",
    );
    expect(modelDisplayName("music", catalog, capabilities)).toBe(
      "ACE-Step XL Turbo",
    );
    expect(modelDisplayName("tts-1", catalog, capabilities)).toBe("TTS 1");
  });

  test("does not treat a display_name equal to the raw id as a name", () => {
    expect(
      modelDisplayName("krea2-turbo", {
        models: [{ id: "krea2-turbo", title: "", display_name: "krea2-turbo" }],
      }),
    ).toBe("Krea2 Turbo");
  });
});

describe("inProgressMedia", () => {
  test("keeps only running media requests and reads their kind and start", () => {
    expect(
      inProgressMedia([
        {
          request_id: "a",
          status: "running",
          intent: "image.generate",
          accepted_at_ms: 1_000_000,
        },
        {
          request_id: "b",
          status: "queued",
          path: "/openai/v1/videos",
          _stored_at: 50,
        },
        { request_id: "c", status: "pending", path: "/openai/v1/audio/speech" },
        { request_id: "d", status: "succeeded", intent: "image.generate" },
        { request_id: "e", status: "running", intent: "chat.completions" },
      ]),
    ).toEqual([
      { id: "a", kind: "image", startedAt: 1000 },
      { id: "b", kind: "video", startedAt: 50 },
      { id: "c", kind: "audio", startedAt: null },
    ]);
  });
});
