import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AccessSection } from "./access-section";

const mocks = vi.hoisted(() => ({ session: vi.fn() }));
vi.mock("@/lib/account-api", () => ({ accountApi: { session: mocks.session } }));

function renderSection() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <AccessSection />
    </QueryClientProvider>,
  );
}

const snapshot = (access_plan: unknown) => ({
  authenticated: true,
  user: { id: "person-1", name: "River", role: "member", access_plan },
  apps: [],
});

beforeEach(() => {
  mocks.session.mockReset();
});

afterEach(() => {
  cleanup();
});

describe("Access on your settings", () => {
  it("names the person's own plan, what it allows, and who sets it", async () => {
    mocks.session.mockResolvedValue(
      snapshot({ id: "plan-family", name: "Family", summary: "Chat and pictures" }),
    );
    renderSection();
    expect(screen.getByRole("heading", { name: "Access" })).toBeVisible();
    expect(await screen.findByText("Family")).toBeVisible();
    expect(screen.getByText("Chat and pictures")).toBeVisible();
    expect(
      screen.getByText("What you can use on this gateway. Your owner sets your access plan."),
    ).toBeVisible();
  });

  it("says no plan is assigned when the account has none", async () => {
    mocks.session.mockResolvedValue(snapshot(null));
    renderSection();
    expect(
      await screen.findByText("No access plan is assigned to your account. Ask the owner."),
    ).toBeVisible();
  });

  it("says the plan could not be confirmed when the account has no plan field", async () => {
    mocks.session.mockResolvedValue(snapshot(undefined));
    renderSection();
    expect(await screen.findByText("Your access plan could not be confirmed.")).toBeVisible();
  });

  it("says the plan could not be confirmed when the account fails to load", async () => {
    mocks.session.mockRejectedValue(new Error("offline"));
    renderSection();
    expect(await screen.findByText("Your access plan could not be confirmed.")).toBeVisible();
  });
});
