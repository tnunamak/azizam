import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { Requests } from "@/app/activity/requests";
import UsagePage from "@/app/usage/page";
import { ViewerContext } from "@/components/shell/session";

vi.mock("recharts", () => ({
  ResponsiveContainer: () => null,
  Bar: () => null,
  BarChart: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
  XAxis: () => null,
  YAxis: () => null,
}));
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

for (const page of ["Activity", "Usage"] as const) {
  for (const scenario of ["ID-less selection", "disappearance"] as const) {
    test(`${page} handles receipt ${scenario}`, async () => {
      const initialReceipts: Record<string, string>[] =
        scenario === "ID-less selection"
          ? [
              { intent: "chat", capability: "chat", status: "succeeded" },
              { intent: "image", capability: "image", status: "failed" },
            ]
          : [
              {
                request_id: "req-disappear",
                intent: "chat",
                status: "succeeded",
              },
            ];
      let receipts = initialReceipts;
      vi.stubGlobal(
        "fetch",
        vi.fn(async () =>
          Response.json(
            page === "Activity"
              ? {
                  records: receipts.map((receipt) => ({
                    receipt,
                    person_label: "Owner",
                    app_label: "Try",
                  })),
                  next_cursor: null,
                  people: [],
                }
              : { requests: receipts.length, receipts },
          ),
        ),
      );
      const client = new QueryClient({
        defaultOptions: { queries: { retry: false } },
      });
      render(
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
            {page === "Activity" ? <Requests /> : <UsagePage />}
          </QueryClientProvider>
        </ViewerContext.Provider>,
      );
      if (page === "Usage")
        fireEvent.click(
          await screen.findByRole("button", { name: "View recent requests" }),
        );
      const rowName =
        page === "Activity" ? /Request details:/ : /Receipt details for/;
      const rows = await screen.findAllByRole("button", { name: rowName });
      fireEvent.click(rows[scenario === "ID-less selection" ? 1 : 0]);
      const dialog = await screen.findByRole("dialog");
      if (scenario === "ID-less selection") {
        expect(within(dialog).getAllByText(/Failed/).length).toBeGreaterThan(0);
        expect(within(dialog).queryByText("Completed")).toBeNull();
        // Fresh objects with the same fields must keep the same selection.
        receipts = initialReceipts.map((receipt) => ({ ...receipt }));
        await act(async () => {
          await client.invalidateQueries();
        });
        expect(
          within(screen.getByRole("dialog")).getAllByText(/Failed/).length,
        ).toBeGreaterThan(0);
      } else {
        receipts = [];
        await act(async () => {
          await client.invalidateQueries();
        });
        await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
        receipts = initialReceipts;
        await act(async () => {
          await client.invalidateQueries();
        });
        await screen.findByRole("button", { name: rowName, hidden: true });
        expect(screen.queryByRole("dialog")).toBeNull();
      }
      client.clear();
    });
  }
  test(`${page} updates an open receipt after refreshing the same request`, async () => {
    let status = "in_progress";
    let fail = false;
    const receipt = () => ({
      request_id: "req-refresh",
      status,
      intent: "chat",
      capability: "chat",
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        if (fail) throw new Error("Refresh failed");
        return Response.json(
          page === "Activity"
            ? {
                records: [
                  {
                    receipt: receipt(),
                    person_label: "Owner",
                    app_label: "Try",
                  },
                ],
                next_cursor: null,
                people: [],
              }
            : { requests: 1, receipts: [receipt()] },
        );
      }),
    );
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
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
          {page === "Activity" ? <Requests /> : <UsagePage />}
        </QueryClientProvider>
      </ViewerContext.Provider>,
    );
    if (page === "Usage")
      fireEvent.click(
        await screen.findByRole("button", { name: "View recent requests" }),
      );
    fireEvent.click(
      await screen.findByRole("button", {
        name: page === "Activity" ? /Request details:/ : /Receipt details for/,
      }),
    );
    expect(
      within(screen.getByRole("dialog")).getAllByText(/In progress/).length,
    ).toBeGreaterThan(0);
    status = "succeeded";
    await act(async () => {
      await client.invalidateQueries();
    });
    await waitFor(() =>
      expect(
        within(screen.getByRole("dialog")).getAllByText(/Completed/).length,
      ).toBeGreaterThan(0),
    );
    expect(
      within(screen.getByRole("dialog")).queryByText(/In progress/),
    ).toBeNull();
    fail = true;
    await act(async () => {
      await client.invalidateQueries();
    });
    await waitFor(() =>
      expect(
        within(screen.getByRole("dialog")).getAllByText(/Completed/).length,
      ).toBeGreaterThan(0),
    );
    client.clear();
  });
}

function renderUsageSummary(summary: Record<string, unknown>) {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json(summary)));
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
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
  return client;
}

test.each([
  { name: "absent counters with spend", counters: {}, spend: 2, unknown: true, none: false },
  { name: "absent counters with zero spend", counters: {}, spend: 0, unknown: true, none: false },
  { name: "missing paid counter", counters: { unknown_paid_requests: 0 }, spend: 0, unknown: true, none: false },
  { name: "missing unknown-cost counter", counters: { paid_requests: 0 }, spend: 0, unknown: true, none: false },
  { name: "explicit zero counters and spend", counters: { paid_requests: 0, unknown_paid_requests: 0 }, spend: 0, unknown: false, none: true },
  { name: "zero counters with recorded spend", counters: { paid_requests: 0, unknown_paid_requests: 0 }, spend: 2, unknown: false, none: false },
  { name: "zero counters with unavailable spend", counters: { paid_requests: 0, unknown_paid_requests: 0 }, spend: null, unknown: false, none: false },
  { name: "positive paid counter", counters: { paid_requests: 1, unknown_paid_requests: 0 }, spend: 0, unknown: false, none: false },
  { name: "positive unknown-cost counter", counters: { paid_requests: 0, unknown_paid_requests: 1 }, spend: 0, unknown: false, none: false },
  { name: "camel-case zero counters", counters: { paidRequests: 0, unknownPaidRequests: 0 }, spend: 0, unknown: false, none: true },
])("Usage preserves paid-count truth: $name", async ({ counters, spend, unknown, none }) => {
  const client = renderUsageSummary({
    requests: 1,
    receipts: [],
    remote_spend: spend,
    ...counters,
  });
  const summary = await screen.findByRole("region", { name: "This month" });
  expect(within(summary).queryByText("No paid requests") !== null).toBe(none);
  expect(within(summary).queryByText("Paid request count is unknown.") !== null).toBe(unknown);
  if (spend === null) expect(within(summary).getByText("Unavailable")).toBeTruthy();
  if (spend === 2) expect(within(summary).getByText(/\$2/)).toBeTruthy();
  client.clear();
});

test.each([
  { name: "absent ID", identity: {}, expected: null },
  { name: "null ID", identity: { request_id: null }, expected: null },
  { name: "empty ID", identity: { request_id: "" }, expected: null },
  { name: "whitespace ID", identity: { request_id: "  " }, expected: null },
  { name: "real request ID", identity: { request_id: "req-real" }, expected: "req-real" },
  { name: "real legacy ID", identity: { id: "req-legacy" }, expected: "req-legacy" },
])("Usage displays only recorded request IDs: $name", async ({ identity, expected }) => {
  const client = renderUsageSummary({
    requests: 1,
    receipts: [{ capability: "chat", status: "succeeded", ...identity }],
  });
  fireEvent.click(await screen.findByRole("button", { name: "View recent requests" }));
  fireEvent.click(await screen.findByRole("button", { name: /Receipt details for/ }));
  const dialog = await screen.findByRole("dialog");
  const requestId = within(dialog).getByText("Request ID").nextElementSibling;
  if (expected === null)
    expect(requestId?.textContent).toBe("No request ID recorded");
  else
    expect(within(requestId as HTMLElement).getByText(expected, { exact: true })).toBeTruthy();
  const copy = within(dialog).queryByRole("button", { name: "Copy request ID" });
  if (expected === null) {
    expect(copy).toBeNull();
    expect(within(dialog).queryByText("receipt", { exact: true })).toBeNull();
  } else {
    expect(copy).not.toBeNull();
    expect(within(dialog).queryByText("No request ID recorded")).toBeNull();
  }
  client.clear();
});

test("Usage sorts top models by requests and model name before slicing", async () => {
  const client = renderUsageSummary({
    requests: 100,
    by_model: [
      { model: "low", requests: 1 },
      { model: "zeta", requests: 10 },
      { model: "middle", requests: 9 },
      { model: "beta", requests: 10 },
      { model: "tail", requests: 2 },
      { model: "highest", requests: 20 },
      { model: "alpha", requests: 10 },
    ],
  });
  const heading = await screen.findByRole("columnheader", { name: "Capability and model" });
  const table = heading.closest("table") as HTMLTableElement;
  const models = () => Array.from(table.querySelectorAll("tbody code"), (node) => node.textContent);
  const top = ["highest", "alpha", "beta", "zeta", "middle"];
  expect(models()).toEqual(top);
  fireEvent.click(screen.getByRole("button", { name: "View all 7 models" }));
  expect(models()).toEqual([...top, "tail", "low"]);
  fireEvent.click(screen.getByRole("button", { name: "Show top 5" }));
  expect(models()).toEqual(top);
  client.clear();
});

test.each([
  { name: "unclassified remainder", total: 10, counts: { chat: 2 }, expected: [2, 8, 10] },
  { name: "unknown and explicit Other counts", total: 10, counts: { chat: 2, other: 1, future: 3 }, expected: [2, 8, 10] },
  { name: "embedding aliases combined", total: 10, counts: { chat: 2, embed: 1, embedding: 2, embeddings: 3 }, expected: [2, 6, 2, 10] },
  { name: "total without capability counts", total: 4, counts: {}, expected: [4, 4] },
  { name: "missing total falls back to alias counts", total: undefined, counts: { chat: 2, embed: 3 }, expected: [2, 3, 5] },
])("Usage reconciles daily capability stacks: $name", async ({ total, counts, expected }) => {
  const client = renderUsageSummary({
    requests: 10,
    by_day: [{ date: "2026-10-07", total, capabilities: counts }],
  });
  const table = await screen.findByRole("table", { name: "Requests by day and capability" });
  const row = within(table).getByRole("rowheader", { name: "2026-10-07" }).closest("tr") as HTMLTableRowElement;
  const values = within(row).getAllByRole("cell").map((cell) => Number(cell.textContent));
  expect(values).toEqual(expected);
  expect(values.slice(0, -1).reduce((sum, value) => sum + value, 0)).toBe(values[values.length - 1]);
  if (total !== undefined)
    expect(within(table).getByRole("columnheader", { name: "Other" })).toBeTruthy();
  client.clear();
});

test("Usage keeps recorded counts on an overfull day and says the counts disagree", async () => {
  const client = renderUsageSummary({
    requests: 10,
    by_day: [{ date: "2026-10-07", total: 5, capabilities: { chat: 4, image: 3 } }],
  });
  const table = await screen.findByRole("table", { name: "Requests by day and capability" });
  const row = within(table).getByRole("rowheader", { name: "2026-10-07" }).closest("tr") as HTMLTableRowElement;
  const cells = within(row).getAllByRole("cell").map((cell) => cell.textContent);
  expect(cells.slice(0, 2)).toEqual(["4", "3"]);
  expect(cells[cells.length - 1]).toBe("5 (capability counts add to 7)");
  expect(
    screen.getByText(/On 1 day the capability counts add to more than the recorded total/),
  ).toBeVisible();
  client.clear();
});

test.each([
  {
    name: "explicit Other over the total",
    day: { total: 10, capabilities: { chat: 8, other: 5 } },
    cells: ["8", "5", "10 (capability counts add to 13)"],
  },
  {
    name: "negative and absurd counts are ignored",
    day: { capabilities: { chat: -2, image: 1e308, video: 3 } },
    cells: ["3", "3"],
  },
])("Usage never reduces or invents daily counts: $name", async ({ day, cells }) => {
  const client = renderUsageSummary({
    requests: 10,
    by_day: [{ date: "2026-10-07", ...day }],
  });
  const table = await screen.findByRole("table", { name: "Requests by day and capability" });
  const row = within(table).getByRole("rowheader", { name: "2026-10-07" }).closest("tr") as HTMLTableRowElement;
  expect(within(row).getAllByRole("cell").map((cell) => cell.textContent)).toEqual(cells);
  client.clear();
});

for (const page of ["Activity", "Usage"] as const) {
  for (const status of ["backend_failed", "failed", "error"]) {
    test.each([
      { name: "no recorded layer", fields: { status_code: 502 }, detail: null },
      { name: "recorded stage", fields: { stage: "Route_Select.v2" }, detail: "Stage: Route_Select.v2" },
      { name: "recorded error code", fields: { error_code: "UPSTREAM_reset-v2" }, detail: "Error code: UPSTREAM_reset-v2" },
      {
        name: "recorded metadata",
        fields: { metadata: { stage: "Provider_Call", error_code: "E_TIMEOUT.v2", status_code: 502 } },
        detail: "Stage: Provider_Call · Error code: E_TIMEOUT.v2",
      },
    ])(`${page} reports ${status} without inferred attribution: $name`, async ({ fields, detail }) => {
      const receipt = {
        request_id: "req-failure",
        intent: "chat",
        capability: "chat",
        status,
        ...fields,
      };
      vi.stubGlobal(
        "fetch",
        vi.fn(async () => Response.json(
          page === "Activity"
            ? {
                records: [{ receipt, person_label: "Owner", app_label: "Try" }],
                next_cursor: null,
                people: [],
              }
            : { requests: 1, receipts: [receipt] },
        )),
      );
      const client = new QueryClient({
        defaultOptions: { queries: { retry: false } },
      });
      render(
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
            {page === "Activity" ? <Requests /> : <UsagePage />}
          </QueryClientProvider>
        </ViewerContext.Provider>,
      );
      if (page === "Usage")
        fireEvent.click(await screen.findByRole("button", { name: "View recent requests" }));
      const row = await screen.findByRole("button", {
        name: page === "Activity" ? /Request details:/ : /Receipt details for/,
      });
      expect(within(row).getByText("Failed", { exact: true })).toBeTruthy();
      expect(within(row).queryByText(/Engine or provider failed/)).toBeNull();
      fireEvent.click(row);
      const dialog = await screen.findByRole("dialog");
      const summary = within(dialog).getByText("The request failed.", { exact: true });
      const paragraphs = Array.from(
        summary.parentElement!.querySelectorAll("p"),
        (node) => node.textContent,
      );
      expect(paragraphs).toEqual([
        "Failed",
        "The request failed.",
        ...(detail === null ? [] : [detail]),
        "Open Technical details for the recorded status and warnings.",
      ]);
      expect(within(dialog).queryByText("The selected model or provider failed before returning a result.")).toBeNull();
      expect(within(dialog).queryByText("The gateway could not complete this request.")).toBeNull();
      client.clear();
    });
  }
}
