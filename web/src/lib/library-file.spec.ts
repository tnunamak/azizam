import { expect, test } from "vitest";
import { groupLibraryByDay, libraryDayLabel } from "./library-file";

const now = new Date(2026, 9, 26, 12);
const stamp = (day: number, hour = 12) => new Date(2026, 9, day, hour).getTime();

test("day labels use local calendar days and short dates beyond the last six days", () => {
  expect(libraryDayLabel(stamp(26), now)).toBe("Today");
  expect(libraryDayLabel(stamp(25), now)).toBe("Yesterday");
  expect(libraryDayLabel(stamp(24), now)).toBe("Saturday");
  expect(libraryDayLabel(stamp(20), now)).toBe("Tuesday");
  expect(libraryDayLabel(stamp(19), now)).toBe("Oct 19");
  expect(libraryDayLabel(new Date(2025, 9, 20).getTime(), now)).toBe("Oct 20, 2025");
  expect(libraryDayLabel(stamp(27), now)).toBe("Oct 27");
});

test("day labels read seconds, milliseconds and date strings alike", () => {
  for (const value of [stamp(25), stamp(25) / 1000, new Date(stamp(25)).toISOString()]) {
    expect(libraryDayLabel(value, now)).toBe("Yesterday");
  }
  for (const value of [undefined, null, "", "invalid", NaN, Infinity]) {
    expect(libraryDayLabel(value, now)).toBeNull();
  }
});

test("calendar boundaries do not depend on elapsed hours, including daylight-saving days", () => {
  expect(libraryDayLabel(stamp(25, 23), new Date(2026, 9, 26, 0, 1))).toBe("Yesterday");
  expect(libraryDayLabel(new Date(2026, 2, 8, 0).getTime(), new Date(2026, 2, 9, 23))).toBe("Yesterday");
  expect(libraryDayLabel(new Date(2026, 10, 1, 0).getTime(), new Date(2026, 10, 2, 23))).toBe("Yesterday");
});

test("groups preserve incoming dated order and combine only adjacent day runs", () => {
  const assets = [
    { id: "a", created_at: stamp(25) },
    { id: "b", created_at: stamp(25, 1) },
    { id: "c", created_at: stamp(26) },
    { id: "d", created_at: stamp(25) },
  ];
  const groups = groupLibraryByDay(assets, now);
  expect(groups.map(({ key, label, items }) => ({ key, label, ids: items.map((item) => item.id) }))).toEqual([
    { key: "2026-10-25", label: "Yesterday", ids: ["a", "b"] },
    { key: "2026-10-26", label: "Today", ids: ["c"] },
    { key: "2026-10-25", label: "Yesterday", ids: ["d"] },
  ]);
  expect(groups[0].items[0]).toBe(assets[0]);
  expect(assets.map((item) => item.id)).toEqual(["a", "b", "c", "d"]);
});

test("unknown dates form one final Undated group without joining separated dated runs", () => {
  const assets = [
    { id: "unknown", created_at: null },
    { id: "a", created_at: stamp(26) },
    { id: "invalid", created_at: "bad" },
    { id: "b", created_at: stamp(26) },
  ];
  expect(groupLibraryByDay(assets, now).map(({ key, label, items }) => ({
    key, label, ids: items.map((item) => item.id),
  }))).toEqual([
    { key: "2026-10-26", label: "Today", ids: ["a"] },
    { key: "2026-10-26", label: "Today", ids: ["b"] },
    { key: "undated", label: "Undated", ids: ["unknown", "invalid"] },
  ]);
  expect(groupLibraryByDay([], now)).toEqual([]);
  expect(groupLibraryByDay([{ created_at: null }], now)[0].label).toBe("Undated");
});
