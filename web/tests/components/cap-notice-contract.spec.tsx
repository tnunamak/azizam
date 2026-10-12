import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { CapNotice } from "@/components/product/cap-notice";

vi.mock("@/lib/use-owner-name", () => ({
  useOwnerName: () => "Robin",
  askOwner: (name: string | null) =>
    name ? "Ask " + name : "Ask the gateway owner",
}));
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

it.each([
  ["2026-12-31T23:59:59Z", "2027-01-01T00:00:00Z"],
  ["2028-02-29T12:00:00Z", "2028-03-01T00:00:00Z"],
  ["2026-04-30T23:59:59Z", "2026-05-01T00:00:00Z"],
])("shows the UTC next-month reset at %s", (now, reset) => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(now));
  const expectedDate = new Date(reset).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
  render(
    <CapNotice
      warning="Monthly spending allowance reached: fixture-owner-detail"
      at={Date.parse("2026-01-15T12:00:00Z") / 1000}
      owner={false}
    />,
  );
  expect(screen.getByRole("heading", { name: "Paid models are paused" })).toBeVisible();
  expect(screen.getByText(
    "The gateway reported that the monthly limit for paid AI services was reached. " +
    "The monthly limit resets on " + expectedDate + ". " +
    "Ask Robin if you need paid models sooner.",
  )).toBeVisible();
  expect(screen.queryByText(/fixture-owner-detail/)).toBeNull();
  expect(screen.queryByRole("link", { name: "Open Status" })).toBeNull();
});

it("gives the owner the original warning and Status action", () => {
  render(
    <CapNotice
      warning="Daily spending allowance reached: fixture-owner-detail"
      at={Date.now() / 1000}
      owner
    />,
  );
  expect(screen.getByRole("heading", {
    name: "A spending limit refused paid requests",
  })).toBeVisible();
  expect(screen.getByText(/fixture-owner-detail/)).toHaveTextContent(
    "The daily limit counts the last 24 hours, so requests can work again within a day.",
  );
  expect(screen.getByRole("link", { name: "Open Status" }))
    .toHaveAttribute("href", "/status/");
  expect(screen.queryByText(/Ask Robin/)).toBeNull();
});

it("gives a member the rolling-day explanation without owner diagnostics", () => {
  render(
    <CapNotice
      warning="Daily spending allowance reached: fixture-owner-detail"
      at={Date.now() / 1000}
      owner={false}
    />,
  );
  expect(screen.getByText(
    "The gateway reported that the daily limit for paid AI services was reached. " +
    "The daily limit counts the last 24 hours, so requests can work again within a day. " +
    "Ask Robin if you need paid models sooner.",
  )).toBeVisible();
  expect(screen.queryByText(/fixture-owner-detail/)).toBeNull();
  expect(screen.queryByText(/monthly limit resets/)).toBeNull();
});
