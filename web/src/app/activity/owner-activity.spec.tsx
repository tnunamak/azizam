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
  usersResult?: () => ReturnType<typeof api.users.list>,
) {
  vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
  vi.spyOn(api.users, "list").mockImplementation(usersResult ?? (async () => [
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
  ]));
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
  expect(screen.queryByText("Technical: filter by person ID")).toBeNull();
  expect(screen.queryByRole("textbox", { name: /person id/i, hidden: true })).toBeNull();
  expect(screen.queryByText("Find a person")).toBeNull();
});
it.each([0, 1])(
  "clears the category and named person with Clear filters button %s",
  async (buttonIndex) => {
    const user = userEvent.setup();
    const audit = mount();
    await screen.findByText("No changes yet");
    await user.click(screen.getByRole("combobox", { name: "Change type" }));
    await user.click(screen.getByRole("option", { name: "Apps" }));
    await user.click(screen.getByRole("combobox", { name: "Person" }));
    await user.click(await screen.findByRole("option", { name: "Azizam Owner" }));
    await waitFor(() => expect(audit).toHaveBeenCalledWith(
      expect.objectContaining({ actor_id: "person-beyond-audit-window" }),
    ));
    expect(screen.queryByRole("textbox", { name: /person id/i, hidden: true })).toBeNull();
    await screen.findByText("No matching changes");
    await user.click(
      screen.getAllByRole("button", { name: "Clear filters" })[buttonIndex],
    );
    expect(screen.getByRole("combobox", { name: "Person" })).toHaveTextContent("Everyone");
    expect(
      within(screen.getByRole("combobox", { name: "Change type" })).getByText(
        "All changes",
      ),
    ).toBeVisible();
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
  // The default mock holds no recent changes, so no fallback names exist and
  // the page must not claim any (PF-141).
  const alert = await screen.findByRole("alert");
  expect(within(alert).getByRole("heading", { name: "Couldn't load people" })).toBeVisible();
  expect(alert).toHaveTextContent(
    "People could not be loaded. Try again.",
  );
  expect(alert).not.toHaveTextContent("still shown");
});

it("says recent-change names are still shown only when there are some (PF-141)", async () => {
  mount(async () => ({ records: [recordedChange], next_cursor: null }), true, () =>
    Promise.reject(new Error("offline")),
  );
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("Names from recent changes are still shown.");
});

it("uses the people list's name for a person in rows, sheet and selector (PF-141)", async () => {
  const user = userEvent.setup();
  mount(async () => ({
    records: [{ ...recordedChange, actor_id: "person-beyond-audit-window", actor_label: "owner@example.test" }],
    next_cursor: null,
  }));
  const row = await screen.findByRole("button", { name: "Details: Created an app by Azizam Owner" });
  expect(row).toHaveTextContent("Azizam Owner · Family app");
  expect(row).toHaveAccessibleDescription(/Created an app.*Azizam Owner.*Family app/);
  await user.click(row);
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText("Changed by").nextElementSibling).toHaveTextContent("Azizam Owner");
  // Who and when appear once, as fields, not again under the title.
  expect(dialog).toHaveAccessibleDescription("Details of this change");
  expect(within(dialog).getAllByText("Azizam Owner")).toHaveLength(1);
  expect(within(dialog).getAllByText("When")).toHaveLength(1);
  expect(within(dialog).getByText("Changed").nextElementSibling).toHaveTextContent("Family app");
});

it("names the layer in change-list failures (PF-142)", async () => {
  const user = userEvent.setup();
  const audit = mount(() => Promise.reject(new Error("offline")));
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(
    "The gateway's changes list could not be loaded. We could not identify the cause. Try again.",
  );
  audit.mockResolvedValue({ records: [recordedChange], next_cursor: "older" });
  await user.click(within(alert).getByRole("button", { name: "Try again" }));
  await screen.findByRole("button", { name: /^Details:/ });
  audit.mockRejectedValue(new Error("offline"));
  await user.click(screen.getByRole("button", { name: "Refresh" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "The gateway's changes list could not be refreshed. We could not identify the cause.",
  );
});

it("uses plain words for access history, assignments and the end of the list (PF-140)", async () => {
  const user = userEvent.setup();
  mount(async () => ({ records: [recordedChange], next_cursor: null }));
  await screen.findByRole("button", { name: /^Details:/ });
  expect(screen.getByText("End of changes")).toBeVisible();
  expect(screen.queryByText(/audit/i)).toBeNull();
  await user.click(screen.getByRole("button", { name: "Show access history" }));
  expect(screen.getByRole("button", { name: "Hide access history" })).toHaveAttribute("aria-pressed", "true");
  await user.click(screen.getByRole("combobox", { name: "Change type" }));
  expect(screen.getByRole("option", { name: "Access assignments" })).toBeVisible();
  expect(screen.queryByRole("option", { name: "Access mappings" })).toBeNull();
});

it("never shows a stored action name as the change title (PF-140)", async () => {
  mount(async () => ({
    records: [
      { ...recordedChange, id: "a", action: "runtime_profile_mapping.create" },
      { ...recordedChange, id: "b", action: "operator.widget_registry.frobnicate" },
    ],
    next_cursor: null,
  }));
  expect(await screen.findByRole("button", { name: "Details: Added an access assignment by Sam" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Details: Recorded a change by Sam" })).toBeVisible();
  expect(screen.queryByText(/frobnicate|widget registry/i)).toBeNull();
});

it("announces loading changes without claiming that an empty list is ready", () => {
  mount(() => new Promise(() => {}));
  const status = screen.getByRole("status", { name: "Changes updates" });
  expect(status).toHaveTextContent("Loading changes");
  expect(status).toHaveAttribute("aria-live", "polite");
  expect(status).toHaveAttribute("aria-atomic", "true");
  expect(screen.getByLabelText("Loading changes")).toHaveAttribute("aria-busy", "true");
  expect(screen.queryByText("No changes yet")).toBeNull();
  expect(screen.queryByText("Ready")).toBeNull();
});

it("keeps the shared page header and empty panel with a role-safe action", async () => {
  mount();
  expect(screen.getByRole("heading", { level: 1, name: "Activity" })).toBeVisible();
  const label = await screen.findByText("No changes yet");
  expect(label.parentElement?.parentElement).toHaveClass("border-dashed");
  expect(screen.getByRole("link", { name: "Go to Home" })).toBeVisible();
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
  expect(screen.getByRole("link", { name: "Go to Home" })).toHaveAttribute("href", "/");
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
  expect(alert).toHaveTextContent("The changes already loaded are still shown.");
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
  expect(buttons[0]).toHaveAccessibleName("Request details: Image by Alex");
  expect(buttons[1]).toHaveAccessibleName("Request details: Voice by Sam");
  expect(within(buttons[0]).getByText("Alex · Studio")).toBeVisible();
  expect(within(buttons[0]).getByText("Failed")).toBeVisible();
  expect(buttons[0]).toHaveTextContent("1.3 s");
  expect(buttons[0]).toHaveAccessibleDescription(/Image.*Alex.*Studio.*Failed.*1[.]3 s/);
  expect(within(table).getByRole("columnheader", { name: "Request receipt" })).toHaveAttribute("scope", "col");
  expect(within(buttons[0]).getByText(/1\.3 s/)).toHaveTextContent(
    /^.+ · 1\.3 s$/,
  );
  expect(buttons[0]).toHaveClass("min-h-(--row-2)", "max-sm:min-h-(--row-phone-2)");
  expect(buttons[1]).toHaveTextContent("0 ms");
  await user.click(buttons[0]);
  const receipt = await screen.findByRole("dialog");
  // The request ID is a technical detail (PF-045); it still identifies the receipt.
  expect(within(receipt).getByText("Request ID: newer")).toBeInTheDocument();
  expect(within(receipt).getByText("Studio")).toBeVisible();
  await user.keyboard("{Escape}");
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  await waitFor(() => expect(buttons[0]).toHaveFocus());
  await user.click(buttons[1]);
  expect(within(await screen.findByRole("dialog")).getByText("Request ID: older")).toBeInTheDocument();
});

it("marks a nested request in plain words, not as internal execution (PF-140)", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({
      records: [{
        receipt: { request_id: "child", parent_request_id: "parent", intent: "chat", status: "success" },
        person_label: "Sam", app_label: "Reader", created_at: 5,
      }],
      next_cursor: null, people: [], sources: [],
    }),
  }));
  mountRequests();
  const row = await screen.findByRole("button", { name: "Request details: Chat by Sam" });
  expect(within(row).getByText("Part of another request")).toBeVisible();
  expect(row).not.toHaveTextContent("Internal execution");
});

it("names the layer when the request list fails (PF-142)", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
  mountRequests();
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "The gateway's request list could not be loaded. We could not identify the cause. Try again.",
  );
});

it("sends the selected person and source as request filters", async () => {
  const user = userEvent.setup();
  const urls: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    urls.push(String(input));
    return Response.json({
      records: [{
        receipt: { request_id: "filtered", intent: "chat", status: "success" },
        person_label: "Owner", app_label: "Open WebUI", created_at: 1,
      }],
      next_cursor: null,
      people: [{ id: "owner", label: "Owner" }],
      sources: [{ id: "app-a", label: "Open WebUI" }],
    });
  }));
  mountRequests();
  await screen.findByRole("table", { name: "Request receipts" });
  await user.click(screen.getByRole("combobox", { name: "Person" }));
  await user.click(await screen.findByRole("option", { name: "Owner" }));
  await user.click(screen.getByRole("combobox", { name: "Source" }));
  await user.click(await screen.findByRole("option", { name: "Open WebUI" }));
  await waitFor(() =>
    expect(urls.some((url) => url.includes("person_id=owner") && url.includes("source=app-a"))).toBe(true),
  );
  expect(urls[0]).not.toContain("person_id");
  expect(urls[0]).not.toContain("source=");
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
  const row = await screen.findByRole("button", { name: "Request details: Voice by Sam" });
  expect(row).toHaveTextContent("Outcome not recorded");
  expect(row).toHaveTextContent("Time not recorded · Duration not recorded");
  expect(row).not.toHaveTextContent("0 ms");
});

it("COPY-10 filtered changes guidance", async () => {
  const user = userEvent.setup();
  mount();
  expect(await screen.findByText("Changes to apps, keys and access plans appear here.")).toBeVisible();
  expect(screen.getByRole("link", { name: "Go to Home" })).toBeVisible();
  await user.click(screen.getByRole("combobox", { name: "Change type" }));
  await user.click(screen.getByRole("option", { name: "Apps" }));
  expect(await screen.findByText("No matching changes")).toBeVisible();
  expect(screen.getByText("Changes to apps, keys and access plans appear here; none match these filters.")).toBeVisible();
  expect(screen.queryByRole("link", { name: "Go to Home" })).toBeNull();
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
  ["speech.synthesis", "Voice"],
  ["image.generate", "Image"],
  ["decide", "Decision"],
  ["embed", "Embedding"],
  ["audio.sfx", "Sound effect"],
  ["video.generate", "Video"],
  ["audio.transcriptions", "Transcription"],
  ["text.completions", "Chat"],
  ["mystery.operation", "AI request"],
])("COPY-13 activity receipt name %s", async (intent, title) => {
  serveCopyReceipt({ intent });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: `Request details: ${title} by Sam` }));
  expect(within(screen.getByRole("dialog")).getByRole("heading", { name: title })).toBeVisible();
});

it.each(["success", "succeeded", "ok"])("COPY-11 activity completion once (%s)", async (status) => {
  serveCopyReceipt({ intent: "image", status });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Request details: Image by Sam" }));
  const sheet = within(screen.getByRole("dialog"));
  expect(sheet.getAllByText("Completed")).toHaveLength(1);
  expect(sheet.queryByText("The gateway completed this request.")).toBeNull();
  expect(screen.getByRole("dialog")).toHaveAccessibleDescription("Request receipt");
});

it.each([undefined, "provider", "opaque_stage"])("COPY-08 activity failure uncertainty (%s)", async (stage) => {
  serveCopyReceipt({ intent: "chat", status: "failed", stage, error_code: "opaque_code" });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Request details: Chat by Sam" }));
  const sheet = within(screen.getByRole("dialog"));
  expect(sheet.getByText("We couldn't tell where this stopped. Try again.")).toBeVisible();
  // Stage and error code stay in Technical details (PF-045).
  expect(sheet.queryByText(/Error code: opaque_code/)).toBeNull();
  expect(sheet.getByText("Technical details").closest("details")).toHaveTextContent("opaque_code");
  expect(sheet.queryByText("The request failed.")).toBeNull();
});

it.each([
  ["rejected", "The gateway rejected this request."],
  ["in_progress", "The request was still running when this receipt was recorded."],
  ["timed_out", "The request timed out before we recorded a result. Try again."],
  ["incomplete", "We couldn't tell where this stopped. Check for a newer receipt, then try again."],
])("COPY-11 activity outcome context (%s)", async (status, summary) => {
  serveCopyReceipt({ intent: "chat", status });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Request details: Chat by Sam" }));
  const sheet = within(screen.getByRole("dialog"));
  expect(screen.getByRole("dialog")).toHaveAccessibleDescription("Request receipt");
  expect(sheet.getByText(summary)).toBeVisible();
});

it.each([
  ["a fallback model served", { requested_model: "safe-chat", resolved_model: "backup-chat", fallback_reasons: ["safe-chat->backup-chat (http_503)"] }, true],
  ["the requested model served", { requested_model: "safe-chat", resolved_model: "safe-chat" }, false],
])("shows requested and served models only when %s", async (_name, models, different) => {
  serveCopyReceipt({ intent: "chat", status: "success", ...models });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Request details: Chat by Sam" }));
  const sheet = within(screen.getByRole("dialog"));
  expect(sheet.queryByText("A different model was used.") !== null).toBe(different);
  expect(sheet.queryByText("Requested model") !== null).toBe(different);
  expect(sheet.queryByText("Served model") !== null).toBe(different);
  expect(sheet.queryByText("Model", { exact: true }) !== null).toBe(!different);
});

it("shows a name that points at a model as the name used, not as a substitution", async () => {
  serveCopyReceipt({ intent: "chat", status: "success", requested_model: "fast", resolved_model: "default-fast" });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Request details: Chat by Sam" }));
  const sheet = within(screen.getByRole("dialog"));
  expect(sheet.queryByText("A different model was used.")).toBeNull();
  expect(sheet.getByText("Name used")).toBeVisible();
  expect(sheet.getByText("Model", { exact: true })).toBeVisible();
  expect(sheet.queryByText("Requested model")).toBeNull();
});

it.each(["timed_out", "disconnected"])("points a media %s receipt to Library before a retry", async (status) => {
  serveCopyReceipt({ intent: "image.generate", status });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Request details: Image by Sam" }));
  expect(within(screen.getByRole("dialog")).getByText(/Check Library for a result before trying again\./)).toBeVisible();
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
  expect(screen.getByText("Paid models run with providers.")).toBeVisible();
  expect(screen.getByText("A request uses a provider only when its model names one.")).toBeVisible();
  expect(screen.queryByText(/Paid access is never automatic/)).toBeNull();
});

it.each([
  ["Remove provider", "Remove Example Provider from this gateway?"],
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
  await user.click(screen.getByRole("button", { name: "Check charges" }));
  expect(await screen.findByText(`USD ${amount}`)).toBeVisible();
});

it("COPY-18 access plan names", async () => {
  mountCopyProviders();
  const user = userEvent.setup();
  await screen.findByRole("heading", { name: "Example Provider" });
  await user.click(screen.getByText("Manage"));
  // Read-only: an access plan's Paid providers decide who can use a provider.
  expect(screen.queryByRole("button", { name: "Edit access plans" })).toBeNull();
  expect(screen.getAllByText("No access plan includes Example Provider.")[0]).toBeInTheDocument();
});

it("COPY-19 backend access guidance", () => {
  render(
    <ViewerContext.Provider value={{ authenticated: true, name: "Sam", email: null, role: "member", admin: false }}>
      <EnginesPage />
    </ViewerContext.Provider>,
  );
  expect(screen.getByText("Only an owner or admin can manage engines. Ask an owner or admin for help.")).toBeVisible();
  expect(screen.queryByText(/Sign in with an owner account/)).toBeNull();
});

it("announces final request pagination and keeps focus on the end message", async () => {
  const user = userEvent.setup();
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const more = String(input).includes("before=older");
    return Response.json({
      records: more ? [] : [{
        receipt: { request_id: "first", intent: "chat", status: "success" },
        person_label: "Sam", app_label: "Reader", created_at: 1,
      }],
      next_cursor: more ? null : "older", people: [], sources: [],
    });
  }));
  mountRequests();
  const button = await screen.findByRole("button", { name: "Load more" });
  button.focus();
  await user.keyboard("{Enter}");
  const end = await screen.findByText("End of request receipts");
  await waitFor(() => expect(end).toHaveFocus());
  const status = screen.getByRole("status", { name: "Request updates" });
  expect(status).toHaveTextContent("1 request loaded. End of request receipts.");
  expect(status).toHaveAttribute("aria-live", "polite");
  expect(status).toHaveAttribute("aria-atomic", "true");
});

it("recognizes fallback_model without fallback reasons in the receipt sheet", async () => {
  serveCopyReceipt({
    intent: "chat", status: "success",
    requested_model: "primary-chat", resolved_model: "backup-chat",
    fallback_model: "backup-chat",
  });
  await userEvent.setup().click(await screen.findByRole("button", { name: "Request details: Chat by Sam" }));
  const sheet = within(screen.getByRole("dialog"));
  expect(sheet.queryByText("A different model was used.")).toBeVisible();
  expect(sheet.getByText("Requested model").nextElementSibling).toHaveTextContent(/primary/i);
  expect(sheet.getByText("Served model").nextElementSibling).toHaveTextContent(/backup/i);
  expect(sheet.queryByText("Name used")).toBeNull();
});

it("does not claim a substitution when fallback metadata records the same served model", async () => {
  serveCopyReceipt({
    intent: "chat", status: "success",
    requested_model: "primary-chat", resolved_model: "primary-chat",
    fallback_model: "primary-chat", fallback_reasons: ["retry after timeout"],
  });
  await userEvent.setup().click(await screen.findByRole("button", { name: "Request details: Chat by Sam" }));
  const sheet = within(screen.getByRole("dialog"));
  expect(sheet.queryByText("A different model was used.")).toBeNull();
  expect(sheet.queryByText("Requested model")).toBeNull();
  expect(sheet.queryByText("Served model")).toBeNull();
  expect(sheet.getByText("Model", { exact: true })).toBeVisible();
});

it("renders backend_failed as failed in both the request row and its opened sheet", async () => {
  serveCopyReceipt({ intent: "chat", status: "backend_failed" });
  const row = await screen.findByRole("button", { name: "Request details: Chat by Sam" });
  expect(within(row).getByText("Failed")).toBeVisible();
  expect(row).not.toHaveTextContent("Outcome not recorded");
  await userEvent.setup().click(row);
  const sheet = within(screen.getByRole("dialog"));
  expect(sheet.getByText("Failed", { exact: true })).toBeVisible();
  expect(sheet.getByText("We couldn't tell where this stopped. Try again.")).toBeVisible();
});
