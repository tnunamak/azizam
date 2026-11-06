import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { ViewerContext } from "@/components/shell/session";
import UsagePage from "./page";

vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: ReactNode }) => <>{children}</>,
  Bar: () => null,
  BarChart: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  CartesianGrid: () => null,
  Tooltip: () => null,
  XAxis: () => null,
  YAxis: () => null,
}));

type ReceiptAnswer = () => Response;

function renderWithReceipt(...answers: ReceiptAnswer[]) {
  const receiptCalls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/me/api/receipts/")) {
        receiptCalls.push(url);
        const answer = answers[Math.min(receiptCalls.length, answers.length) - 1];
        return answer();
      }
      if (url.includes("/usage/summary"))
        return Response.json({ requests: 0, media_count: 0, receipts: [] });
      return Response.json({});
    }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <ViewerContext.Provider
      value={{ authenticated: true, name: "Member", email: null, role: "member", admin: false }}
    >
      <QueryClientProvider client={client}>
        <UsagePage />
      </QueryClientProvider>
    </ViewerContext.Provider>,
  );
  return receiptCalls;
}

const found: ReceiptAnswer = () =>
  Response.json({
    request_id: "req/one",
    status: "succeeded",
    requested_model: "chat-fast",
    resolved_model: "chat-fast",
    latency_ms: 120,
    intent: "chat",
  });

beforeEach(() => {
  window.history.replaceState(null, "", "/usage/?receipt=req%2Fone&view=all#top");
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

test("a receipt deep link opens the receipt and closing it drops the parameter", async () => {
  const calls = renderWithReceipt(found);
  expect(await screen.findByText("Opening receipt…")).toBeInTheDocument();
  const sheet = await screen.findByRole("dialog");
  expect(sheet).toHaveTextContent("Request receipt");
  expect(calls).toEqual(["/me/api/receipts/req%2Fone"]);

  await userEvent.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(`${window.location.pathname}${window.location.search}${window.location.hash}`).toBe(
    "/usage/?view=all#top",
  );
  expect(screen.queryByText("Opening receipt…")).not.toBeInTheDocument();
});

test.each([404, 403])("a receipt that answers %i says it isn't available", async (status) => {
  renderWithReceipt(() => Response.json({ error: { code: "receipt_not_found" } }, { status }));
  expect(
    await screen.findByText(
      "This receipt isn’t available. It may belong to someone else or be deleted.",
    ),
  ).toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));
  expect(screen.queryByText(/This receipt isn’t available/)).not.toBeInTheDocument();
  expect(window.location.search).toBe("?view=all");
});

test("a gateway failure offers Try again, which opens the receipt", async () => {
  const calls = renderWithReceipt(
    () => Response.json({ error: { code: "internal" } }, { status: 500 }),
    found,
  );
  expect(await screen.findByText("Couldn’t open this receipt")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(await screen.findByRole("dialog")).toHaveTextContent("Request receipt");
  expect(calls).toHaveLength(2);
});

test("Usage without a receipt parameter asks for no receipt", async () => {
  window.history.replaceState(null, "", "/usage/");
  const calls = renderWithReceipt(found);
  expect(await screen.findByRole("heading", { name: "Usage" })).toBeInTheDocument();
  expect(screen.queryByText("Opening receipt…")).not.toBeInTheDocument();
  expect(calls).toEqual([]);
});
