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
