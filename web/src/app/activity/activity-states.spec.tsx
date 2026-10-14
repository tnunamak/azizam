import { afterEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import Activity from "./page";
import { StateBadge } from "@/components/product/status-ui";

// Radix Select and Sheet call these; jsdom lacks them.
for (const method of [
  "hasPointerCapture",
  "setPointerCapture",
  "releasePointerCapture",
  "scrollIntoView",
] as const)
  Object.defineProperty(HTMLElement.prototype, method, {
    configurable: true,
    value: () => false,
  });

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

type Entry = Awaited<ReturnType<typeof api.audit.list>>["records"][number];
const entry = (row: Record<string, unknown>) => row as unknown as Entry;

/** Mounts the Activity page. `audit` stands in for the gateway's audit endpoint. */
function mountActivity(opts: {
  audit?: (
    params: Parameters<typeof api.audit.list>[0],
  ) => ReturnType<typeof api.audit.list>;
  requests?: unknown;
  tab?: "Changes";
}) {
  const requestUrls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/admin/api/activity/requests"))
        requestUrls.push(url);
      return {
        ok: true,
        json: async () =>
          url.startsWith("/admin/api/activity/requests")
            ? (opts.requests ?? {
                records: [],
                next_cursor: null,
                people: [],
              })
            : {},
      };
    }),
  );
  vi.spyOn(api.users, "list").mockResolvedValue([]);
  const audit = vi
    .spyOn(api.audit, "list")
    .mockImplementation(
      (params) =>
        opts.audit?.(params) ??
        Promise.resolve({ records: [], next_cursor: null }),
    );
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
  if (opts.tab === "Changes")
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Changes" }), {
      button: 0,
      ctrlKey: false,
    });
  return { audit, requestUrls };
}

it("hides owner access history until the filter is enabled, and asks the server for it", async () => {
  const user = userEvent.setup();
  const rows = [
    entry({
      id: "setting-change",
      action: "operator.instance.update",
      actor_id: "owner-user",
      actor_label: "owner@example.test",
      target_kind: "operator_setting",
      target_id: "instance",
      metadata: {},
      created_at: 1_760_000_000,
    }),
    entry({
      id: "usage-view",
      action: "usage.summary.everyone",
      actor_id: "owner-user",
      actor_label: "owner@example.test",
      target_kind: "usage",
      target_id: "everyone",
      metadata: {},
      created_at: 1_760_000_001,
    }),
  ];
  const { audit } = mountActivity({
    tab: "Changes",
    audit: async (params) => ({
      records: params?.exclude_access_audit ? rows.slice(0, 1) : rows,
      next_cursor: null,
    }),
  });
  expect(
    await screen.findByRole("button", { name: /Updated instance settings/ }),
  ).toBeVisible();
  expect(
    screen.queryByRole("button", { name: /Viewed everyone’s usage/ }),
  ).toBeNull();
  expect(audit).toHaveBeenCalledWith(
    expect.objectContaining({ exclude_access_audit: true }),
  );
  expect(screen.queryByText("owner@example.test")).toBeNull();

  await user.click(screen.getByRole("button", { name: "Show access history" }));
  expect(
    await screen.findByRole("button", { name: /Viewed everyone’s usage/ }),
  ).toBeVisible();
  expect(audit).toHaveBeenCalledWith(
    expect.objectContaining({ exclude_access_audit: false }),
  );
  await user.click(screen.getByRole("button", { name: "Hide access history" }));
  await waitFor(() =>
    expect(
      screen.queryByRole("button", { name: /Viewed everyone’s usage/ }),
    ).toBeNull(),
  );
});

it("groups automated changes by default and uses plain wording when shown", async () => {
  const user = userEvent.setup();
  const rows = [
    entry({
      id: "human-backfill",
      action: "operator.service_registry.backfill",
      actor_kind: "user",
      actor_id: "owner-user",
      actor_label: "Owner",
      target_kind: "operator_setting",
      target_id: "service_registry",
      metadata: {},
      created_at: 1_760_000_000,
    }),
    entry({
      id: "automated-key",
      action: "credential.create",
      actor_kind: "service_account",
      actor_id: "svc_deploy",
      actor_label: "safe-restart",
      target_kind: "credential",
      target_id: "cred_1",
      metadata: {},
      created_at: 1_760_000_002,
    }),
    entry({
      id: "automated-app",
      action: "app.create",
      actor_kind: "service_account",
      actor_id: "svc_deploy",
      actor_label: "safe-restart",
      target_kind: "app",
      target_id: "app_1",
      metadata: {},
      created_at: 1_760_000_001,
    }),
  ];
  mountActivity({
    tab: "Changes",
    audit: async (params) => ({
      records: params?.show_automated
        ? rows
        : rows.filter((row) => row.actor_kind !== "service_account"),
      next_cursor: null,
      automated_count: 2,
    }),
  });
  expect(await screen.findByText("2 automated changes hidden")).toBeVisible();
  expect(
    screen.getByRole("button", { name: /Updated engine configuration/ }),
  ).toBeVisible();
  expect(screen.queryByRole("button", { name: /Created a key/ })).toBeNull();

  await user.click(
    screen.getByRole("button", { name: "Show automated changes" }),
  );
  expect(await screen.findByText("Showing 2 automated changes")).toBeVisible();
  expect(
    await screen.findByRole("button", { name: /Created a key by Automation/ }),
  ).toBeVisible();
  expect(document.body.textContent).not.toMatch(/registry|safe-restart/i);
});

it("keeps a system change label generic until its technical details are opened", async () => {
  const user = userEvent.setup();
  const rawLabel =
    "registry-reconstruct-20261001 (restore Sep 30 pre-damage + phase5b/6 changes)";
  mountActivity({
    tab: "Changes",
    audit: async () => ({
      records: [
        entry({
          id: "system-reconstruction",
          action: "operator.service_registry.backfill",
          actor_kind: "system",
          actor_id: "startup-task",
          actor_label: rawLabel,
          target_kind: "operator_setting",
          target_id: "service_registry",
          metadata: { updated_by: rawLabel },
          created_at: 1_760_000_000,
        }),
      ],
      next_cursor: null,
      automated_count: 0,
    }),
  });
  const change = await screen.findByRole("button", {
    name: /Updated engine configuration by System/,
  });
  expect(change).toHaveTextContent("System · Gateway settings");
  expect(screen.queryByText(rawLabel)).toBeNull();

  await user.click(change);
  await user.click(await screen.findByText("Technical details"));
  expect(screen.getByLabelText("Technical change details")).toHaveTextContent(
    rawLabel,
  );
});

const seededRequests = {
  records: [
    {
      receipt: {
        request_id: "req-1",
        intent: "chat",
        status: "success",
        requested_model: "demo-chat",
        resolved_model: "demo-chat",
        latency_ms: 420,
      },
      person_label: "Mira",
      app_label: "Notes",
      created_at: 1_790_870_400,
    },
    {
      receipt: { request_id: "req-2", intent: "image", status: "success" },
      person_label: "Sam",
      app_label: "Reader",
      created_at: 1_790_870_300,
    },
  ],
  people: [
    { id: "person-mira", label: "Mira" },
    { id: "person-sam", label: "Sam" },
  ],
  next_cursor: null,
};

it("opens a request with its outcome in the body, filters by person and clears the filter", async () => {
  const user = userEvent.setup();
  const { requestUrls } = mountActivity({ requests: seededRequests });
  await user.click(
    await screen.findByRole("button", { name: "Request details: Chat by Mira" }),
  );
  const dialog = await screen.findByRole("dialog");
  expect(dialog).toHaveTextContent("Completed");
  // The outcome sits in the sheet body; the description only names the sheet.
  expect(
    dialog.querySelector('[data-slot="sheet-description"]'),
  ).not.toHaveTextContent("Completed");
  expect(dialog.querySelector("pre")).not.toBeVisible();
  await user.click(within(dialog).getByText("Technical details"));
  expect(dialog.querySelector("pre")).toBeVisible();
  await user.keyboard("{Escape}");
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

  await user.click(screen.getByRole("combobox", { name: "Person" }));
  const options = await screen.findAllByRole("option");
  expect(options.map((option) => option.textContent)).toEqual([
    "Everyone",
    "Mira",
    "Sam",
  ]);
  await user.click(options[1]);
  await waitFor(() =>
    expect(requestUrls.some((url) => url.includes("person_id=person-mira"))).toBe(
      true,
    ),
  );
  await user.click(screen.getByRole("button", { name: "Clear filters" }));
  expect(screen.queryByRole("button", { name: "Clear filters" })).toBeNull();
  expect(screen.getByRole("combobox", { name: "Person" })).toHaveFocus();
  expect(
    within(screen.getByRole("combobox", { name: "Person" })).getByText(
      "Everyone",
    ),
  ).toBeVisible();
});

it.each([
  ["disconnected", /before we recorded a result\. Try again\./],
  ["timed_out", /before we recorded a result\. Try again\./],
  ["incomplete", /Check for a newer receipt, then try again/],
])(
  "keeps routing details of a %s receipt private and offers a next action",
  async (status, next) => {
    const user = userEvent.setup();
    mountActivity({
      requests: {
        records: [
          {
            person_label: "Mira",
            app_label: "Notes",
            created_at: 1_790_870_400,
            receipt: {
              request_id: "private-request-id",
              intent: "chat",
              status,
              requested_model: "demo-chat",
              resolved_model: "demo-chat",
              parent_request_id: "parent",
              stage: "private-stage",
              error_code: "private-code",
              warnings: ["private-warning"],
            },
          },
        ],
        people: [],
        next_cursor: null,
      },
    });
    await user.click(
      await screen.findByRole("button", { name: /Request details:/ }),
    );
    const sheet = within(await screen.findByRole("dialog"));
    expect(sheet.getByText("Model")).toBeVisible();
    expect(sheet.queryByText("Requested model")).toBeNull();
    expect(sheet.queryByText("Served model")).toBeNull();
    expect(sheet.getAllByText("Mira")).toHaveLength(1);
    expect(sheet.getAllByText("Notes")).toHaveLength(1);
    expect(sheet.getByText(next)).toBeVisible();
    const technical = screen
      .getByRole("dialog")
      .querySelector("details") as HTMLDetailsElement;
    expect(technical).not.toHaveAttribute("open");
    expect(technical.querySelector("pre")).not.toBeVisible();
    await user.click(within(technical).getByText("Technical details"));
    expect(technical).toHaveAttribute("open");
    expect(technical).toHaveTextContent("private-code");
    expect(
      within(technical).getByText("Part of another request"),
    ).toBeVisible();
  },
);

it("preserves keyboard focus through change details and final pagination", async () => {
  const user = userEvent.setup();
  mountActivity({
    tab: "Changes",
    audit: async (params) => ({
      records: params?.before ? [] : [entry({
        id: "a11y-change", action: "app.create", actor_id: "sam",
        actor_kind: "user", actor_label: "Sam", target_kind: "app",
        target_id: "reader", metadata: { name: "Reader" }, created_at: 1,
      })],
      next_cursor: params?.before ? null : "older",
    }),
  });
  const row = await screen.findByRole("button", { name: "Details: Created an app by Sam" });
  expect(row).toHaveAccessibleDescription(/Created an app.*Sam.*Reader/);
  row.focus();
  await user.keyboard("{Enter}");
  expect(await screen.findByRole("dialog")).toBeVisible();
  await user.keyboard("{Escape}");
  await waitFor(() => expect(row).toHaveFocus());
  const more = screen.getByRole("button", { name: "Load more" });
  more.focus();
  await user.keyboard("{Enter}");
  const end = await screen.findByText("End of changes");
  await waitFor(() => expect(end).toHaveFocus());
  expect(screen.getByRole("status", { name: "Changes updates" }))
    .toHaveTextContent("1 change loaded. End of changes.");
});

it("empty requests have a purpose and a destination that matches the action", async () => {
  mountActivity({});
  const region = await screen.findByRole("region", { name: "No request receipts yet" });
  expect(region).toHaveAccessibleDescription("Requests from apps and devices on your trusted network appear here.");
  expect(within(region).getAllByRole("link")).toHaveLength(1);
  expect(within(region).getByRole("link", { name: "Go to Home" })).toHaveAttribute("href", "/");
  expect(within(region).queryByRole("button")).toBeNull();
});

it("empty request filters have one reset action that restores the unfiltered state", async () => {
  mountActivity({ requests: { records: [], next_cursor: null, people: [{ id: "mira", label: "Mira" }] } });
  await screen.findByRole("region", { name: "No request receipts yet" });
  const user = userEvent.setup();
  await user.click(screen.getByRole("combobox", { name: "Person" }));
  await user.click(await screen.findByRole("option", { name: "Mira" }));
  const region = await screen.findByRole("region", { name: "No matching requests" });
  expect(region).toHaveAccessibleDescription("Receipts show request outcomes from your apps and devices; none match these filters.");
  expect(within(region).getAllByRole("button")).toHaveLength(1);
  expect(within(region).queryByRole("link")).toBeNull();
  await user.click(within(region).getByRole("button", { name: "Clear filters" }));
  expect(await screen.findByRole("region", { name: "No request receipts yet" })).toBeVisible();
  expect(screen.getByRole("combobox", { name: "Person" })).toHaveTextContent("Everyone");
});

it("empty changes explain their purpose without offering an unverified creation permission", async () => {
  mountActivity({ tab: "Changes" });
  const region = await screen.findByRole("region", { name: "No changes yet" });
  expect(region).toHaveAccessibleDescription("Changes to apps, keys and access plans appear here.");
  expect(within(region).getAllByRole("link")).toHaveLength(1);
  expect(within(region).getByRole("link", { name: "Go to Home" })).toHaveAttribute("href", "/");
  expect(within(region).queryByRole("link", { name: "Create an app" })).toBeNull();
});

it("empty changes reveal existing automated records instead of claiming there are no changes", async () => {
  const row = entry({
    id: "automated-empty-state",
    action: "app.create",
    actor_kind: "service_account",
    actor_id: "service",
    actor_label: "Automation",
    target_kind: "app",
    target_id: "app",
    metadata: {},
    created_at: 1_760_000_000,
  });
  mountActivity({
    tab: "Changes",
    audit: async (params) => ({
      records: params?.show_automated ? [row] : [],
      next_cursor: null,
      automated_count: 1,
    }),
  });
  expect(await screen.findByText("1 automated change hidden")).toBeVisible();
  const region = await screen.findByRole("region", { name: "No visible changes" });
  expect(region).toHaveAccessibleDescription("Changes to apps, keys and access plans appear here; automated changes are currently hidden.");
  expect(within(region).getAllByRole("button")).toHaveLength(1);
  await userEvent.setup().click(within(region).getByRole("button", { name: "Show automated changes" }));
  expect(await screen.findByRole("button", { name: /Created an app/ })).toBeVisible();
  expect(screen.queryByRole("region", { name: "No visible changes" })).toBeNull();
  expect(screen.getByText("Showing 1 automated change")).toBeVisible();
});

function mountOutcome(receipt: Record<string, unknown>) {
  mountActivity({ requests: {
    records: [{ person_label: "Mira", app_label: "Notes", created_at: 1,
      receipt: { request_id: "outcome", intent: "chat", ...receipt } }],
    people: [], next_cursor: null,
  } });
}

it.each([
  [401, "The sign-in or app-key check refused this request. Check the app's access key for this person, then try again."],
  [403, "This request was rejected because it did not have permission. Check this person's access plan and the app's access."],
  [429, "A request limit refused this request. The receipt does not identify which limit. Wait before trying again; if it happens again, ask the owner to check the request limits."],
  [500, "A server error prevented this request from completing. No refusal rule was recorded. Try again; if it happens again, share the request ID from Technical details with the owner."],
  [599, "A server error prevented this request from completing. No refusal rule was recorded. Try again; if it happens again, share the request ID from Technical details with the owner."],
])("describes stored rejection %s in the past tense", async (statusCode, message) => {
  mountOutcome({ status: "rejected", metadata: { status_code: statusCode } });
  await userEvent.click(await screen.findByRole("button", { name: /Request details:/ }));
  expect(within(screen.getByRole("dialog")).getByText(message)).toBeVisible();
  if (statusCode === 403) expect(screen.getByRole("dialog")).toHaveTextContent("Access check: the receipt does not identify the specific rule.");
  expect(screen.getByRole("dialog")).not.toHaveTextContent("Sign in to continue.");
  expect(screen.getByRole("dialog")).not.toHaveTextContent("Your account does not have permission");
});

it.each([400, 404, 600])("does not invent a cause for stored status %s", async (status_code) => {
  mountOutcome({ status: "rejected", status_code });
  await userEvent.click(await screen.findByRole("button", { name: /Request details:/ }));
  const summary = within(screen.getByRole("dialog")).getByText("The gateway rejected this request.").parentElement;
  expect(summary?.querySelectorAll("p")).toHaveLength(3);
  expect(summary).toHaveTextContent("The receipt does not identify the rule that refused this request. Share the request ID from Technical details with the owner to check before trying again.");
});

it.each([
  ["timed_out", "Timed out", "degraded"],
  ["disconnected", "Disconnected", "degraded"],
  ["incomplete", "Outcome not recorded", "unknown"],
  ["stale_incomplete", "Outcome not recorded", "unknown"],
  [undefined, "Outcome not recorded", "unknown"],
  ["unrecognized", "Outcome not recorded", "unknown"],
  ["accepted", "Accepted", "busy"],
  ["cancelled", "Cancelled", "unknown"],
] as const)("visually distinguishes the %s outcome from completion", async (status, label, state) => {
  mountOutcome({ status });
  const row = await screen.findByRole("button", { name: /Request details:/ });
  const reference = render(<StateBadge state={state} word={label} />);
  const expected = reference.container.firstElementChild;
  const outcome = row.querySelector('[id$="-outcome"]')?.firstElementChild;
  expect(outcome?.outerHTML).toBe(expected?.outerHTML);
  expect(outcome?.querySelector("svg")).not.toBeNull();
});

it.each(["success", "succeeded", "ok"])("keeps %s completion quiet", async (status) => {
  mountOutcome({ status });
  const row = await screen.findByRole("button", { name: /Request details:/ });
  expect(within(row).getByText("Completed")).toHaveClass("text-foreground");
  expect(row.querySelector('[id$="-outcome"]')?.querySelector("svg")).toBeNull();
});

it.each([undefined, null, -1])("records unknown duration in row and details (%s)", async (latency_ms) => {
  mountOutcome({ status: "success", latency_ms });
  const row = await screen.findByRole("button", { name: /Request details:/ });
  expect(row).toHaveTextContent("Duration not recorded");
  await userEvent.click(row);
  const label = within(screen.getByRole("dialog")).getByText("Time taken");
  expect(label.nextElementSibling).toHaveTextContent("Duration not recorded");
});

it("resets a source-only empty result to all sources from its empty-state action", async () => {
  const { requestUrls } = mountActivity({
    requests: {
      records: [], next_cursor: null, people: [],
      sources: [{ id: "reader-app", label: "Reader" }],
    },
  });
  await screen.findByRole("region", { name: "No request receipts yet" });
  const user = userEvent.setup();
  await user.click(screen.getByRole("combobox", { name: "Source" }));
  await user.click(await screen.findByRole("option", { name: "Reader" }));
  const region = await screen.findByRole("region", { name: "No matching requests" });
  expect(region).toHaveAccessibleDescription("Receipts show request outcomes from your apps and devices; none match these filters.");
  await waitFor(() => expect(requestUrls.at(-1)).toContain("source=reader-app"));
  expect(requestUrls.at(-1)).not.toContain("person_id");
  await user.click(within(region).getByRole("button", { name: "Clear filters" }));
  expect(await screen.findByRole("region", { name: "No request receipts yet" })).toBeVisible();
  expect(screen.getByRole("combobox", { name: "Source" })).toHaveTextContent("All sources");
  expect(screen.getByRole("combobox", { name: "Person" })).toHaveFocus();
  expect(screen.queryByRole("button", { name: "Clear filters" })).toBeNull();
});

const filterRow = (id: string, name = id) => entry({
  id, action: "app.create", actor_id: "sam", actor_kind: "user",
  actor_label: "Sam", target_kind: "app", target_id: id,
  metadata: { name }, created_at: 1_760_000_000,
});

async function pickChangeFilter(label: string, option: string) {
  const user = userEvent.setup();
  await user.click(screen.getByRole("combobox", { name: label }));
  await user.click(await screen.findByRole("option", { name: option, exact: true }));
}

it("filter regression: change-type pick-list sends each category and restores all changes", async () => {
  const { audit } = mountActivity({ tab: "Changes", audit: async () => ({
    records: [filterRow("pick-list")], next_cursor: null,
  }) });
  await screen.findByRole("button", { name: /Details: Created an app/ });
  for (const [label, prefix] of [
    ["Apps", "app."], ["Keys", "credential."], ["Access plans", "profile."],
    ["Gateway settings", "operator."], ["Access assignments", "runtime_profile_mapping."],
    ["All changes", undefined],
  ] as const) {
    audit.mockClear();
    await pickChangeFilter("Change type", label);
    await waitFor(() => expect(audit).toHaveBeenCalledWith(expect.objectContaining({
      limit: 50, action_prefix: prefix, actor_id: undefined, before: undefined,
    })));
    expect(screen.getByRole("combobox", { name: "Change type" })).toHaveTextContent(label);
  }
});

it("filter regression: Clear filters resets both selectors and refetches the unfiltered list", async () => {
  const { audit } = mountActivity({ tab: "Changes", audit: async (params) => ({
    records: [filterRow(params?.action_prefix || params?.actor_id ? "filtered" : "unfiltered")],
    next_cursor: null,
  }) });
  await screen.findByText("Sam · unfiltered");
  await pickChangeFilter("Change type", "Apps");
  await pickChangeFilter("Person", "Sam");
  await screen.findByText("Sam · filtered");
  audit.mockClear();
  await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
  expect(await screen.findByText("Sam · unfiltered")).toBeVisible();
  await waitFor(() => expect(audit).toHaveBeenCalledWith(expect.objectContaining({
    limit: 50, action_prefix: undefined, actor_id: undefined,
  })));
  expect(screen.getByRole("combobox", { name: "Change type" })).toHaveTextContent("All changes");
  expect(screen.getByRole("combobox", { name: "Person" })).toHaveTextContent("Everyone");
  expect(screen.getByRole("combobox", { name: "Change type" })).toHaveFocus();
  expect(screen.queryByRole("button", { name: "Clear filters" })).toBeNull();
});

it("filter regression: empty filtered changes explain no matches and can clear from that state", async () => {
  mountActivity({ tab: "Changes", audit: async (params) => ({
    records: params?.action_prefix ? [] : [filterRow("visible-before-filter")],
    next_cursor: null,
  }) });
  await screen.findByText("Sam · visible-before-filter");
  await pickChangeFilter("Change type", "Keys");
  const empty = await screen.findByRole("region", { name: "No matching changes" });
  expect(empty).toHaveAccessibleDescription("Changes to apps, keys and access plans appear here; none match these filters.");
  expect(screen.queryByText("Sam · visible-before-filter")).toBeNull();
  expect(screen.queryByRole("region", { name: "No changes yet" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  await userEvent.click(within(empty).getByRole("button", { name: "Clear filters" }));
  expect(await screen.findByText("Sam · visible-before-filter")).toBeVisible();
  expect(screen.queryByRole("region", { name: "No matching changes" })).toBeNull();
});

it("filter regression: a 503 failure retries the same filtered changes and recovers", async () => {
  let unavailable = true;
  const { audit } = mountActivity({ tab: "Changes", audit: async (params) => {
    if (params?.action_prefix && unavailable) {
      throw Object.assign(new Error("Service Unavailable"), { status: 503 });
    }
    return { records: [filterRow(params?.action_prefix ? "recovered-filter" : "initial-row")], next_cursor: null };
  } });
  await screen.findByText("Sam · initial-row");
  await pickChangeFilter("Change type", "Apps");
  expect(await screen.findByText("Couldn't load changes")).toBeVisible();
  expect(screen.queryByRole("region", { name: "No matching changes" })).toBeNull();
  expect(screen.queryByText("Sam · initial-row")).toBeNull();
  expect(screen.getByRole("combobox", { name: "Change type" })).toHaveTextContent("Apps");
  audit.mockClear();
  unavailable = false;
  await userEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(await screen.findByText("Sam · recovered-filter")).toBeVisible();
  expect(audit).toHaveBeenCalledTimes(1);
  expect(audit).toHaveBeenLastCalledWith(expect.objectContaining({ limit: 50, action_prefix: "app." }));
  expect(screen.queryByText("Couldn't load changes")).toBeNull();
});

it("filter regression: paging retains both filters and appends older matching changes", async () => {
  const { audit } = mountActivity({ tab: "Changes", audit: async (params) => ({
    records: [filterRow(params?.before ? "older-match" : "first-match")],
    next_cursor: params?.before ? null : "older-filter-cursor",
  }) });
  await screen.findByText("Sam · first-match");
  await pickChangeFilter("Change type", "Apps");
  await pickChangeFilter("Person", "Sam");
  await waitFor(() => expect(audit).toHaveBeenCalledWith(expect.objectContaining({
    limit: 50, action_prefix: "app.", actor_id: "sam", before: undefined,
  })));
  audit.mockClear();
  await userEvent.click(screen.getByRole("button", { name: "Load more" }));
  expect(await screen.findByText("Sam · older-match")).toBeVisible();
  expect(screen.getByText("Sam · first-match")).toBeVisible();
  expect(audit).toHaveBeenCalledTimes(1);
  expect(audit).toHaveBeenLastCalledWith({
    limit: 50, action_prefix: "app.", actor_id: "sam",
    before: "older-filter-cursor", exclude_access_audit: true, show_automated: false,
  });
  expect(screen.getByRole("combobox", { name: "Change type" })).toHaveTextContent("Apps");
  expect(screen.getByRole("combobox", { name: "Person" })).toHaveTextContent("Sam");
  expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  expect(screen.getByRole("status", { name: "Changes updates" })).toHaveTextContent("2 changes loaded. No more matching changes.");
});
