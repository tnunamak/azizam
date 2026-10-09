import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
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
function renderUsage(
  summary: Record<string, unknown>,
  fetchSummary?: () => Promise<Response>,
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  vi.stubGlobal(
    "fetch",
    vi.fn(fetchSummary ?? (async () => Response.json({ requests: 4, ...summary }))),
  );
  const view = render(
    <ViewerContext.Provider
      value={{
        authenticated: true,
        name: "Owner",
        email: null,
        role: "owner",
        admin: true,
      }}
    >
      <QueryClientProvider client={client}>
        <UsagePage />
      </QueryClientProvider>
    </ViewerContext.Provider>,
  );
  return { ...view, client };
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
    expect(screen.getByText("Spend not recorded")).toBeVisible();
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

test.each([
  { emails: ["first@example.com", "second@example.com"], suffixes: ["first", "second"] },
  { emails: [undefined, undefined], suffixes: ["#1", "#2"] },
])("USAGE-03 duplicate explicit names have stable accessible labels and retain filter IDs: $suffixes", async ({ emails, suffixes }) => {
  const people = [
    { id: "person-002", name: "Mira", email: emails[1] },
    { id: "person-001", name: "Mira", email: emails[0] },
    { id: "person-003", name: "Azizam" },
  ];
  renderUsage({ people });
  await screen.findByText("Requests");
  const user = userEvent.setup();
  const labels = suffixes.map((suffix) => `Mira · ${suffix}`);
  for (const [index, label] of labels.entries()) {
    const filter = screen.getByRole("combobox", { name: "Person" });
    filter.focus();
    await user.keyboard("{Enter}");
    const option = await screen.findByRole("option", { name: label });
    expect(option).toHaveAccessibleName(label);
    expect(screen.getByRole("option", { name: "Azizam" })).toBeVisible();
    expect(screen.getAllByRole("option").map((item) => item.textContent).join(" ")).not.toContain("@");
    expect(screen.getByRole("option", { name: labels[1 - index] })).toBeVisible();
    // A different response order must not change which person a label identifies.
    people.reverse();
    vi.mocked(fetch).mockClear();
    await user.click(option);
    expect(
      vi.mocked(fetch).mock.calls.some(([url]) =>
        new URL(String(url), "https://azizam.test").searchParams.get("person") === `person-00${index + 1}`,
      ),
    ).toBe(true);
    expect(await screen.findByRole("combobox", { name: "Person" })).toHaveTextContent(label);
  }
});

test.each([undefined, null])("UA-01 labels an unrecorded receipt cost in summary and details: %s", async (cost) => {
  renderUsage({
    receipts: [
      { request_id: "unknown", capability: "chat", cost_usd: cost },
      { request_id: "zero", capability: "image", cost_usd: 0 },
      { request_id: "local", capability: "video", cost_usd: cost, remote: false },
    ],
    by_model: [{ model: "unmeasured-model", capability: "chat", requests: 1 }],
  });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "View recent requests" }));
  const unknown = screen.getByRole("button", { name: "Receipt details for Chat request" });
  expect(within(unknown).getByText("Cost unknown")).toBeInTheDocument();
  const zero = screen.getByRole("button", { name: "Receipt details for Images request" });
  expect(within(zero).queryByText("Cost unknown")).not.toBeInTheDocument();
  expect(within(zero).getByText("$0.00")).toBeInTheDocument();
  const local = screen.getByRole("button", { name: "Receipt details for Video request" });
  expect(within(local).getByText("No provider charge")).toBeInTheDocument();
  // Model rows carry no locality, so an unmeasured one stays blank.
  expect(screen.getAllByText("Cost unknown")).toHaveLength(1);
  await user.click(unknown);
  const sheet = screen.getByRole("dialog");
  expect(within(sheet).getByText("Cost")).toBeVisible();
  expect(within(sheet).getByText("Cost unknown")).toBeVisible();
});


function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

test("Usage announces loading with the shared Busy status and no invented totals", async () => {
  const pending = deferred<Response>();
  renderUsage({}, () => pending.promise);
  expect(screen.getByRole("status")).toHaveTextContent("Busy · Loading usage");
  expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
  expect(screen.queryByText("No requests in this period")).not.toBeInTheDocument();
  await act(async () => pending.resolve(Response.json({ requests: 0 })));
  expect(await screen.findByText("No requests in this period")).toBeVisible();
  expect(screen.getByRole("link", { name: "Connect an app" })).toHaveAttribute("href", "/ui/apps/");
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});

test("Usage reports an Unknown load failure with a shared callout and retry", async () => {
  const retry = deferred<Response>();
  let calls = 0;
  renderUsage({}, () => ++calls === 1 ? Promise.reject(new Error("offline")) : retry.promise);
  const alert = await screen.findByRole("alert");
  expect(within(alert).getByRole("heading", { name: "Usage could not be loaded" })).toBeVisible();
  expect(screen.getByRole("status")).toHaveTextContent("Unknown · Usage unavailable");
  expect(screen.queryByText("No requests in this period")).not.toBeInTheDocument();
  await userEvent.setup().click(within(alert).getByRole("button", { name: "Retry usage" }));
  expect(screen.getByRole("status")).toHaveTextContent("Busy · Loading usage");
  await act(async () => retry.resolve(Response.json({ requests: 4, remote_spend: 1.25 })));
  expect(await screen.findByText("$1.25")).toBeVisible();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

test.each([0, 4])("Usage keeps %i cached requests through refresh failure and retry", async (requests) => {
  const refresh = deferred<Response>();
  const retry = deferred<Response>();
  let calls = 0;
  const { client } = renderUsage({}, () => {
    calls += 1;
    if (calls === 1) return Promise.resolve(Response.json({ requests, remote_spend: null }));
    return calls === 2 ? refresh.promise : retry.promise;
  });
  const cachedLabel = requests ? "Spend not recorded" : "No requests in this period";
  expect(await screen.findByText(cachedLabel)).toBeVisible();
  act(() => { void client.invalidateQueries({ queryKey: ["usage"] }); });
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Busy · Refreshing usage"));
  expect(screen.getByText(cachedLabel)).toBeVisible();
  await act(async () => refresh.reject(new Error("refresh unavailable")));
  expect(await screen.findByRole("heading", { name: "Usage could not be refreshed" })).toBeVisible();
  expect(screen.getByRole("status")).toHaveTextContent("Degraded · Usage refresh failed");
  expect(screen.getByText(cachedLabel)).toBeVisible();
  expect(screen.getByText("Showing the last loaded usage. New requests and spend may be missing.")).toBeVisible();
  expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  await userEvent.setup().click(screen.getByRole("button", { name: "Retry usage" }));
  expect(screen.getByRole("button", { name: "Retrying…" })).toBeDisabled();
  expect(screen.getByText(cachedLabel)).toBeVisible();
  await act(async () => retry.resolve(Response.json({ requests: 5, remote_spend: 2.5 })));
  expect(await screen.findByText("$2.50")).toBeVisible();
  expect(screen.queryByRole("heading", { name: "Usage could not be refreshed" })).not.toBeInTheDocument();
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});

test.each([undefined, null, "", "   ", false])("Usage never formats unrecorded spend as zero: %s", async (spend) => {
  renderUsage({ remote_spend: spend, remote_spend_available: true });
  expect(await screen.findByText("Spend not recorded")).toBeVisible();
  expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
});

test("Usage uses shared empty panels for missing daily and model breakdowns", async () => {
  renderUsage({});
  for (const title of [
    "Daily request history will appear here.",
    "Model totals will appear here after your first request.",
  ]) {
    expect((await screen.findByText(title)).closest(".border-dashed")).toBeInTheDocument();
  }
});
