// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { focusManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Requests } from "./requests";

vi.mock("@/components/shell/session", () => ({ useViewer: () => ({ role: "member" }) }));
vi.mock("@/lib/use-model-name", () => ({ useModelName: () => (name: string) => name }));
for (const method of ["hasPointerCapture", "setPointerCapture", "releasePointerCapture", "scrollIntoView"])
  Object.defineProperty(HTMLElement.prototype, method, { configurable: true, value: () => false });

const NOW = Date.UTC(2026, 10, 18, 12);
const row = (id: string, person: string, created = NOW / 1000 - 1200, status = "running") => ({
  receipt: { request_id: id, intent: "chat", status },
  person_label: person, app_label: "Test app", created_at: created,
});
const clients: QueryClient[] = [];
async function advance(ms: number) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
}
async function mount(records = [row("old-request", "Original person")]) {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  focusManager.setFocused(true);
  let current = records;
  const requests = vi.fn(async () => ({ ok: true,
    json: async () => ({ records: current, people: [], next_cursor: null }) }));
  vi.stubGlobal("fetch", requests);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  clients.push(client);
  render(<QueryClientProvider client={client}><Requests /></QueryClientProvider>);
  await advance(100);
  expect(screen.getByRole("button", { name: /Request details: Chat by/ })).toBeTruthy();
  expect(requests).toHaveBeenCalledTimes(1);
  return { client, requests, replace: (next: typeof records) => { current = next; } };
}
afterEach(() => {
  cleanup();
  for (const client of clients.splice(0)) client.clear();
  focusManager.setFocused(undefined);
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("starts paused and makes no second fetch after six seconds", async () => {
  const { requests } = await mount();
  expect(screen.getByRole("button", { name: "Follow live" }).getAttribute("aria-pressed")).toBe("false");
  await advance(6000);
  expect(requests).toHaveBeenCalledTimes(1);
});

it("following polls after five seconds without changing the query key and Pause stops the timer", async () => {
  const { client, requests } = await mount();
  const keys = client.getQueryCache().getAll().map(query => query.queryKey);
  fireEvent.click(screen.getByRole("button", { name: "Follow live" }));
  await advance(4900);
  expect(requests).toHaveBeenCalledTimes(1);
  await advance(200);
  expect(requests).toHaveBeenCalledTimes(2);
  expect(client.getQueryCache().getAll().map(query => query.queryKey)).toEqual(keys);
  fireEvent.click(screen.getByRole("button", { name: "Pause" }));
  await advance(6000);
  expect(requests).toHaveBeenCalledTimes(2);
});

it("shows No update since for a running receipt created twenty minutes ago", async () => {
  await mount();
  const request = screen.getByRole("button", { name: "Request details: Chat by Original person" });
  expect(within(request).getByText(/No update since/)).toBeTruthy();
});

it("never shows stale words when the created time is absent", async () => {
  await mount([{ ...row("unknown-time", "Unknown time"), created_at: 0 }]);
  expect(screen.queryByText(/No update since/)).toBeNull();
});

it("keeps the open request sheet on its request when a live refetch prepends a newer row", async () => {
  const { replace, requests } = await mount();
  fireEvent.click(screen.getByRole("button", { name: "Follow live" }));
  fireEvent.click(screen.getByRole("button", { name: "Request details: Chat by Original person" }));
  expect(within(screen.getByRole("dialog")).getByText("Original person")).toBeTruthy();
  replace([row("new-request", "New person", NOW / 1000), row("old-request", "Original person")]);
  await advance(5100);
  expect(requests).toHaveBeenCalledTimes(2);
  const dialog = within(screen.getByRole("dialog"));
  expect(dialog.getByText("Original person")).toBeTruthy();
  expect(dialog.queryByText("New person")).toBeNull();
  expect(dialog.getByText("Request ID: old-request")).toBeTruthy();
});
