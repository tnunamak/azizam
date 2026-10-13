import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StatusView } from "./status-view";
import { productApi, type MemberStatus as Snapshot } from "@/lib/product-api";
import * as session from "@/components/shell/session";

// Own file: the module registry is per file, so the load count cannot depend on
// which case ran first (the gate shuffles case order).
const ownerModule = vi.hoisted(() => ({ loads: 0 }));
vi.mock("./status-client", () => {
  ownerModule.loads += 1;
  return { StatusClient: () => <p>Owner status</p> };
});
vi.mock("@/app/_home/home-api", () => ({
  homeApi: {
    home: async () => ({ authenticated: true, capabilities: [], aliases: [], receipts: [], apps: [] }),
  },
}));
vi.mock("next/link", () => ({
  default: ({ href, children, ...props }: React.ComponentProps<"a">) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

const snapshot: Snapshot = {
  scope: "member",
  gateway: { owner_name: null },
  status: {
    state: "unknown",
    reason_code: "unknown",
    affected_capability_ids: [],
    counts: { total: 0, available: 0, affected: 0 },
  },
  capabilities: [],
  receipts: [],
  help: "Ask the owner if an allowed capability is unavailable.",
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("does not load the owner status module for a member", async () => {
  vi.spyOn(session, "useViewer").mockReturnValue({ admin: false } as ReturnType<typeof session.useViewer>);
  vi.spyOn(productApi, "status").mockResolvedValue(snapshot);
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}><StatusView /></QueryClientProvider>);
  await screen.findByRole("heading", { name: "Your latest requests" }).catch(() => undefined);
  expect(ownerModule.loads).toBe(0);
});
