import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { CapNotice } from "@/components/product/cap-notice";
import { errorCopy } from "@/lib/error-copy";

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
    .toHaveAttribute("href", "/status");
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


const sharedErrorCases: Array<{
  label: string;
  error: unknown;
  role?: "member" | "owner" | "admin";
  message: string;
  recovery: "retry" | "sign-in" | "owner-help" | "refresh";
}> = [
  ...[
    "link_unavailable",
    "invite_unavailable",
    "invite_expired",
    "invite_revoked",
    "invite_consumed",
    "invite_used",
    "invite_not_found",
  ].map((code) => {
    const link = code === "link_unavailable" ? "link" : "invite link";
    return {
      label: code,
      error: { code, status: 401 },
      message: `This ${link} can no longer be used. Ask the gateway owner for a new ${link}.`,
      recovery: "owner-help" as const,
    };
  }),
  ...["account_disabled", "credential_revoked"].map((code) => ({
    label: code,
    error: { code, status: 401 },
    message: "Your access is no longer available. Ask the gateway owner to check your access.",
    recovery: "owner-help" as const,
  })),
  ...["session_expired", "authentication_required"].map((code) => ({
    label: code,
    error: { code },
    message: "Sign in to continue.",
    recovery: "sign-in" as const,
  })),
  {
    label: "HTTP 401",
    error: { status: 401 },
    message: "Sign in to continue.",
    recovery: "sign-in",
  },
  ...(["member", "owner", "admin"] as const).map((role) => ({
    label: `HTTP 403 for ${role}`,
    error: { status: 403 },
    role,
    message: role === "member"
      ? "Your account does not have permission for this action. Ask the gateway owner to check your access."
      : "This account does not have permission for this action. Sign in with an account that has access.",
    recovery: role === "member" ? "owner-help" as const : "sign-in" as const,
  })),
  ...[
    { label: "HTTP 412", error: { status: 412 } },
    { label: "generation_mismatch", error: { code: "generation_mismatch" } },
  ].map((input) => ({
    ...input,
    message: "The engine configuration changed. Refresh it before saving again.",
    recovery: "refresh" as const,
  })),
  {
    label: "HTTP 429",
    error: { status: 429 },
    message: "The gateway is limiting requests. Wait before trying again.",
    recovery: "retry",
  },
  {
    label: "registry_saved_not_applied",
    error: { code: "registry_saved_not_applied", status: 500 },
    message: "Your change is saved, but the gateway is still running the previous settings. Open Status and choose Apply again.",
    recovery: "retry",
  },
  ...[500, 503, 599].map((status) => ({
    label: `HTTP ${status}`,
    error: { status },
    message: "The gateway could not complete this request.",
    recovery: "retry" as const,
  })),
  ...[
    { label: "browser network TypeError", error: new TypeError("Failed to fetch") },
    { label: "network_unavailable", error: { code: "network_unavailable", status: 0 } },
  ].map((input) => ({
    ...input,
    message: "The gateway could not be reached. Check your connection.",
    recovery: "retry" as const,
  })),
  ...[
    { label: "unmapped HTTP 400", error: { status: 400 } },
    { label: "unknown error code", error: { code: "unknown_failure" } },
  ].map((input) => ({
    ...input,
    message: "The request could not be completed. Try again; if this continues, ask the gateway owner for help.",
    recovery: "retry" as const,
  })),
];

// These cases cover the shared helper independently of CapNotice rendering.
// The current 403 contract exposes permission recovery, not a plan-name field.
it.each(sharedErrorCases)("shared error copy: $label", ({ error, role, message, recovery }) => {
  expect(errorCopy(error, { role })).toMatchObject({ message, recovery });
});
