import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RelativeTime } from "@/components/product/relative-time";
import { formatRelativeTime } from "@/lib/format";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const now = Date.parse("2026-10-03T12:00:00Z");
const day = 24 * 60 * 60 * 1000;

describe("calendar dates for older activity", () => {
  it("keeps relative time through 30 days and switches beyond that boundary", () => {
    expect(formatRelativeTime(now - 30 * day, now)).toBe("4w ago");
    expect(formatRelativeTime(now - 30 * day - 1, now)).toBe("Sep 2026");
    expect(formatRelativeTime("2025-10-03T12:00:00Z", now)).toBe("Oct 2025");
    expect(formatRelativeTime(now - 120000, now)).toBe("2m ago");
  });

  it("uses UTC month boundaries for strings, epoch seconds, and milliseconds", () => {
    const timestamp = "2025-10-01T00:30:00Z";
    const milliseconds = Date.parse(timestamp);
    for (const value of [timestamp, milliseconds, milliseconds / 1000]) {
      expect(formatRelativeTime(value, now)).toBe("Oct 2025");
    }
    expect(formatRelativeTime(now + 31 * day, now)).toBe("Nov 2026");
    expect(formatRelativeTime(undefined, now)).toBe("");
  });

  it("retains the precise absolute tooltip and machine-readable datetime", () => {
    vi.useFakeTimers();
    vi.setSystemTime(now);
    render(<RelativeTime ts="2025-10-03T12:00:00Z" />);
    const time = screen.getByText("Oct 2025");
    expect(time).toHaveAttribute("title", "2025-10-03 12:00:00 UTC");
    expect(time).toHaveAttribute("datetime", "2025-10-03T12:00:00.000Z");
  });
});
