import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CapNotice } from "@/components/product/cap-notice";
import { askOwner } from "@/lib/use-owner-name";

afterEach(() => vi.unstubAllGlobals());

function renderNotice(ownerName: string | null) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      Response.json({
        authenticated: true,
        user: { email: "robin@example.test", role: "member" },
        gateway: { owner_name: ownerName },
        capabilities: [],
        apps: [],
        receipts: [],
      }),
    ),
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <CapNotice
        warning="Monthly spending allowance reached"
        at={Date.now() / 1000}
        owner={false}
      />
    </QueryClientProvider>,
  );
}

describe("the gateway owner by name", () => {
  it("asks a person when the gateway knows their name, a role otherwise", () => {
    expect(askOwner("Sam")).toBe("Ask Sam");
    expect(askOwner(null)).toBe("Ask the gateway owner");
  });
  it("a member's spending-limit notice names the owner", async () => {
    renderNotice("Sam");
    expect(
      await screen.findByText(/Ask Sam if you need them sooner\./),
    ).toBeVisible();
  });
  it("falls back to the role when the gateway has no owner name", async () => {
    renderNotice(null);
    expect(
      await screen.findByText(/Ask the gateway owner if you need them sooner\./),
    ).toBeVisible();
  });
});
