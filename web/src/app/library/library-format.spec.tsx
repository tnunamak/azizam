import { describe, expect, test } from "vitest";

import {
  inProgressMedia,
  modelDisplayName,
  prettifyModelId,
  weekLabel,
} from "./library-format";

// Thursday 8 October 2026, mid-afternoon local time. Its week starts Monday 5 October.
const now = new Date(2026, 9, 8, 15, 0);

describe("weekLabel", () => {
  test.each([
    [new Date(2026, 9, 5, 0, 0), "This week"],
    [new Date(2026, 9, 8, 9, 0), "This week"],
    [new Date(2026, 9, 4, 23, 59), "Last week"],
    [new Date(2026, 8, 28, 0, 0), "Last week"],
    [new Date(2026, 8, 27, 23, 59), "Week of Sep 21"],
    [new Date(2026, 8, 20, 12, 0), "Week of Sep 14"],
    [new Date(2026, 0, 1, 12, 0), "Week of Dec 29, 2025"],
    [new Date(2025, 4, 14, 12, 0), "Week of May 12, 2025"],
  ])("groups %s as %s", (date, label) => {
    expect(weekLabel(date, now)).toBe(label);
  });

  test("never groups by month alone", () => {
    for (let day = 0; day < 400; day += 3) {
      const label = weekLabel(new Date(2026, 9, 8 - day, 12), now);
      expect(label).toMatch(/^(This week|Last week|Week of [A-Z][a-z]{2} \d{1,2}(, \d{4})?)$/);
    }
  });

  test("keeps undated creations apart", () => {
    expect(weekLabel(null, now)).toBe("Undated");
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
      { recommended_model: "ACE-Step XL Turbo", use_name: "music", use_names: ["song"] },
    ];
    expect(modelDisplayName("flux-dev", catalog, capabilities)).toBe("Flux Dev");
    expect(modelDisplayName("song", catalog, capabilities)).toBe("ACE-Step XL Turbo");
    expect(modelDisplayName("music", catalog, capabilities)).toBe("ACE-Step XL Turbo");
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
        { request_id: "a", status: "running", intent: "image.generate", accepted_at_ms: 1_000_000 },
        { request_id: "b", status: "queued", path: "/openai/v1/videos", _stored_at: 50 },
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
