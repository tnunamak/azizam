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
