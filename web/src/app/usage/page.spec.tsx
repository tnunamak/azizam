import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { ViewerContext } from "@/components/shell/session";
import UsagePage from "./page";
import { Requests } from "../activity/requests";
import WorkflowsPage from "../workflows/page";

vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: ReactNode }) => <>{children}</>,
  Bar: ({ dataKey, stackId, fill }: { dataKey: string; stackId?: string; fill: string }) =>
    <span data-testid="usage-bar" data-key={dataKey} data-stack={stackId} data-fill={fill} />,
  BarChart: ({ children, data }: { children: ReactNode; data: unknown }) =>
    <div data-testid="usage-chart" data-values={JSON.stringify(data)}>{children}</div>,
  CartesianGrid: () => null,
  Tooltip: () => null,
  XAxis: () => null,
  YAxis: () => null,
}));
function renderUsage(
  summary: Record<string, unknown>,
  fetchSummary?: () => Promise<Response>,
  lookups: Record<string, unknown> = {},
  role: "owner" | "member" = "owner",
  Page = UsagePage,
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // Only the usage summary follows the test's script; the name lookups
  // (model catalog, home) answer with an empty payload.
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) =>
      String(input).includes("/usage/summary")
        ? (fetchSummary ?? (async () => Response.json({ media_count: 0, requests: 4, ...summary })))()
        : Promise.resolve(Response.json(lookups)),
    ),
  );
  const view = render(
    <ViewerContext.Provider
      value={{
        authenticated: true,
        name: "Owner",
        email: null,
        role,
        admin: role === "owner",
      }}
    >
      <QueryClientProvider client={client}>
        <Page />
      </QueryClientProvider>
    </ViewerContext.Provider>,
  );
  return { ...view, client };
}
HTMLElement.prototype.scrollIntoView = vi.fn();
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test.each(["owner", "member"] as const)("Usage shows full-period failures in the fourth summary figure for %s", async (role) => {
  renderUsage({
    requests: 150,
    failed_requests: 3,
    receipts: Array.from({ length: 50 }, (_, index) => ({ request_id: `recent-${index}`, status: "succeeded" })),
  }, undefined, {}, role);
  const label = await screen.findByText("Failed", { selector: "dt" });
  const stat = label.closest<HTMLElement>('[data-slot="stat"]')!;
  expect(within(stat).getByText("3 · 2%")).toBeVisible();
  const strip = label.closest('[data-slot="stat-strip"]')!;
  expect(strip.querySelectorAll('[data-slot="stat"]')).toHaveLength(4);
  expect(strip.lastElementChild).toBe(stat);
  expect(screen.queryByRole("button", { name: /Receipt details for/ })).not.toBeInTheDocument();
});

test.each([undefined, null])("Usage keeps absent failure totals Unknown despite recent receipts: %s", async (failed) => {
  renderUsage({
    failed_requests: failed,
    receipts: [{ request_id: "recent-failure", status: "failed" }],
  });
  const stat = (await screen.findByText("Failed", { selector: "dt" })).closest<HTMLElement>('[data-slot="stat"]')!;
  expect(within(stat).getByText("Unknown")).toBeVisible();
  expect(within(stat).queryByText("0 · 0%")).not.toBeInTheDocument();
});

test("Usage marks a failure count above zero with the warning badge", async () => {
  renderUsage({ requests: 20, failed_requests: 2 });
  const stat = (await screen.findByText("Failed", { selector: "dt" })).closest<HTMLElement>('[data-slot="stat"]')!;
  expect(within(stat).getByText("2 · 10%")).toBeVisible();
  const badge = stat.querySelector('[data-slot="badge"]');
  expect(badge).toBeNull();
  expect(within(stat).getAllByText("Failed")).toHaveLength(1);
});

test("Usage keeps zero failures quiet", async () => {
  renderUsage({ failed_requests: 0 });
  const stat = (await screen.findByText("Failed", { selector: "dt" })).closest<HTMLElement>('[data-slot="stat"]')!;
  expect(within(stat).getByText("0 · 0%")).toBeVisible();
  expect(stat.querySelector('[data-slot="badge"]')).toBeNull();
});

test("Usage does not divide a failure share by zero", async () => {
  renderUsage({ requests: 0, media_count: 1, failed_requests: 0 });
  const stat = (await screen.findByText("Failed", { selector: "dt" })).closest<HTMLElement>('[data-slot="stat"]')!;
  expect(within(stat).getByText("0 · Unknown")).toBeVisible();
});

test.each([0, 4])(
  "USAGE-01 keeps unknown paid spend visible with %i settled paid requests",
  async (paidRequests) => {
    renderUsage({
      paid_requests: paidRequests,
      unknown_paid_requests: 4,
      remote_spend: null,
      spend_measured_requests: 0,
      spend_total_requests: 4,
    });
    expect(await screen.findByText("Spend")).toBeVisible();
    expect(screen.getByText("Cost not recorded")).toBeVisible();
    expect(screen.getByText(/Costs recorded for 0 of 4 requests/)).toBeVisible();
    expect(screen.getByText(/4 paid requests have unknown cost/)).toBeVisible();
  },
);
test.each([
  {
    unknown: 2,
    measured: 2,
    label: "Spend",
    detail: /2 paid requests have unknown cost/,
  },
  {
    unknown: 0,
    measured: 4,
    label: "Spend",
    detail: /Costs recorded for 4 of 4 requests/,
  },
])(
  "USAGE-01 identifies completeness: $label",
  async ({ unknown, measured, label, detail }) => {
    renderUsage({
      paid_requests: 4,
      unknown_paid_requests: unknown,
      remote_spend: 1.25,
      spend_measured_requests: measured,
      spend_total_requests: 4,
    });
    expect(await screen.findByText(label)).toBeVisible();
    expect(screen.getByText("$1.25")).toBeVisible();
    expect(screen.getByText(detail)).toBeVisible();
  },
);
test("USAGE-01 does not claim complete coverage without measurement counts", async () => {
  renderUsage({ paid_requests: 4, remote_spend: 0 });
  expect(
    await screen.findByText("Spend"),
  ).toBeVisible();
  expect(screen.getByText("$0.00")).toBeVisible();
  expect(screen.getByText(/We don't know how many request costs were recorded./)).toBeVisible();
});
test("Usage exposes the compact filters and named dense model table", async () => {
  renderUsage({
    by_model: [{ model: "default", capability: "chat", requests: 4 }],
  });
  const table = await screen.findByRole("table", { name: "Models" });
  expect(table).toHaveAttribute("data-dense");
  expect(within(table).getByRole("columnheader", { name: "Task and model" })).toBeVisible();
  expect(within(table).getByRole("row", { name: /Chat/ })).toBeVisible();
  expect(screen.getByRole("combobox", { name: "Person" })).toHaveAttribute("data-size", "sm");
  expect(screen.getByRole("combobox", { name: "Source" })).toHaveAttribute("data-size", "sm");
});

test("USAGE-02 chart table includes each active capability and daily total", async () => {
  renderUsage({
    by_day: [
      { date: "2026-10-01", chat: 2, image: 1 },
      { date: "2026-10-02", image: 4 },
    ],
  });
  await userEvent.setup().click(await screen.findByText("View daily totals"));
  const table = screen.getByRole("table", { name: "Requests by day and task" });
  expect(
    within(table)
      .getAllByRole("columnheader")
      .map((cell) => cell.textContent),
  ).toEqual(["Date", "Chat", "Make a picture", "Total"]);
  const rows = within(table).getAllByRole("row").slice(1);
  expect(
    rows.map((row) =>
      within(row)
        .getAllByRole("cell")
        .map((cell) => cell.textContent),
    ),
  ).toEqual([
    ["2", "1", "3"],
    ["Unknown", "4", "4"],
  ]);

});
test("USAGE-03 anonymous people have distinct safe labels and retain filter IDs", async () => {
  renderUsage({
    people: [
      { id: "person-001", label: "one@example.com" },
      { id: "person-002", label: "two@example.com" },
      { id: "person-003" },
      { id: "person-004", label: "Mira" },
    ],
  });
  await screen.findByText("Requests");
  const user = userEvent.setup();
  screen.getByRole("combobox", { name: "Person" }).focus();
  await user.keyboard("{Enter}");
  const options = await screen.findAllByRole("option");
  const labels = options.map((option) => option.textContent ?? "");
  const anonymous = labels.filter((label) => label.startsWith("Person name not recorded"));
  expect(anonymous).toHaveLength(3);
  expect(new Set(anonymous).size).toBe(3);
  expect(labels.join(" ")).not.toContain("@example.com");
  expect(labels).toContain("Mira");
  // "self" is the person, not their apps (PF-183).
  expect(labels).toContain("You");
  expect(labels).not.toContain("Your apps");
  await user.click(
    options.find((option) => option.textContent === anonymous[1])!,
  );
  expect(
    vi
      .mocked(fetch)
      .mock.calls.some(([url]) => String(url).includes("person=person-002")),
  ).toBe(true);
});

test.each([
  { emails: ["first@example.com", "second@example.com"], suffixes: ["first", "second"] },
  { emails: [undefined, undefined], suffixes: ["#1", "#2"] },
])("USAGE-03 duplicate explicit names have stable accessible labels and retain filter IDs: $suffixes", async ({ emails, suffixes }) => {
  const people = [
    { id: "person-002", name: "Mira", email: emails[1] },
    { id: "person-001", name: "Mira", email: emails[0] },
    { id: "person-003", name: "Azizam" },
  ];
  renderUsage({ people });
  await screen.findByText("Requests");
  const user = userEvent.setup();
  const labels = suffixes.map((suffix) => `Mira · ${suffix}`);
  for (const [index, label] of labels.entries()) {
    const filter = screen.getByRole("combobox", { name: "Person" });
    filter.focus();
    await user.keyboard("{Enter}");
    const option = await screen.findByRole("option", { name: label });
    expect(option).toHaveAccessibleName(label);
    expect(screen.getByRole("option", { name: "Azizam" })).toBeVisible();
    expect(screen.getAllByRole("option").map((item) => item.textContent).join(" ")).not.toContain("@");
    expect(screen.getByRole("option", { name: labels[1 - index] })).toBeVisible();
    // A different response order must not change which person a label identifies.
    people.reverse();
    vi.mocked(fetch).mockClear();
    await user.click(option);
    expect(
      vi.mocked(fetch).mock.calls.some(([url]) =>
        new URL(String(url), "https://azizam.test").searchParams.get("person") === `person-00${index + 1}`,
      ),
    ).toBe(true);
    expect(await screen.findByRole("combobox", { name: "Person" })).toHaveTextContent(label);
  }
});

test.each([undefined, null])("UA-01 labels an unrecorded receipt cost in summary and details: %s", async (cost) => {
  renderUsage({
    receipts: [
      { request_id: "unknown", capability: "chat", cost_usd: cost },
      { request_id: "zero", capability: "image", cost_usd: 0 },
      { request_id: "local", capability: "video", cost_usd: cost, remote: false },
    ],
    by_model: [{ model: "unmeasured-model", capability: "chat", requests: 1 }],
  });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "View recent requests" }));
  const unknown = screen.getByRole("button", { name: "Receipt details for Chat request" });
  expect(within(unknown).getByText("Cost not recorded")).toBeInTheDocument();
  const zero = screen.getByRole("button", { name: "Receipt details for Image request" });
  expect(within(zero).queryByText("Cost not recorded")).not.toBeInTheDocument();
  expect(within(zero).getByText("$0.00")).toBeInTheDocument();
  const local = screen.getByRole("button", { name: "Receipt details for Video request" });
  expect(within(local).getByText("No provider charge")).toBeInTheDocument();
  // A model row with no locality evidence says Cost not recorded, never blank.
  expect(screen.getAllByText("Cost not recorded")).toHaveLength(4);
  const modelRow = screen.getByRole("row", { name: /unmeasured-model|Unmeasured Model/ });
  expect(within(modelRow).getByText("Cost not recorded")).toBeVisible();
  await user.click(unknown);
  const sheet = screen.getByRole("dialog");
  expect(within(sheet).getByText("Spend")).toBeVisible();
  expect(within(sheet).getByText("Cost not recorded")).toBeVisible();
});


function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

test("Usage announces loading with the shared Busy status and no invented totals", async () => {
  const pending = deferred<Response>();
  renderUsage({}, () => pending.promise);
  expect(screen.getByRole("status")).toHaveTextContent("Loading usage…");
  expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
  expect(screen.queryByText("No requests in this period")).not.toBeInTheDocument();
  await act(async () => pending.resolve(Response.json({ media_count: 0, requests: 0 })));
  expect(await screen.findByText("No requests in this period")).toBeVisible();
  expect(screen.getByRole("link", { name: "Connect an app" })).toHaveAttribute("href", "/apps/");
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});

test("Usage reports an Unknown load failure with a shared callout and retry", async () => {
  const retry = deferred<Response>();
  let calls = 0;
  renderUsage({}, () => ++calls === 1 ? Promise.reject(new Error("offline")) : retry.promise);
  const alert = await screen.findByRole("alert");
  expect(within(alert).getByRole("heading", { name: "Usage could not be loaded" })).toBeVisible();
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  expect(screen.queryByText("No requests in this period")).not.toBeInTheDocument();
  await userEvent.setup().click(within(alert).getByRole("button", { name: "Retry usage" }));
  expect(screen.getByRole("status")).toHaveTextContent("Loading usage…");
  await act(async () => retry.resolve(Response.json({ media_count: 0, requests: 4, remote_spend: 1.25 })));
  expect(await screen.findByText("$1.25")).toBeVisible();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

test.each([0, 4])("Usage keeps %i cached requests through refresh failure and retry", async (requests) => {
  const refresh = deferred<Response>();
  const retry = deferred<Response>();
  let calls = 0;
  const { client } = renderUsage({}, () => {
    calls += 1;
    if (calls === 1) return Promise.resolve(Response.json({ media_count: 0, requests, remote_spend: null }));
    return calls === 2 ? refresh.promise : retry.promise;
  });
  const cachedLabel = requests ? "Cost not recorded" : "No requests in this period";
  expect(await screen.findByText(cachedLabel)).toBeVisible();
  act(() => { void client.invalidateQueries({ queryKey: ["usage"] }); });
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Refreshing usage…"));
  expect(screen.getByText(cachedLabel)).toBeVisible();
  await act(async () => refresh.reject(new Error("refresh unavailable")));
  expect(await screen.findByRole("heading", { name: "Usage couldn’t refresh. These are the last loaded totals." })).toBeVisible();
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  expect(screen.getByText(cachedLabel)).toBeVisible();
  expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  await userEvent.setup().click(screen.getByRole("button", { name: "Retry usage" }));
  expect(screen.getByRole("button", { name: "Retrying…" })).toBeDisabled();
  expect(screen.getByText(cachedLabel)).toBeVisible();
  await act(async () => retry.resolve(Response.json({ media_count: 0, requests: 5, remote_spend: 2.5 })));
  expect(await screen.findByText("$2.50")).toBeVisible();
  expect(screen.queryByRole("heading", { name: "Usage couldn’t refresh. These are the last loaded totals." })).not.toBeInTheDocument();
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});

test.each([undefined, null, "", "   ", false])("Usage never formats unrecorded spend as zero: %s", async (spend) => {
  renderUsage({ remote_spend: spend, remote_spend_available: true });
  expect(await screen.findByText("Cost not recorded")).toBeVisible();
  expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
});

test("Usage uses shared empty panels for missing daily and model breakdowns", async () => {
  renderUsage({});
  for (const title of [
    "Daily totals weren’t recorded for these requests.",
    "Model totals weren’t recorded.",
  ]) {
    expect((await screen.findByText(title)).closest(".border-dashed")).toBeInTheDocument();
  }
});

const GGUF = "/media/windows/AI Models/LLMs/GGUF/Qwen3.8-27B-Uncensored-Q4_K_M.gguf";

test("Usage preserves exact path IDs for distinct catalog names and receipt lookups", async () => {
  const other = GGUF.replace("/media/windows/", "/other/");
  renderUsage({
    by_model: [
      { model: GGUF, capability: "chat", requests: 2 },
      { model: other, capability: "chat", requests: 1 },
    ],
    receipts: [{ request_id: "exact", capability: "chat", requested_model: other, resolved_model: GGUF }],
  }, undefined, { models: [{ id: GGUF, title: "Main chat" }, { id: other, title: "Backup chat" }] });
  expect(await screen.findByTitle("Main chat")).toBeVisible();
  expect(await screen.findByTitle("Backup chat")).toBeVisible();
  expect(within(modelRow("Main chat")).getByText("2")).toBeVisible();
  expect(within(modelRow("Backup chat")).getByText("1")).toBeVisible();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "View recent requests" }));
  await user.click(await screen.findByRole("button", { name: "Receipt details for Chat request" }));
  const sheet = screen.getByRole("dialog");
  expect(within(sheet).getByText("Requested model").nextElementSibling).toHaveTextContent("Backup chat");
  expect(within(sheet).getByText("Served model").nextElementSibling).toHaveTextContent("Main chat");
});

test.each(["owner", "member"] as const)("Usage formats fallback names and protects technical paths for %s", async (role) => {
  renderUsage({
    receipts: [{ request_id: "fallback", capability: "chat", requested_model: GGUF, resolved_model: GGUF, fallback_model: GGUF }],
  }, undefined, { models: [{ id: GGUF, title: "Fallback chat" }] }, role);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "View recent requests" }));
  await user.click(await screen.findByRole("button", { name: "Receipt details for Chat request" }));
  const sheet = screen.getByRole("dialog");
  await waitFor(() => expect(within(sheet).getByText("Model").nextElementSibling).toHaveTextContent("Fallback chat"));
  expect(within(sheet).queryByText("Declared fallback")).not.toBeInTheDocument();
  await user.click(within(sheet).getByText("Technical details"));
  const technical = within(sheet).getByText("Technical details").closest("details")!;
  expect(technical.textContent?.includes(GGUF)).toBe(role === "owner");
  expect(technical.textContent).toContain("Qwen3.8-27B-Uncensored-Q4_K_M.gguf");
});

test.each(["owner", "member"] as const)("Activity formats fallback names and protects technical paths for %s", async (role) => {
  renderUsage({}, undefined, {
    records: [{ receipt: { request_id: "activity-fallback", intent: "chat", fallback_model: GGUF, resolved_model: GGUF }, person_label: "Mira", app_label: "Chat", created_at: 0 }],
    next_cursor: null, people: [],
    models: [{ id: GGUF, title: "Fallback chat" }],
  }, role, Requests);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Request details: Chat by Mira" }));
  const sheet = screen.getByRole("dialog");
  await waitFor(() => expect(within(sheet).getByText("Model").nextElementSibling).toHaveTextContent("Fallback chat"));
  expect(within(sheet).queryByText("Declared fallback")).not.toBeInTheDocument();
  await user.click(within(sheet).getByText("Technical details"));
  expect(within(sheet).getByLabelText("Technical request details").textContent?.includes(GGUF)).toBe(role === "owner");
});

test.each(["owner", "member"] as const)("Workflows use readable recent models and role-specific input labels for %s", async (role) => {
  renderUsage({}, undefined, {
    schema_version: 1,
    workflows: [{
      id: "picture", title: "Picture workflow", description: "Make a picture.", capability: "image",
      aliases: [], availability: { status: "ready" }, typical_seconds: null,
      inputs: [{ name: "negative_prompt", label: "Things to avoid", type: "string", accepted: true, required: false }],
      outputs: [], examples: { curl: "negative_prompt", python: "negative_prompt", javascript: "negative_prompt" },
    }],
    assets: [{ asset_id: "output", kind: "image", model: GGUF, created_at: 0 }],
    models: [{ id: GGUF, title: "Picture maker" }],
  }, role, WorkflowsPage);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Details for Picture workflow" }));
  const sheet = screen.getByRole("dialog");
  const jobs = within(sheet).getByRole("region", { name: "Recent creations" });
  expect(await within(jobs).findByText("Picture maker")).toBeVisible();
  const details = within(sheet).getAllByText(role === "owner" ? "Technical details" : "Input details")[0].closest("details")!;
  await user.click(details.querySelector("summary")!);
  expect(details.textContent).toContain(role === "owner" ? "negative_prompt" : "Things to avoid");
  expect(details.textContent?.includes("negative_prompt")).toBe(role === "owner");
  expect(jobs.textContent?.includes(GGUF)).toBe(role === "owner");
  if (role === "member") expect(jobs.textContent).not.toMatch(/\.gguf|Q4_K_M/);
});


function modelRow(label: RegExp | string) {
  return screen
    .getAllByRole("row")
    .find((row) =>
      typeof label === "string"
        ? row.textContent?.includes(label)
        : label.test(row.textContent ?? ""),
    )!;
}

test("Usage model rows read as names, never as a path on the gateway host", async () => {
  renderUsage(
    {
      by_model: [
        { model: GGUF, capability: "chat", requests: 3402, paid_requests: 0, unknown_paid_requests: 0 },
        { model: "decide-quality", capability: "decide", requests: 377, paid_requests: 0, unknown_paid_requests: 0 },
        { model: "tts-1", capability: "voice", requests: 9, paid_requests: 0, unknown_paid_requests: 0 },
      ],
    },
    undefined,
    { aliases: [{ id: "decide-quality", title: "Careful choices" }], models: [] },
  );
  await screen.findByText("Top models by requests");
  expect(document.body.textContent).not.toMatch(/\/media\/|AI Models|\/GGUF\//);
  const chat = modelRow("Qwen3.8 27B Uncensored");
  expect(chat).toBeTruthy();
  // Not even the tooltip names the weight file: it repeats the readable name.
  expect(within(chat).getByTitle("Qwen3.8 27B Uncensored")).toBeVisible();
  expect(document.body.innerHTML).not.toMatch(/\.gguf|Q4_K_M/);
  await waitFor(() => expect(modelRow("Careful choices")).toBeTruthy());
  expect(within(modelRow("Careful choices")).getByText("Choose an option")).toBeVisible();
  expect(within(modelRow("TTS 1")).getByText("Read text aloud")).toBeVisible();
});

test("Usage reads text to speech as Read text aloud, music as Make music or sounds", async () => {
  renderUsage({
    by_model: [
      { model: "kokoro", capability: "speech.synthesis", requests: 9, paid_requests: 0, unknown_paid_requests: 0 },
      { model: "tts-1", capability: "audio.speech", requests: 8, paid_requests: 0, unknown_paid_requests: 0 },
      { model: "song-maker", capability: "audio", requests: 7, paid_requests: 0, unknown_paid_requests: 0 },
    ],
  });
  await screen.findByText("Top models by requests");
  expect(within(modelRow("Kokoro")).getByText("Read text aloud")).toBeVisible();
  expect(within(modelRow("TTS 1")).getByText("Read text aloud")).toBeVisible();
  expect(within(modelRow("Song Maker")).getByText("Make music or sounds")).toBeVisible();
});

test("Usage model Spend cell is Local, an amount or Cost not recorded", async () => {
  renderUsage({
    by_model: [
      { model: "local-chat", capability: "chat", requests: 50, remote_spend: null, paid_requests: 0, unknown_paid_requests: 0 },
      { model: "zero-chat", capability: "chat", requests: 40, remote_spend: 0, paid_requests: 0, unknown_paid_requests: 0 },
      { model: "paid-image", capability: "image", requests: 30, remote_spend: 0.25, paid_requests: 3, unknown_paid_requests: 0 },
      { model: "mystery", capability: "image", requests: 20, remote_spend: null, paid_requests: 0, unknown_paid_requests: 2 },
      { model: "partial", capability: "video", requests: 10, remote_spend: 0.5, paid_requests: 2, unknown_paid_requests: 1 },
    ],
  });
  await screen.findByText("Top models by requests");
  expect(within(modelRow("Local Chat")).getByText("Cost not recorded")).toBeVisible();
  expect(within(modelRow("Zero Chat")).getByText("$0.00")).toBeVisible();
  expect(within(modelRow("Paid Image")).getByText("$0.25")).toBeVisible();
  expect(within(modelRow("Mystery")).getByText("Cost not recorded")).toBeVisible();
  const partial = modelRow("Partial");
  expect(within(partial).getByText("$0.50")).toBeVisible();
  expect(within(partial).getByText("partial")).toBeVisible();
  expect(screen.queryByText(/Local: no provider charge\./)).not.toBeInTheDocument();
  expect(screen.queryByText(/Unavailable: the provider cost was not recorded\./)).not.toBeInTheDocument();
  // No row is blank in the Spend column.
  for (const row of within(screen.getByRole("table", { name: "Models" })).getAllByRole("row").slice(1)) {
    expect(within(row).getAllByRole("cell").at(-1)).not.toBeEmptyDOMElement();
  }
});

test("Usage chart legend names capabilities as Home and Try do", async () => {
  renderUsage({
    by_day: [
      { date: "2026-10-01", chat: 1, image: 1, transcribe: 1, decide: 1, embeddings: 1 },
    ],
  });
  await screen.findByText("Requests by day and task");
  const legend = screen.getAllByText("Make a picture").map((node) => node.parentElement);
  expect(legend.length).toBeGreaterThan(0);
  for (const name of [
    "Transcribe a recording",
    "Choose an option",
    "Turn text into numbers",
  ]) {
    expect(screen.getAllByText(name).length).toBeGreaterThan(0);
  }
  expect(screen.queryByText("Decisions")).not.toBeInTheDocument();
  expect(screen.queryByText("Embeddings")).not.toBeInTheDocument();
});

test("Usage receipts show host paths only in owner technical details", async () => {
  renderUsage({
    receipts: [
      { request_id: "r1", capability: "chat", requested_model: "default", resolved_model: GGUF },
    ],
  });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "View recent requests" }));
  expect(document.body.textContent).not.toMatch(/\/media\/|AI Models/);
  await user.click(screen.getByRole("button", { name: "Receipt details for Chat request" }));
  const sheet = screen.getByRole("dialog");
  expect(sheet.textContent).toContain("Qwen3.8-27B-Uncensored-Q4_K_M.gguf");
  expect(sheet.textContent).toContain(GGUF);
});

test("Usage Source list ranks busy apps first and tells same-named apps apart", async () => {
  renderUsage({
    sources: [
      { id: "app:old1", label: "Temp triage", requests: 5, disabled: true, created_at: 100 },
      { id: "app:idle", label: "Idle app", requests: 0, disabled: false, created_at: 50 },
      { id: "app:old2", label: "Temp triage", requests: 5, disabled: true, created_at: 200 },
      { id: "app:small", label: "Small app", requests: 2, disabled: false, created_at: 10 },
      { id: "local-network", label: "Local network · no key", requests: 700, disabled: false, created_at: null },
      { id: "app:big", label: "Big app", requests: 90, disabled: false, created_at: 20 },
    ],
  });
  await screen.findByText("Top models by requests");
  const user = userEvent.setup();
  screen.getByRole("combobox", { name: "Source" }).focus();
  await user.keyboard("{Enter}");
  const labels = (await screen.findAllByRole("option")).map((option) => option.textContent);
  expect(labels).toEqual([
    "All sources",
    "Local network · no key",
    "Big app",
    "Small app",
    "Temp triage (1)",
    "Temp triage (2)",
    "Idle app",
  ]);
  expect(screen.getByText("Turned off or no requests")).toBeVisible();
});


test("Usage daily bars use recorded totals without stacking and preserve overfull counts", async () => {
  renderUsage({
    by_day: [{ date: "2026-10-01", total: 4, chat: 4, image: 3 }],
  });
  const chart = await screen.findByTestId("usage-chart");
  expect(JSON.parse(chart.getAttribute("data-values")!)).toEqual([{ label: "Oct 1", total: 4 }]);
  const bars = screen.getAllByTestId("usage-bar");
  expect(bars).toHaveLength(1);
  expect(bars[0]).toHaveAttribute("data-key", "total");
  expect(bars[0]).not.toHaveAttribute("data-stack");
  expect(bars[0]).toHaveAttribute("data-fill", "var(--chart-1)");
  expect(screen.getByText(/On 1 day the task counts add to more than the recorded total/)).toBeVisible();
  await userEvent.setup().click(screen.getByText("View daily totals"));
  const daily = screen.getByRole("table", { name: "Requests by day and task" });
  expect(within(daily).getByText("4 (task counts add to 7)")).toBeInTheDocument();
  expect(within(daily).getByText("3")).toBeInTheDocument();
});

test("Usage By task combines model counts and cost, sorts by count and shows period share", async () => {
  renderUsage({
    requests: 10,
    by_model: [
      { model: "picture", capability: "image", requests: 3, cost_usd: 0.5 },
      { model: "chat-a", capability: "chat", requests: 2, cost_usd: 0.25 },
      { model: "chat-b", capability: "chat", requests: 5, cost_usd: 0.75 },
    ],
  });
  const table = await screen.findByRole("table", { name: "By task" });
  expect(table).toHaveAttribute("data-dense");
  expect(within(table).getAllByRole("columnheader").map((cell) => cell.textContent))
    .toEqual(["Task", "Requests", "Share", "Spend"]);
  expect(within(table).getAllByRole("row").slice(1).map((row) =>
    within(row).getAllByRole("cell").map((cell) => cell.textContent),
  )).toEqual([
    ["Chat", "7", "70%", "$1.00"],
    ["Make a picture", "3", "30%", "$0.50"],
  ]);
});

test("Usage By task keeps missing and partial costs distinct from known local cost", async () => {
  renderUsage({
    requests: 5,
    by_model: [
      { model: "chat-known", capability: "chat", requests: 1, cost_usd: 0.5 },
      { model: "chat-unknown", capability: "chat", requests: 1 },
      { model: "picture-unknown", capability: "image", requests: 2 },
      { model: "local-video", capability: "video", requests: 1, paid_requests: 0, unknown_paid_requests: 0 },
    ],
  });
  const table = await screen.findByRole("table", { name: "By task" });
  const chat = within(table).getByRole("row", { name: /Chat/ });
  expect(within(chat).getByText("$0.50")).toBeVisible();
  expect(within(chat).getByText("partial")).toBeVisible();
  const picture = within(table).getByRole("row", { name: /Make a picture/ });
  expect(within(picture).getByText("Cost not recorded")).toBeVisible();
  expect(within(picture).queryByText("$0.00")).not.toBeInTheDocument();
  const video = within(table).getAllByRole("row").at(-1)!;
  expect(within(video).getByText("Cost not recorded")).toBeVisible();
});

test("Usage By task does not divide by an empty period total", async () => {
  renderUsage({
    requests: 0, media_count: 1,
    by_model: [{ model: "chat", capability: "chat", requests: 1 }],
  });
  const table = await screen.findByRole("table", { name: "By task" });
  expect(within(table).getByText("Unknown")).toBeVisible();
  expect(table).not.toHaveTextContent(/NaN|Infinity/);
});

test.each(["success", "succeeded", "ok"])("COPY-11 usage completion once (%s)", async (status) => {
  renderUsage({ receipts: [{ request_id: "completed", capability: "chat", status }] });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "View recent requests" }));
  await user.click(screen.getByRole("button", { name: "Receipt details for Chat request" }));
  const sheet = within(screen.getByRole("dialog"));
  expect(sheet.getAllByText("Completed")).toHaveLength(1);
  expect(sheet.queryByText("The gateway completed this request.")).toBeNull();
});

test.each([
  ["in_progress", "In progress", "The request was still running when this receipt was recorded."],
  ["timed_out", "Timed out", "The request timed out before we recorded a result. Try again."],
  ["incomplete", "Outcome not recorded", "We couldn't tell where this stopped. Check for a newer receipt, then try again."],
])("COPY-11 usage outcome context (%s)", async (status, label, summary) => {
  renderUsage({ receipts: [{ request_id: "context", capability: "chat", status }] });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "View recent requests" }));
  await user.click(screen.getByRole("button", { name: "Receipt details for Chat request" }));
  const sheet = within(screen.getByRole("dialog"));
  expect(sheet.getAllByText(label)).toHaveLength(1);
  expect(sheet.getByText(summary)).toBeVisible();
});

test.each([undefined, "provider", "opaque_stage"])("COPY-08 usage failure uncertainty (%s)", async (stage) => {
  renderUsage({
    receipts: [{ request_id: "failed", capability: "chat", status: "failed", metadata: { stage, error_code: "opaque_code" } }],
  });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "View recent requests" }));
  await user.click(screen.getByRole("button", { name: "Receipt details for Chat request" }));
  const sheet = within(screen.getByRole("dialog"));
  expect(sheet.getByText("We couldn't tell where this stopped. Try again.")).toBeVisible();
  expect(sheet.getAllByText("Failed")).toHaveLength(1);
  const details = sheet.getByText("Technical details").closest("details")!;
  expect(details).not.toHaveAttribute("open");
  expect(details).toHaveTextContent("opaque_code");
  expect(sheet.queryByText(/Stage:|Error code:/)).not.toBeInTheDocument();
  expect(sheet.queryByText("The request failed.")).toBeNull();
});

test("COPY-17 usage spend and task labels", async () => {
  renderUsage({
    by_model: [
      { model: "sorter", capability: "rerank", requests: 1 },
      { model: "recorder", capability: "transcribe", requests: 1 },
    ],
  });
  await screen.findByRole("table", { name: "Models" });
  for (const name of ["By task", "Models"]) {
    const table = within(screen.getByRole("table", { name }));
    expect(table.getByRole("columnheader", { name: "Spend" })).toBeVisible();
    expect(table.queryByRole("columnheader", { name: "Cost" })).toBeNull();
    expect(table.getByText("Sort search results")).toBeVisible();
    expect(table.getByText("Transcribe a recording")).toBeVisible();
    expect(table.getAllByText("Cost not recorded")).toHaveLength(2);
  }
});

test.each([undefined, null, "", "   ", false, true, -1, "NaN"])("PF-040 rejects missing or invalid required totals: %s", async (value) => {
  renderUsage({}, async () => Response.json({ requests: value, media_count: 0, receipts: [{ request_id: "one" }] }));
  expect(await screen.findByText("Usage totals weren’t included in the response. Try again.")).toBeVisible();
  expect(screen.queryByText("No requests in this period")).not.toBeInTheDocument();
});

test.each([null, "", " ", false, true, -1])("PF-040 preserves unknown model and failed counts: %s", async (value) => {
  renderUsage({ failed_requests: value, by_model: [{ model: "test", capability: "chat", requests: value }] });
  const models = await screen.findByRole("table", { name: "Models" });
  expect(within(models).getByText("Unknown")).toBeVisible();
  const stat = screen.getByText("Failed", { selector: "dt" }).closest<HTMLElement>('[data-slot="stat"]')!;
  expect(within(stat).getByText("Unknown")).toBeVisible();
});

test("PF-040 rejects a partial summary without media totals", async () => {
  renderUsage({}, async () => Response.json({ requests: 4 }));
  expect(await screen.findByText("Usage totals weren’t included in the response. Try again.")).toBeVisible();
});

test.each([false, true])("PF-045 shows requested and served models only when different: %s", async (different) => {
  renderUsage({ receipts: [{ request_id: "routing", capability: "chat", requested_model: "alpha", resolved_model: different ? "beta" : "alpha", stage: "private_stage", error_code: "private_code", fallback_model: "gamma" }] });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "View recent requests" }));
  await user.click(screen.getByRole("button", { name: "Receipt details for Chat request" }));
  const sheet = within(screen.getByRole("dialog"));
  expect(Boolean(sheet.queryByText("A different model was used."))).toBe(different);
  expect(Boolean(sheet.queryByText("Requested model"))).toBe(different);
  expect(Boolean(sheet.queryByText("Served model"))).toBe(different);
  expect(Boolean(sheet.queryByText("Model", { exact: true }))).toBe(!different);
  const technical = sheet.getByText("Technical details").closest("details")!;
  expect(technical).not.toHaveAttribute("open");
  expect(technical).toHaveTextContent("private_stage");
});


test.each([null, "", false])("PF-040 an invalid daily total stays unknown: %s", async (total) => {
  renderUsage({ by_day: [{ date: "2026-10-01", total, chat: 2 }] });
  await userEvent.setup().click(await screen.findByText("View daily totals"));
  const table = screen.getByRole("table", { name: "Requests by day and task" });
  expect(within(table).getByText("Unknown")).toBeVisible();
  expect(screen.queryByTestId("usage-chart")).not.toBeInTheDocument();
});

test("PF-040 accepts explicit numeric strings including genuine zero", async () => {
  renderUsage({ requests: "2", media_count: "0", failed_requests: "0" });
  const stat = (await screen.findByText("Failed", { selector: "dt" })).closest<HTMLElement>('[data-slot="stat"]')!;
  expect(within(stat).getByText("0 · 0%")).toBeVisible();
});
