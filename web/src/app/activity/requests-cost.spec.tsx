import { afterEach, expect, test, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Requests } from "@/app/activity/requests";

for (const method of ["hasPointerCapture", "setPointerCapture", "releasePointerCapture", "scrollIntoView"] as const)
  Object.defineProperty(HTMLElement.prototype, method, { configurable: true, value: () => false });

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function mountReceipt(receipt: Record<string, unknown>) {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => ({
    ok: true,
    json: async () => String(input).startsWith("/admin/api/activity/requests")
      ? { records: [{
          person_label: "Mira", app_label: "Notes", created_at: 1,
          receipt: { request_id: "cost-case", intent: "chat", status: "success", latency_ms: 125, ...receipt },
        }], people: [], next_cursor: null }
      : {},
  })));
  render(<QueryClientProvider client={new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })}><Requests /></QueryClientProvider>);
}

test.each([
  { name: "reported amount", receipt: { cost_usd: 0.0123 }, expected: "$0.0123" },
  { name: "local request", receipt: { served_locally: true }, expected: "Local · no charge" },
  { name: "remote unknown cost", receipt: { remote: true }, expected: "Cost not reported by the provider" },
  { name: "unrecorded cost", receipt: {}, expected: "Cost not recorded" },
])("Activity shows $name in the row and opened receipt", async ({ receipt, expected }) => {
  mountReceipt(receipt);
  const row = await screen.findByRole("button", { name: "Request details: Chat by Mira" });
  expect(within(row).getByText(expected, { exact: true })).toBeVisible();
  expect(row).toHaveTextContent("125 ms");
  if (!("cost_usd" in receipt)) expect(row.textContent).not.toContain("$0");
  await userEvent.click(row);
  const sheet = within(screen.getByRole("dialog"));
  const spend = sheet.getByText("Spend", { exact: true }).nextElementSibling!;
  expect(spend).toHaveTextContent(expected);
  if (!("cost_usd" in receipt)) expect(spend.textContent).not.toContain("$0");
});
