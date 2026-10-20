import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

const fetchLedger = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ adminFetch: fetchLedger }));

import { RetentionLedgerPanel } from "./retention-ledger";

afterEach(() => {
  cleanup();
  fetchLedger.mockReset();
});

it("loads only when opened and shows dated destination facts and server disclosure", async () => {
  const user = userEvent.setup();
  fetchLedger.mockResolvedValue({
    ledger: {
      fact_basis: "The facts saved for this request are no longer kept.",
      kinds: [
        {
          id: "library_prompt",
          purpose: "A Library prompt can be kept so you can find it again.",
          readers: "maker",
          reader_explanation: "The maker and holders of their personal App keys",
        },
      ],
      destinations: [
        { id: "removed", name: "Removed service", kind: "former_destination", facts: [] },
        {
          id: "local-server",
          name: "Local server",
          kind: "engine",
          address: "127.0.0.1:8188",
          reach: "on this computer",
          facts: [
            {
              id: "history",
              holds: "submitted graphs",
              where: "engine history",
              how_long: "until restart",
              default: "on",
              condition: null,
              reach: "anyone who can reach the port",
              control: "restart it",
              evidence_url: "https://example.com/history",
              checked: "2026-01-01",
              confidence: "documented",
              stale: true,
              source: "set by the owner",
              revision: "abc",
            },
          ],
        },
      ],
      never_save_text:
        "Azizam will not keep the request. The engine may keep its own history.",
    },
  });
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <RetentionLedgerPanel
        id="ledger"
        endpoint="/admin/api/settings/retention/ledger"
      />
    </QueryClientProvider>,
  );
  expect(fetchLedger).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "View ledger" }));
  expect(fetchLedger).toHaveBeenCalledExactlyOnceWith(
    "/admin/api/settings/retention/ledger",
  );
  const destination = await screen.findByRole("region", {
    name: "Local server",
  });
  expect(screen.getByText("Former destination")).toBeVisible();
  expect(screen.queryByText("former_destination")).not.toBeInTheDocument();
  expect(screen.getByText("The facts saved for this request are no longer kept.")).toBeVisible();
  expect(within(destination).getByText("submitted graphs")).toBeVisible();
  expect(
    within(destination).getByText(
      "This was last checked on 2026-01-01. Set by the owner.",
    ),
  ).toBeVisible();
  expect(
    within(destination).getByRole("link", { name: "Source" }),
  ).toHaveAttribute("href", "https://example.com/history");
  expect(screen.getByText("The maker and holders of their personal App keys", { exact: false })).toBeVisible();
  expect(
    screen.getByText(
      "Azizam will not keep the request. The engine may keep its own history.",
    ),
  ).toBeVisible();
});
