import { Component, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { Requests } from "@/app/activity/requests";

vi.mock("@/lib/use-model-name", () => ({
  useModelName: () => (id: string) => id,
}));
vi.mock("@/components/shell/session", () => ({
  useViewer: () => ({ authenticated: true, role: "owner", admin: true, name: "Owner" }),
}));

class CrashBoundary extends Component<{ children: ReactNode }, { crashed: boolean }> {
  state = { crashed: false };
  static getDerivedStateFromError() { return { crashed: true }; }
  render() {
    return this.state.crashed
      ? <p role="alert">Unexpected render crash</p>
      : this.props.children;
  }
}

let client: QueryClient | undefined;
afterEach(() => {
  cleanup();
  client?.clear();
  vi.unstubAllGlobals();
});

it.each([
  {},
  {"records":[],"next_cursor":null,"people":{}},
  {"records":[],"next_cursor":null,"people":[null]},
  {"records":[],"next_cursor":null,"people":[],"sources":{}},
  {"records":[],"next_cursor":null,"people":[],"sources":[null]},
  {"records":[],"next_cursor":null,"people":[{"id":"p","label":4}]},
  {"records":[],"next_cursor":null,"people":[],"sources":[{"id":"s","label":4}]},
  {"records":[],"next_cursor":null,"people":[{"id":4,"label":"Person"}]},
  {"records":[],"next_cursor":null,"people":[],"sources":[{"id":4,"label":"App"}]},
  {"records":[],"next_cursor":4,"people":[]},
  { records: null, next_cursor: null, people: [] },
  { records: [null], next_cursor: null, people: [] },
  {
    records: [{ receipt: null, person_label: "Owner", app_label: "Try", created_at: 1 }],
    next_cursor: null,
    people: [],
  },
])("handles malformed Activity payload %# as a retryable request error", async (bad) => {
  let valid = false;
  let requestReads = 0;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    if (!String(input).startsWith("/admin/api/activity/requests?")) {
      throw new Error("Unexpected fixture request: " + String(input));
    }
    requestReads += 1;
    return Response.json(valid ? { records: [], next_cursor: null, people: [] } : bad);
  }));
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  render(
    <QueryClientProvider client={client}>
      <CrashBoundary><Requests /></CrashBoundary>
    </QueryClientProvider>,
  );
  expect(await screen.findByText("Couldn't load requests")).toBeVisible();
  expect(screen.queryByText("Unexpected render crash")).not.toBeInTheDocument();
  expect(screen.queryByText("No request receipts yet")).not.toBeInTheDocument();
  expect(requestReads).toBe(1);
  valid = true;
  await userEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(await screen.findByText("No request receipts yet")).toBeVisible();
  await waitFor(() => expect(requestReads).toBe(2));
  expect(screen.queryByText("Couldn't load requests")).not.toBeInTheDocument();
});

it("keeps the first request page when the next pick-list is malformed", async () => {
  let validNext = false;
  const urls: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    urls.push(url);
    return Response.json(url.includes("before=older")
      ? { records: [], people: validNext ? [] : [null], next_cursor: null }
      : { records: [{
          receipt: { request_id: "first", intent: "chat", status: "success" },
          person_label: "Mira", app_label: "Notes", created_at: 1,
        }], people: [], next_cursor: "older" });
  }));
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(<QueryClientProvider client={client}><CrashBoundary><Requests /></CrashBoundary></QueryClientProvider>);
  const row = await screen.findByRole("button", { name: "Request details: Chat by Mira" });
  await userEvent.click(screen.getByRole("button", { name: "Load more" }));
  expect(await screen.findByText("Couldn't load more requests")).toBeVisible();
  expect(row).toBeVisible();
  expect(screen.queryByText("Unexpected render crash")).not.toBeInTheDocument();
  validNext = true;
  await userEvent.click(screen.getByRole("button", { name: "Try again" }));
  await waitFor(() => expect(screen.queryByText("Couldn't load more requests")).not.toBeInTheDocument());
  expect(row).toBeVisible();
  expect(urls.filter((url) => url.includes("before=older"))).toHaveLength(2);
});
