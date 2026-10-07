import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { ViewerContext } from "@/components/shell/session";
import UsagePage from "./page";

vi.mock("recharts", () => ({
  ResponsiveContainer: () => null,
  Bar: () => null,
  BarChart: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
  XAxis: () => null,
  YAxis: () => null,
}));
function renderUsage(summary: Record<string, unknown>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => Response.json({ requests: 4, ...summary })),
  );
  return render(
    <ViewerContext.Provider
      value={{
        authenticated: true,
        name: "Owner",
        email: null,
        role: "owner",
        admin: true,
      }}
    >
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <UsagePage />
      </QueryClientProvider>
    </ViewerContext.Provider>,
  );
}
HTMLElement.prototype.scrollIntoView = vi.fn();
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test.each([0, 4])(
  "USAGE-01 keeps unknown paid spend visible with %i settled paid requests",
  async (paidRequests) => {
    renderUsage({
      paid_requests: paidRequests,
      unknown_paid_requests: 4,
      remote_spend: null,
      spend_measured_requests: 0,
      spend_total_requests: 4,
    });
    expect(await screen.findByText("Remote spend")).toBeVisible();
    expect(screen.getByText("Unavailable")).toBeVisible();
    expect(screen.getByText(/Cost measured for 0 of 4 requests/)).toBeVisible();
    expect(screen.getByText(/4 paid requests have unknown cost/)).toBeVisible();
  },
);
test.each([
  {
    unknown: 2,
    measured: 2,
    label: "Remote spend · partial",
    detail: /2 paid requests have unknown cost/,
  },
  {
    unknown: 0,
    measured: 4,
    label: "Remote spend",
    detail: /Cost measured for 4 of 4 requests/,
  },
])(
  "USAGE-01 identifies completeness: $label",
  async ({ unknown, measured, label, detail }) => {
    renderUsage({
      paid_requests: 4,
      unknown_paid_requests: unknown,
      remote_spend: 1.25,
      spend_measured_requests: measured,
      spend_total_requests: 4,
    });
    expect(await screen.findByText(label)).toBeVisible();
    expect(screen.getByText("$1.25")).toBeVisible();
    expect(screen.getByText(detail)).toBeVisible();
  },
);
test("USAGE-01 does not claim complete coverage without measurement counts", async () => {
  renderUsage({ paid_requests: 4, remote_spend: 0 });
  expect(
    await screen.findByText("Remote spend · coverage unknown"),
  ).toBeVisible();
  expect(screen.getByText("$0.00")).toBeVisible();
  expect(screen.getByText(/Measurement coverage is unavailable/)).toBeVisible();
});
test("USAGE-02 chart table includes each active capability and daily total", async () => {
  renderUsage({
    by_day: [
      { date: "2026-10-01", chat: 2, image: 1 },
      { date: "2026-10-02", image: 4 },
    ],
  });
  const table = await screen.findByRole("table", {
    name: "Requests by day and capability",
  });
  expect(
    within(table)
      .getAllByRole("columnheader")
      .map((cell) => cell.textContent),
  ).toEqual(["Date", "Chat", "Images", "Total"]);
  const rows = within(table).getAllByRole("row").slice(1);
  expect(
    rows.map((row) =>
      within(row)
        .getAllByRole("cell")
        .map((cell) => cell.textContent),
    ),
  ).toEqual([
    ["2", "1", "3"],
    ["0", "4", "4"],
  ]);
  for (const row of rows) {
    const counts = within(row)
      .getAllByRole("cell")
      .map((cell) => Number(cell.textContent));
    expect(counts[0] + counts[1]).toBe(counts[2]);
  }
});
test("USAGE-03 anonymous people have distinct safe labels and retain filter IDs", async () => {
  renderUsage({
    people: [
      { id: "person-001", label: "one@example.com" },
      { id: "person-002", label: "two@example.com" },
      { id: "person-003" },
      { id: "person-004", label: "Mira" },
    ],
  });
  await screen.findByText("Requests");
  const user = userEvent.setup();
  screen.getByRole("combobox", { name: "Person" }).focus();
  await user.keyboard("{Enter}");
  const options = await screen.findAllByRole("option");
  const labels = options.map((option) => option.textContent ?? "");
  const anonymous = labels.filter((label) => label.startsWith("Person · "));
  expect(anonymous).toHaveLength(3);
  expect(new Set(anonymous).size).toBe(3);
  expect(labels.join(" ")).not.toContain("@example.com");
  expect(labels).toContain("Mira");
  await user.click(
    options.find((option) => option.textContent === anonymous[1])!,
  );
  expect(
    vi
      .mocked(fetch)
      .mock.calls.some(([url]) => String(url).includes("person=person-002")),
  ).toBe(true);
});
