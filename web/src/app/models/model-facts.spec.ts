import { describe, expect, it } from "vitest";
import type { CatalogRow } from "@/lib/product-api";
import { modelDates, modelSource } from "@/app/models/model-facts";

describe("modelDates", () => {
  it("returns all three supplied dates in label order", () => {
    expect(modelDates({ updated_at: "2026-03-03", released_at: "2026-01-01", added_at: "2026-02-02" })).toEqual([
      { label: "Released", iso: "2026-01-01T00:00:00.000Z", calendarDay: true },
      { label: "Added here", iso: "2026-02-02T00:00:00.000Z", calendarDay: true },
      { label: "Updated", iso: "2026-03-03T00:00:00.000Z", calendarDay: true },
    ]);
  });
  it("returns only Added here without guessing other dates", () => {
    expect(modelDates({ name: "model-2025-01-01", added_at: "2026-02-02" })).toEqual([
      { label: "Added here", iso: "2026-02-02T00:00:00.000Z", calendarDay: true },
    ]);
  });
  it("skips unparseable and invalid calendar dates", () => {
    expect(modelDates({ released_at: "not a date", added_at: "2026-02-30", updated_at: "2026-03-03" })).toEqual([
      { label: "Updated", iso: "2026-03-03T00:00:00.000Z", calendarDay: true },
    ]);
  });
  it("ignores numbers, which carry no unit", () => {
    expect(modelDates({ released_at: 1704067200, added_at: 1704067200000, updated_at: 0 })).toEqual([]);
  });
  it("skips missing fields and non-date values without coercion", () => {
    expect(modelDates({})).toEqual([]);
    for (const value of [null, true, {}, [], "", "123", Infinity, NaN, 1e30]) {
      expect(modelDates({ released_at: value })).toEqual([]);
    }
  });
  it("normalizes explicit ISO offsets", () => {
    expect(modelDates({ released_at: "2024-01-01T01:00:00+01:00" })).toEqual([
      { label: "Released", iso: "2024-01-01T00:00:00.000Z", calendarDay: false },
    ]);
  });
  it("accepts a typed CatalogRow without new API fields", () => {
    // Existing required catalog fields are irrelevant to these helpers.
    const row = { name: "model-2026-01-01" } as CatalogRow;
    expect(modelDates(row)).toEqual([]);
    expect(modelSource(row)).toBeNull();
  });
});

describe("modelSource", () => {
  it("returns the supplied HTTPS or HTTP source string", () => {
    for (const source of ["https://example.test/models/a", "http://example.test/a"]) {
      expect(modelSource({ source_url: source })).toBe(source);
    }
  });
  it("rejects javascript and ftp protocols", () => {
    for (const source of ["javascript:alert(1)", "ftp://example.test/a"]) {
      expect(modelSource({ source_url: source })).toBeNull();
    }
  });
  it("returns null for missing, invalid and non-string sources", () => {
    expect(modelSource({})).toBeNull();
    for (const source of [null, 42, {}, "", "/models/a", "not a URL"]) {
      expect(modelSource({ source_url: source })).toBeNull();
    }
  });
});
