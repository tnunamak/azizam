import { describe, expect, it } from "vitest";
import { STALE_AFTER_MS, staleWords } from "@/app/activity/request-staleness";

const now = Date.parse("2026-11-18T15:00:00Z");
const old = now - 11 * 60_000;
const words = (time: number) =>
  "No update since " + new Date(time).toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  });

describe("staleWords", () => {
  it("uses a ten-minute threshold", () => {
    expect(STALE_AFTER_MS).toBe(10 * 60_000);
  });

  it("describes a running row eleven minutes old", () => {
    expect(staleWords("running", old, now)).toBe(words(old));
  });

  it("keeps a running row nine minutes old fresh", () => {
    expect(staleWords("running", now - 9 * 60_000, now)).toBeNull();
  });

  it("does not label an old succeeded row stale", () => {
    expect(staleWords("succeeded", old, now)).toBeNull();
  });

  it("never invents a missing or invalid update time", () => {
    for (const time of [undefined, null, "", "  ", "not-a-date", NaN, Infinity, -Infinity, 1e20]) {
      expect(staleWords("running", time, now)).toBeNull();
    }
  });

  it("accepts epoch seconds, milliseconds and ISO strings", () => {
    for (const time of [old / 1000, old, String(old / 1000), String(old), new Date(old).toISOString()]) {
      expect(staleWords("running", time, now)).toBe(words(old));
    }
  });

  it("only marks accepted, running and in_progress as in flight", () => {
    for (const status of ["accepted", "running", "in_progress"]) {
      expect(staleWords(status, old, now)).toBe(words(old));
    }
    for (const status of [undefined, "", "queued", "failed", "cancelled", "RUNNING"]) {
      expect(staleWords(status, old, now)).toBeNull();
    }
  });

  it("requires strictly more than ten minutes and ignores future times", () => {
    expect(staleWords("running", now - STALE_AFTER_MS, now)).toBeNull();
    expect(staleWords("running", now - STALE_AFTER_MS - 1, now)).toBe(words(now - STALE_AFTER_MS - 1));
    expect(staleWords("running", now + 60_000, now)).toBeNull();
  });

  it("ignores a non-finite current time", () => {
    expect(staleWords("running", old, NaN)).toBeNull();
    expect(staleWords("running", old, Infinity)).toBeNull();
  });
});
