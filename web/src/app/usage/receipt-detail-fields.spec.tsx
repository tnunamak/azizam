import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
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

const CREATED_AT = 1792291546;
const created = new Date(CREATED_AT * 1000).toISOString();

// What GET /me/api/receipts/{id} returns: the receipt plus the person, App and
// time the server adds (created_at is a table column, not part of the JSON).
const jobSubmission = {
  request_id: "req_job", status: "ok", intent: "image.generate",
  accepted_at_ms: null, finished_at_ms: CREATED_AT * 1000 + 998,
  requested_model: null, resolved_model: null,
  requested_workflow: "flux2-klein-4b",
  resolved_workflow: "image.generate.flux2-klein-4b",
  created_at: CREATED_AT, person_label: "Alice Example", app_label: "Desktop",
};
const chat = {
  request_id: "req_chat", status: "succeeded", intent: "chat.completions",
  requested_model: "chat-fast", resolved_model: "chat-fast", latency_ms: 120,
  created_at: CREATED_AT, person_label: "Bob Example", app_label: "Phone",
};
const noOwner = {
  request_id: "req_orphan", status: "succeeded", intent: "chat.completions",
  resolved_model: "chat-fast", created_at: CREATED_AT,
  person_label: "Unattributed request", app_label: "Local network · no key",
};

function open(receipt: Record<string, unknown>, summary: unknown[] = []) {
  window.history.replaceState(null, "", `/usage/?receipt=${receipt.request_id}`);
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/me/api/receipts/")) return Response.json(receipt);
    if (url.includes("/usage/summary"))
      return Response.json({ requests: summary.length, media_count: 0, receipts: summary });
    return Response.json({});
  }));
  render(
    <ViewerContext.Provider
      value={{ authenticated: true, name: "Owner", email: null, role: "owner", admin: true }}
    >
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <UsagePage />
      </QueryClientProvider>
    </ViewerContext.Provider>,
  );
}

beforeEach(() => window.history.replaceState(null, "", "/usage/"));
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("a job-submission receipt shows its person, workflow and time, not 'not recorded'", async () => {
  open(jobSubmission);
  const sheet = await screen.findByRole("dialog");
  expect(within(sheet).getByText("Alice Example")).toBeInTheDocument();
  expect(within(sheet).getByText("Desktop")).toBeInTheDocument();
  expect(within(sheet).getByText("Workflow")).toBeInTheDocument();
  expect(within(sheet).getByText("image.generate.flux2-klein-4b")).toBeInTheDocument();
  expect(sheet.querySelector("time")).toHaveAttribute("datetime", created);
  expect(sheet).not.toHaveTextContent("Not recorded");
  expect(sheet).not.toHaveTextContent("Model not recorded");
  expect(sheet).not.toHaveTextContent("Time not recorded");
});

test("a chat receipt shows its person, model and time", async () => {
  open(chat);
  const sheet = await screen.findByRole("dialog");
  expect(within(sheet).getByText("Bob Example")).toBeInTheDocument();
  expect(within(sheet).getByText("Model")).toBeInTheDocument();
  expect(within(sheet).queryByText("Workflow")).not.toBeInTheDocument();
  expect(sheet.querySelector("time")).toHaveAttribute("datetime", created);
  expect(sheet).not.toHaveTextContent("Model not recorded");
  expect(sheet).not.toHaveTextContent("Time not recorded");
});

test("a receipt from an app with no owner says it is unattributed", async () => {
  open(noOwner);
  const sheet = await screen.findByRole("dialog");
  expect(within(sheet).getByText("Unattributed request")).toBeInTheDocument();
  expect(sheet).not.toHaveTextContent("Not recorded");
});

test("the receipts list shows the workflow when a receipt has no model", async () => {
  open(chat, [{ ...jobSubmission, request_id: "req_listed" }]);
  expect(await screen.findByText("image.generate.flux2-klein-4b")).toBeInTheDocument();
});
