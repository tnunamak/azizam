// @vitest-environment jsdom
import { createElement } from "react";
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { RelativeTime } from "../components/product/relative-time";
import {
  formatAbsoluteUTC,
  formatRelativeTime,
  formatTimestamp,
  toDate,
} from "./format";

afterEach(cleanup);

describe("timestamp lower bound", () => {
  it.each([0, -1, 300, 999_999_999])("treats %s as a missing numeric timestamp", (value) => {
    expect(toDate(value)).toBeNull();
  });

  it.each([1_000_000_000, 1_700_000_000, 5_000_000_000])(
    "keeps %s as epoch seconds",
    (value) => {
      expect(toDate(value)?.getTime()).toBe(value * 1000);
    },
  );

  it.each([5_000_000_001, 1_700_000_000_000])(
    "keeps %s as epoch milliseconds",
    (value) => {
      expect(toDate(value)?.getTime()).toBe(value);
    },
  );

  it("preserves ISO strings and Date objects, including historical dates", () => {
    const iso = "2023-11-14T22:13:20.000Z";
    expect(toDate(iso)?.toISOString()).toBe(iso);
    const date = new Date(iso);
    expect(toDate(date)).toBe(date);
    const historical = new Date(0);
    expect(toDate(historical)).toBe(historical);
    expect(toDate("1970-01-01T00:00:00.000Z")?.getTime()).toBe(0);
  });

  it("uses existing missing-time formatter results for zero", () => {
    expect(formatRelativeTime(0, 1_700_000_000_000)).toBe("");
    expect(formatAbsoluteUTC(0)).toBe("");
    expect(formatTimestamp(0)).toEqual({ children: "Time not recorded" });
  });

  it("renders the RelativeTime fallback for zero", () => {
    const { container, getByText } = render(
      createElement(RelativeTime, { ts: 0, fallback: "Never" }),
    );
    expect(getByText("Never").textContent).toBe("Never");
    expect(container.querySelector("time")).toBeNull();
    expect(container.textContent).not.toContain("1970");
  });
});
