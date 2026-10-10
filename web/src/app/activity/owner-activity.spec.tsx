import { afterEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import Activity from "./page";
import ProvidersPage from "../providers/page";
import EnginesPage from "../engines/page";
import { ViewerContext } from "@/components/shell/session";

import { Requests } from "./requests";
const pointerMethods = [
  "hasPointerCapture",
  "setPointerCapture",
  "releasePointerCapture",
  "scrollIntoView",
] as const;
for (const method of pointerMethods)
  Object.defineProperty(HTMLElement.prototype, method, {
    configurable: true,
    value: () => false,
  });
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
function mount(
  auditResult?: () => ReturnType<typeof api.audit.list>,
  showChanges = true,
) {
  vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
  vi.spyOn(api.users, "list").mockResolvedValue([
    {
      id: "person-beyond-audit-window",
      issuer: "",
      subject: "",
      display_name: "Azizam Owner",
      name: null,
      email: null,
      created_at: 0,
      last_login_at: null,
      roles: [],
    },
  ]);
  const audit = vi
    .spyOn(api.audit, "list")
    .mockImplementation(auditResult ?? (async () => ({ records: [], next_cursor: null })));
  render(
    <QueryClientProvider
      client={
        new QueryClient({
          defaultOptions: { queries: { retry: false, gcTime: 0 } },
        })
      }
    >
      <Activity />
    </QueryClientProvider>,
  );
  if (showChanges) {
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Changes" }), {
      button: 0,
      ctrlKey: false,
    });
  }
  return audit;
}
it("offers human names from the people source beyond the recent audit window", async () => {
  const user = userEvent.setup();
  const audit = mount();
  await screen.findByText("No changes yet");
  await user.click(screen.getByRole("combobox", { name: "Person" }));
  await user.click(await screen.findByRole("option", { name: "Azizam Owner" }));
  await waitFor(() =>
    expect(audit).toHaveBeenCalledWith(
      expect.objectContaining({ actor_id: "person-beyond-audit-window" }),
    ),
  );
  expect(screen.getByText("Technical: filter by person ID")).toBeVisible();
  expect(screen.queryByText("Find a person")).toBeNull();
});
it.each([0, 1])(
  "clears the category, applied actor and draft with Clear filters button %s",
  async (buttonIndex) => {
    const user = userEvent.setup();
    const audit = mount();
    await screen.findByText("No changes yet");
    await user.click(screen.getByRole("combobox", { name: "Change type" }));
    await user.click(screen.getByRole("option", { name: "Apps" }));
    await user.click(screen.getByText("Technical: filter by person ID"));
    await user.type(
      screen.getByRole("textbox", { name: "Technical person ID" }),
      "opaque-person",
    );
    await user.click(screen.getByRole("button", { name: "Apply" }));
    await screen.findByText("No matching changes");
    await user.click(
      screen.getAllByRole("button", { name: "Clear filters" })[buttonIndex],
    );
    expect(
      screen.getByRole("textbox", { name: "Technical person ID" }),
    ).toHaveValue("");
    expect(
      within(screen.getByRole("combobox", { name: "Change type" })).getByText(
        "All changes",
      ),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Apply" }));
    await screen.findByText("No changes yet");
    await waitFor(() =>
      expect(audit).toHaveBeenLastCalledWith(
        expect.objectContaining({
          actor_id: undefined,
          action_prefix: undefined,
        }),
      ),
    );
  },
);
it("shows people lookup errors with a retry action", async () => {
  mount();
  vi.mocked(api.users.list).mockRejectedValue(new Error("offline"));
  cleanup();
  render(
    <QueryClientProvider
      client={
        new QueryClient({
          defaultOptions: { queries: { retry: false, gcTime: 0 } },
        })
      }
    >
      <Activity />
    </QueryClientProvider>,
  );
  fireEvent.mouseDown(screen.getByRole("tab", { name: "Changes" }), {
    button: 0,
    ctrlKey: false,
  });
  expect(
    await screen.findByText(
      "Recent activity names are still available.",
    ),
  ).toBeVisible();
  expect(screen.getByRole("heading", { name: "Couldn't load people" })).toBeVisible();
});

it("announces loading changes without claiming that an empty list is ready", () => {
  mount(() => new Promise(() => {}));
  const status = screen.getByText("Loading changes").closest('[role="status"]');
  expect(status).toHaveTextContent("Loading changes");
  expect(status).toHaveAttribute("aria-live", "polite");
  expect(status).toHaveAttribute("aria-atomic", "true");
  expect(screen.getByLabelText("Loading changes")).toHaveAttribute("aria-busy", "true");
  expect(screen.queryByText("No changes yet")).toBeNull();
  expect(screen.queryByText("Ready")).toBeNull();
});

it("keeps the shared page header and empty panel with its existing action", async () => {
  mount();
  expect(screen.getByRole("heading", { level: 1, name: "Activity" })).toBeVisible();
  const label = await screen.findByText("No changes yet");
  expect(label.parentElement?.parentElement).toHaveClass("border-dashed");
  expect(screen.getByRole("link", { name: "Create an app" })).toBeVisible();
});

it("names a changes load error in an alert and retries the same query", async () => {
  const user = userEvent.setup();
  const audit = mount(() => Promise.reject(new Error("offline")));
  const alert = await screen.findByRole("alert");
  expect(within(alert).getByRole("heading", { name: "Couldn't load changes" })).toBeVisible();
  expect(screen.queryByText("No changes yet")).toBeNull();
  audit.mockResolvedValue({ records: [], next_cursor: null });
  await user.click(within(alert).getByRole("button", { name: "Try again" }));
  await screen.findByText("No changes yet");
  expect(screen.queryByRole("alert")).toBeNull();
});

function mountRequests() {
  render(
    <QueryClientProvider
      client={new QueryClient({
        defaultOptions: { queries: { retry: false, gcTime: 0 } },
      })}
    >
      <Requests />
    </QueryClientProvider>,
  );
}

it("announces loading requests without showing an empty result", () => {
  vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
  mountRequests();
  expect(screen.getByRole("status")).toHaveTextContent("Loading requests");
  expect(screen.getByLabelText("Loading requests")).toHaveAttribute("aria-busy", "true");
  expect(screen.queryByText("No request receipts yet")).toBeNull();
});

it("keeps request errors distinct from empty results and retries", async () => {
  const user = userEvent.setup();
  const fetchRequests = vi.fn().mockRejectedValue(new Error("offline"));
  vi.stubGlobal("fetch", fetchRequests);
  mountRequests();
  const alert = await screen.findByRole("alert");
  expect(within(alert).getByRole("heading", { name: "Couldn't load requests" })).toBeVisible();
  expect(screen.queryByText("No request receipts yet")).toBeNull();
  fetchRequests.mockResolvedValue({
    ok: true,
    json: async () => ({ records: [], next_cursor: null, people: [], sources: [] }),
  });
  await user.click(within(alert).getByRole("button", { name: "Try again" }));
  const label = await screen.findByText("No request receipts yet");
  expect(label.parentElement?.parentElement).toHaveClass("border-dashed");
  expect(screen.getByText("Requests from apps and devices on your trusted network appear here.")).toBeVisible();
  expect(screen.getByRole("link", { name: "Connect an app" })).toHaveAttribute("href", "/ui/");
  expect(screen.queryByRole("alert")).toBeNull();
});

const recordedChange = {
  id: "change-1",
  action: "app.create",
  actor_id: "person-1",
  actor_kind: "user",
  actor_label: "Sam",
  target_kind: "app",
  target_id: "app-1",
  metadata: { name: "Family app" },
  created_at: 1,
} as Awaited<ReturnType<typeof api.audit.list>>["records"][number];

it("keeps recorded changes visible when refreshing fails", async () => {
  const user = userEvent.setup();
  const audit = mount(async () => ({ records: [recordedChange], next_cursor: null }));
  await screen.findByRole("button", { name: /^Details:/ });
  audit.mockRejectedValue(new Error("offline"));
  await user.click(screen.getByRole("button", { name: "Refresh" }));
  const alert = await screen.findByRole("alert");
  expect(within(alert).getByRole("heading", { name: "Couldn't refresh changes" })).toBeVisible();
  expect(within(alert).getByText("The changes already loaded are still shown.")).toBeVisible();
  expect(screen.getByRole("button", { name: /^Details:/ })).toBeVisible();
  audit.mockResolvedValue({ records: [recordedChange], next_cursor: null });
  await user.click(within(alert).getByRole("button", { name: "Try again" }));
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
});

it("retries the next changes page without discarding the loaded page", async () => {
  const user = userEvent.setup();
  const audit = mount(async () => ({ records: [recordedChange], next_cursor: "older" }));
  await screen.findByRole("button", { name: /^Details:/ });
  audit.mockRejectedValue(new Error("offline"));
  await user.click(screen.getByRole("button", { name: "Load more" }));
  const alert = await screen.findByRole("alert");
  expect(within(alert).getByRole("heading", { name: "Couldn't load more changes" })).toBeVisible();
  expect(screen.queryByRole("heading", { name: "Couldn't refresh changes" })).toBeNull();
  expect(screen.getByRole("button", { name: /^Details:/ })).toBeVisible();
  audit.mockResolvedValue({ records: [], next_cursor: null });
  await user.click(within(alert).getByRole("button", { name: "Try again" }));
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  expect(audit).toHaveBeenLastCalledWith(expect.objectContaining({ before: "older" }));
  expect(screen.getByRole("button", { name: /^Details:/ })).toBeVisible();
});

it("opens Requests first by default and keeps Changes reachable", async () => {
  const user = userEvent.setup();
  mount(undefined, false);
  const tabs = screen.getAllByRole("tab");
  expect(tabs.map((tab) => tab.textContent)).toEqual(["Requests", "Changes"]);
  expect(tabs[0]).toHaveAttribute("aria-selected", "true");
  expect(screen.getByLabelText("Loading requests")).toBeVisible();
  await user.click(tabs[1]);
  expect(tabs[1]).toHaveAttribute("aria-selected", "true");
  await screen.findByText("No changes yet");
  await user.click(tabs[0]);
  expect(screen.getByLabelText("Loading requests")).toBeVisible();
});

it("shows newest receipts first with person and app despite a source label, and opens the matching receipt", async () => {
  const user = userEvent.setup();
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({
      records: [
        {
          receipt: { request_id: "older", intent: "speech", status: "success", latency_ms: 0 },
          person_label: "Sam", app_label: "Reader", created_at: 100,
        },
        {
          receipt: { request_id: "newer", intent: "image", status: "failed", latency_ms: 1250 },
          person_label: "Alex", app_label: "Studio", created_at: 200,
          source: { label: "Shared device", kind: "app" },
        },
      ],
      next_cursor: null, people: [], sources: [],
    }),
  }));
  mountRequests();
  const table = await screen.findByRole("table", { name: "Request receipts" });
  const buttons = within(table).getAllByRole("button");
  expect(buttons[0]).toHaveAccessibleName("Request details: Image request by Alex");
  expect(buttons[1]).toHaveAccessibleName("Request details: Voice request by Sam");
  expect(within(buttons[0]).getByText("Alex · Studio")).toBeVisible();
  expect(within(buttons[0]).getByText("Failed")).toBeVisible();
  expect(buttons[0]).toHaveTextContent("1.3 s");
  expect(within(buttons[0]).getByText(/1\.3 s/)).toHaveTextContent(
    /^.+ · 1\.3 s$/,
  );
  expect(buttons[0]).toHaveClass("min-h-(--row-2)", "max-sm:min-h-(--row-phone-2)");
  expect(buttons[1]).toHaveTextContent("0 ms");
  await user.click(buttons[0]);
  const receipt = await screen.findByRole("dialog");
  expect(within(receipt).getByText("newer")).toBeVisible();
  expect(within(receipt).getByText("Studio")).toBeVisible();
  await user.keyboard("{Escape}");
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  await user.click(buttons[1]);
  expect(within(await screen.findByRole("dialog")).getByText("older")).toBeVisible();
});

it("labels unrecorded time and duration without inventing a zero duration", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({
      records: [{
        receipt: { request_id: "unknown", intent: "speech" },
        person_label: "Sam", app_label: "Reader", created_at: 0,
      }],
      next_cursor: null, people: [], sources: [],
    }),
  }));
  mountRequests();
  const row = await screen.findByRole("button", { name: "Request details: Voice request by Sam" });
  expect(row).toHaveTextContent("Outcome not recorded");
  expect(row).toHaveTextContent("Time not recorded · Duration not recorded");
  expect(row).not.toHaveTextContent("0 ms");
});

it("COPY-10 filtered changes guidance", async () => {
  const user = userEvent.setup();
  mount();
  expect(await screen.findByText("Changes to apps, keys and access plans appear here.")).toBeVisible();
  expect(screen.getByRole("link", { name: "Create an app" })).toBeVisible();
  await user.click(screen.getByRole("combobox", { name: "Change type" }));
  await user.click(screen.getByRole("option", { name: "Apps" }));
  expect(await screen.findByText("No matching changes")).toBeVisible();
  expect(screen.getByText("Clear filters to see other changes.")).toBeVisible();
  expect(screen.queryByRole("link", { name: "Create an app" })).toBeNull();
  expect(screen.getAllByRole("button", { name: "Clear filters" })).toHaveLength(2);
});

function serveCopyReceipt(receipt: Record<string, unknown>) {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({
      records: [{ receipt: { request_id: "copy-receipt", ...receipt }, person_label: "Sam", app_label: "Reader", created_at: 0 }],
      next_cursor: null, people: [], sources: [],
    }),
  }));
  mountRequests();
}

it.each([
  ["speech.synthesis", "Voice request"],
  ["image.generate", "Image request"],
  ["decide", "Decision request"],
  ["embed", "Embedding request"],
])("COPY-13 activity receipt name %s", async (intent, title) => {
  serveCopyReceipt({ intent });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: `Request details: ${title} by Sam` }));
  expect(within(screen.getByRole("dialog")).getByRole("heading", { name: title })).toBeVisible();
});

it.each(["success", "succeeded", "ok"])("COPY-11 activity completion once (%s)", async (status) => {
  serveCopyReceipt({ intent: "image", status });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Request details: Image request by Sam" }));
  const sheet = within(screen.getByRole("dialog"));
  expect(sheet.getAllByText("Completed")).toHaveLength(1);
  expect(sheet.queryByText("The gateway completed this request.")).toBeNull();
  expect(screen.getByRole("dialog")).toHaveAccessibleDescription("Sam");
});

it.each([undefined, "provider", "opaque_stage"])("COPY-08 activity failure uncertainty (%s)", async (stage) => {
  serveCopyReceipt({ intent: "chat", status: "failed", stage, error_code: "opaque_code" });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Request details: Chat by Sam" }));
  const sheet = within(screen.getByRole("dialog"));
  expect(sheet.getByText("We couldn't tell where this stopped.")).toBeVisible();
  expect(sheet.getByText(stage ? `Stage: ${stage} · Error code: opaque_code` : "Error code: opaque_code")).toBeVisible();
  expect(sheet.queryByText("The request failed.")).toBeNull();
});

it.each([
  ["in_progress", "The request was still running when this receipt was recorded."],
  ["timed_out", "The request timed out before a result was recorded."],
  ["incomplete", "The receipt does not include a final outcome."],
])("COPY-11 activity outcome context (%s)", async (status, summary) => {
  serveCopyReceipt({ intent: "chat", status });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Request details: Chat by Sam" }));
  const sheet = within(screen.getByRole("dialog"));
  expect(screen.getByRole("dialog")).toHaveAccessibleDescription("Sam");
  expect(sheet.getByText(summary)).toBeVisible();
});

function mountCopyProviders(provider: Record<string, unknown> = {}, reconciliations: unknown[] = []) {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path.endsWith("/reconciliation")) return Response.json({ reconciliations });
    if (path === "/admin/api/providers") return Response.json({
      providers: [{ id: "example-provider", name: "Example Provider", key_set: true, ...provider }],
    });
    return Response.json({});
  }));
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}>
      <ProvidersPage />
    </QueryClientProvider>,
  );
}

it.each([
  [null, "Cost not recorded"],
  [0, "USD 0.00"],
  [0.001, "Less than USD 0.01"],
  [0.42, "USD 0.42"],
])("COPY-06 provider amounts (%s)", async (amount, label) => {
  mountCopyProviders({ spend_today_usd: amount, spend_month_usd: amount });
  await screen.findByRole("heading", { name: "Example Provider" });
  expect(screen.getAllByText(String(label))).toHaveLength(2);
  expect(screen.getByText("Paid models run with provider pages.")).toBeVisible();
  expect(screen.getByText("A request uses a provider only when its model names one.")).toBeVisible();
  expect(screen.queryByText(/Paid access is never automatic/)).toBeNull();
});

it.each([
  ["Remove provider", "Remove Example Provider from this proxy?"],
  ["Delete key", "Delete the saved key for Example Provider?"],
])("COPY-07 provider confirmation %s", async (action, description) => {
  mountCopyProviders();
  const user = userEvent.setup();
  await screen.findByRole("heading", { name: "Example Provider" });
  await user.click(screen.getByText("Manage"));
  await user.click(screen.getByRole("button", { name: "More actions for Example Provider" }));
  await user.click(await screen.findByRole("menuitem", { name: action }));
  expect(await screen.findByText(description)).toBeVisible();
  expect(screen.queryByText(/The full key is never shown again/)).toBeNull();
});

it("COPY-06 provider charge precision", async () => {
  const amount = "0.0000123456789012345";
  mountCopyProviders(
    { spend: { pending_reconciliation_count: 1 } },
    [{ id: 1, estimated_usd: amount, request_id: "request-1" }],
  );
  const user = userEvent.setup();
  await screen.findByRole("heading", { name: "Example Provider" });
  await user.click(screen.getByText("Manage"));
  await user.click(screen.getByRole("button", { name: "Reconcile charges" }));
  expect(await screen.findByText(`USD ${amount}`)).toBeVisible();
});

it("COPY-18 access plan names", async () => {
  mountCopyProviders();
  const user = userEvent.setup();
  await screen.findByRole("heading", { name: "Example Provider" });
  await user.click(screen.getByText("Manage"));
  await user.click(screen.getByRole("button", { name: "Edit access plans" }));
  expect(screen.getByText("No access plans selected. Paid requests will be denied.")).toBeVisible();
  expect(screen.getByRole("button", { name: "Save access plans" })).toBeVisible();
});

it("COPY-19 backend access guidance", () => {
  render(
    <ViewerContext.Provider value={{ authenticated: true, name: "Sam", email: null, role: "member", admin: false }}>
      <EnginesPage />
    </ViewerContext.Provider>,
  );
  expect(screen.getByText("Only an owner or admin can manage backends. Ask an owner or admin for help.")).toBeVisible();
  expect(screen.queryByText(/Sign in with an owner account/)).toBeNull();
});
