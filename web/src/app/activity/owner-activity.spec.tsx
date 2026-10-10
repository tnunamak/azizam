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
    await screen.findByText("No changes match these filters");
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
  expect(buttons[0]).toHaveAccessibleName("Request details: Images request by Alex");
  expect(buttons[1]).toHaveAccessibleName("Request details: Speech request by Sam");
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
  const row = await screen.findByRole("button", { name: "Request details: Speech request by Sam" });
  expect(row).toHaveTextContent("Outcome not recorded");
  expect(row).toHaveTextContent("Time not recorded · Duration not recorded");
  expect(row).not.toHaveTextContent("0 ms");
});
