import { describe, expect, test } from "vitest";
import { formatTimestamp } from "@/lib/format";

import {
  dayMarks,
  inProgressMedia,
  modelDisplayName,
  prettifyModelId,
  tileDate,
  captionTitle,
  thumbnailSize,
} from "./library-format";

// Thursday 8 October 2026, mid-afternoon local time.
const now = new Date(2026, 9, 8, 15, 0);

describe("thumbnailSize", () => {
  test.each([
    [1536, 1024, 480, 320],
    [1024, 1536, 320, 480],
    [480, 480, 480, 480],
    [128, 64, 128, 64],
    [8192, 1, 480, 1],
    [null, null, 480, 360],
    [0, 1024, 480, 360],
    [-1, 1024, 480, 360],
    [Infinity, 1024, 480, 360],
    [1536, NaN, 480, 360],
  ])("sizes %s × %s within the thumbnail bounds", (width, height, w, h) => {
    expect(thumbnailSize(width, height)).toEqual({ width: w, height: h });
  });
});

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

describe("day marks", () => {
  test("label the first creation of each day and keep the order", () => {
    const dates = [
      new Date(2026, 9, 8, 14, 0),
      new Date(2026, 9, 8, 9, 0),
      new Date(2026, 9, 7, 9, 0),
      new Date(2026, 9, 5, 9, 0),
      new Date(2026, 9, 5, 8, 0),
      null,
      new Date(2026, 8, 12, 9, 0),
    ];
    expect(dayMarks(dates, now)).toEqual([
      "Today",
      null,
      "Yesterday",
      "Monday",
      null,
      "Undated",
      "Sep 12",
    ]);
    expect(dayMarks([], now)).toEqual([]);
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
      models: [{ id: "flux2-klein-4b", title: "Flux2 Klein 4B", display_name: "flux2-klein-4b" }],
    };
    const capabilities = [
      {
        recommended_model: "ACE-Step XL Turbo",
        use_name: "music",
        use_names: ["song"],
      },
    ];
    expect(modelDisplayName("flux2-klein-4b", catalog, capabilities)).toBe(
      "Flux2 Klein 4B",
    );
    expect(modelDisplayName("song", catalog, capabilities)).toBe("Song");
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

describe("captionTitle", () => {
  test.each([
    [
      "An overhead photograph of a raised garden bed with ripe tomatoes, gloves and a trowel.",
      "Raised garden bed with ripe tomatoes…",
    ],
    [
      "A golden retriever asleep on a knit rug",
      "Golden retriever asleep on a knit rug",
    ],
    [
      "A cozy reading corner by a rainy window. Warm light.",
      "Cozy reading corner by a rainy window",
    ],
    [
      "Basil, rosemary and thyme on a sunny windowsill",
      "Basil, rosemary and thyme on a sunny…",
    ],
    [
      // Never ends on a joining word.
      "Basil, rosemary and thyme on a windowsill",
      "Basil, rosemary and thyme…",
    ],
    ["", "Image"],
    [null, "Image"],
  ])("captions %s as %s", (prompt, title) => {
    expect(captionTitle(prompt, "Image")).toBe(title);
  });
});

describe("shared timestamp presentation", () => {
  const nowMs = Date.parse("2026-10-08T15:00:00.000Z");
  const options = { nowMs, locale: "en-US", timeZone: "UTC" };
  test.each([
    [-1_000, "1 second ago"], [-60_000, "1 minute ago"],
    [-3_600_000, "1 hour ago"], [-86_399_999, "23 hours ago"],
    [60_000, "in 1 minute"], [0, "now"],
  ])("renders recent offset %s relatively", (offset, label) => {
    const date = new Date(nowMs + offset);
    expect(formatTimestamp(date, options)).toEqual({
      children: label, title: date.toISOString(), dateTime: date.toISOString(),
    });
  });
  test.each([-86_400_000, 86_400_000])("uses absolute time at the 24-hour boundary %s", (offset) => {
    const date = new Date(nowMs + offset);
    expect(formatTimestamp(date, options).children).toBe(
      new Intl.DateTimeFormat("en-US", {
        year: "numeric", month: "short", day: "numeric",
        hour: "numeric", minute: "2-digit", timeZoneName: "short", timeZone: "UTC",
      }).format(date),
    );
  });
  test("honors locale for relative labels", () => {
    expect(formatTimestamp(new Date(nowMs - 3_600_000), { ...options, locale: "fr-FR" }).children)
      .toBe(new Intl.RelativeTimeFormat("fr-FR", { numeric: "auto" }).format(-1, "hour"));
  });
  test.each(["UTC", "America/Los_Angeles", "Asia/Tokyo"])("honors locale and %s for absolute labels", (timeZone) => {
    const date = new Date("2026-10-01T01:30:00.123Z");
    const formatted = formatTimestamp(date, { ...options, locale: "de-DE", timeZone });
    expect(formatted.children).toBe(new Intl.DateTimeFormat("de-DE", {
      year: "numeric", month: "short", day: "numeric",
      hour: "numeric", minute: "2-digit", timeZoneName: "short", timeZone,
    }).format(date));
    expect(formatted.title).toBe("2026-10-01T01:30:00.123Z");
  });
  test("normalizes seconds, milliseconds and offset ISO inputs to the same exact instant", () => {
    const expected = formatTimestamp("2026-10-08T14:00:00.123Z", options);
    expect(formatTimestamp((nowMs - 3_600_000 + 123) / 1000, options)).toEqual(expected);
    expect(formatTimestamp(nowMs - 3_600_000 + 123, options)).toEqual(expected);
    expect(formatTimestamp("2026-10-08T16:00:00.123+02:00", options)).toEqual(expected);
  });
  test.each([null, undefined, "", "invalid", NaN, Infinity, 1e20, new Date(NaN)])
    ("handles missing or invalid timestamp %s without an invalid tooltip", (input) => {
      expect(formatTimestamp(input, options)).toEqual({ children: "Time not recorded" });
    });
});
