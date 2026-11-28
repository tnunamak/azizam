import { describe, expect, test } from "vitest";
import { EXPIRY_CUTOFF_MS, expiryNote } from "./library-expiry";

const cutoffS = EXPIRY_CUTOFF_MS / 1000;

describe("the note on a file the 24-hour expiry deleted", () => {
  test("the cutoff is 24 hours before the expiry was removed: 2026-10-10 11:59:31 CDT", () => {
    expect(new Date(EXPIRY_CUTOFF_MS).toISOString()).toBe("2026-10-10T16:59:31.000Z");
  });

  test("a file made one second before the cutoff was deleted; one made at the cutoff was not", () => {
    expect(expiryNote(cutoffS - 1, { locale: "en-US", timeZone: "UTC" })).toMatch(/^This file was deleted on or before /);
    expect(expiryNote(cutoffS, { locale: "en-US", timeZone: "UTC" })).toBeNull();
    expect(expiryNote(cutoffS + 3600, { locale: "en-US", timeZone: "UTC" })).toBeNull();
  });

  test("no known creation time makes no claim", () => {
    expect(expiryNote(null)).toBeNull();
    expect(expiryNote(undefined)).toBeNull();
    expect(expiryNote(Number.NaN)).toBeNull();
  });

  test.each([
    ["America/Chicago", "Oct 11, 11:59 AM CDT"],
    ["Asia/Tokyo", "Oct 12, 1:59 AM GMT+9"],
    ["UTC", "Oct 11, 4:59 PM UTC"],
  ])("the cutoff is written in the viewer's time zone: %s", (timeZone, written) => {
    expect(expiryNote(0, { locale: "en-US", timeZone })).toBe(
      `This file was deleted on or before ${written} and can’t be recovered.`,
    );
  });
});
