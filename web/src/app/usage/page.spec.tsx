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
  expect(screen.getByRole("link", { name: "Connect an app" })).toHaveAttribute("href", "/apps");
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
  // No fallback was recorded: the name used points at the model that ran.
  expect(within(sheet).getByText("Name used").nextElementSibling).toHaveTextContent("Backup chat");
  expect(within(sheet).getByText("Model", { exact: true }).nextElementSibling).toHaveTextContent("Main chat");
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
    "Big app",
    "Small app",
    "Temp triage (1)",
    "Temp triage (2)",
    "Idle app",
  ]);
  expect(screen.getByText("Turned off or no requests")).toBeVisible();
});

test("Usage Source list offers each sign-in connection by client name, apart from apps, and filters by it", async () => {
  renderUsage({
    sources: [
      { id: "app:session", kind: "app", label: "Azizam", requests: 12, disabled: false, created_at: 5 },
      { id: "connection:mcg_1", kind: "connection", label: "ChatGPT", requests: 40, disabled: false, created_at: 30 },
      { id: "connection:mcg_2", kind: "connection", label: "Old client", requests: 3, disabled: true, created_at: 10 },
      { id: "connection:mcg_3", kind: "connection", label: "Claude", requests: 0, disabled: false, created_at: 20 },
    ],
  });
  await screen.findByText("Top models by requests");
  const user = userEvent.setup();
  screen.getByRole("combobox", { name: "Source" }).focus();
  await user.keyboard("{Enter}");
  const labels = (await screen.findAllByRole("option")).map((option) => option.textContent);
  expect(labels).toEqual([
    "All sources",
    "Azizam",
    "ChatGPT",
    "Old client (disconnected)",
    "Claude",
  ]);
  expect(screen.getByText("Connected with sign-in")).toBeVisible();
  await user.click(screen.getByRole("option", { name: "ChatGPT" }));
  await waitFor(() =>
    expect(
      vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("source=connection%3Amcg_1")),
    ).toBe(true),
  );
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
  ["rejected", "Rejected", "The gateway rejected this request."],
  ["accepted", "Accepted", "The gateway accepted this request for processing."],
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

test("a rejected receipt gives the recorded 403 reason in plain words", async () => {
  renderUsage({
    receipts: [{ request_id: "refused", capability: "chat", status: "rejected", metadata: { status_code: 403 } }],
  });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "View recent requests" }));
  await user.click(screen.getByRole("button", { name: "Receipt details for Chat request" }));
  expect(within(screen.getByRole("dialog")).getByText(
    "Your account does not have permission for this action. Ask the gateway owner to check your access.",
  )).toBeVisible();
});

test("the receipt coverage note says when the view holds only the most recent receipts", async () => {
  renderUsage({ retention_days: 30, truncated: true });
  const user = userEvent.setup();
  await user.click(await screen.findByText("Receipt coverage"));
  expect(screen.getByText("Receipts are kept for 30 days.")).toBeVisible();
  expect(screen.getByText("This view is limited to the most recent stored receipts.")).toBeVisible();
});

test("a summary without retention_days never claims receipts are kept until deleted", async () => {
  renderUsage({ coverage_start: "2026-10-01T00:00:00Z" });
  const user = userEvent.setup();
  await user.click(await screen.findByText("Receipt coverage"));
  expect(screen.getByText(/Coverage starts/)).toBeVisible();
  expect(screen.queryByText(/Receipts are kept/)).toBeNull();
});

test("an empty period names the retention window in the receipts hint", async () => {
  renderUsage({ requests: 0, media_count: 0, retention_days: 90 });
  expect(await screen.findByText("No requests in this period")).toBeVisible();
  expect(screen.getByText(/90-day receipts appear here/)).toBeVisible();
});

test("choosing a source sends it as the source filter of the usage summary", async () => {
  renderUsage({ sources: [{ id: "app-a", label: "Open WebUI" }] });
  const user = userEvent.setup();
  await screen.findByText("Requests");
  screen.getByRole("combobox", { name: "Source" }).focus();
  await user.keyboard("{Enter}");
  await user.click(await screen.findByRole("option", { name: "Open WebUI" }));
  await waitFor(() =>
    expect(vi.mocked(fetch).mock.calls.some(([url]) =>
      new URL(String(url), "https://azizam.test").searchParams.get("source") === "app-a")).toBe(true),
  );
});

test("choosing a source keeps the selected person in the usage summary request", async () => {
  renderUsage({
    people: [{ id: "person-a", label: "Mira" }],
    sources: [{ id: "app-a", label: "Open WebUI" }],
  });
  const user = userEvent.setup();
  await screen.findByText("Requests");
  screen.getByRole("combobox", { name: "Person" }).focus();
  await user.keyboard("{Enter}");
  await user.click(await screen.findByRole("option", { name: "Mira" }));
  await waitFor(() =>
    expect(vi.mocked(fetch).mock.calls.some(([url]) => {
      const params = new URL(String(url), "https://azizam.test").searchParams;
      return params.get("person") === "person-a" && params.get("source") === null;
    })).toBe(true),
  );
  screen.getByRole("combobox", { name: "Source" }).focus();
  await user.keyboard("{Enter}");
  await user.click(await screen.findByRole("option", { name: "Open WebUI" }));
  await waitFor(() =>
    expect(vi.mocked(fetch).mock.calls.some(([url]) => {
      const params = new URL(String(url), "https://azizam.test").searchParams;
      return params.get("person") === "person-a" && params.get("source") === "app-a"
        && params.get("period") === "month";
    })).toBe(true),
  );
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

// ---- Moved from the usage browser spec (e2e/usage.spec.ts) ----
// The summary the browser spec intercepted.
const seededSummary = {
  requests: 4,
  media_count: 1,
  remote_spend: 0.023,
  remote_spend_available: true,
  paid_requests: 3,
  spend_measured_requests: 3,
  spend_total_requests: 4,
  retention_days: null,
  coverage_start: "2026-10-01T00:00:00Z",
  coverage_note: "Provider spend is available for matched receipts.",
  by_day: [
    { date: "2026-10-01", chat: 1, image: 1 },
    { date: "2026-10-02", chat: 1, embeddings: 1 },
  ],
  by_model: [
    { model: "demo-chat", capability: "chat", requests: 2, remote_spend: 0.01 },
    { model: "image", capability: "image", requests: 1, remote_spend: 0.013 },
  ],
  receipts: [
    {
      request_id: "req_demo_chat",
      accepted_at_ms: 1790870400000,
      requested_model: "demo-chat",
      resolved_model: "demo-chat",
      intent: "chat",
      capability: "chat",
      status: "success",
      latency_ms: 104,
      remote_spend: null,
    },
  ],
};

test("Usage lists a receipt by display name and keeps the raw model id in Technical details", async () => {
  renderUsage(seededSummary);
  const user = userEvent.setup();
  expect(await screen.findByRole("heading", { name: "Usage" })).toBeVisible();
  await screen.findByText("Top models by requests");
  await user.click(screen.getByText("Receipt coverage", { exact: true }));
  expect(screen.getByText("Receipts are kept until you delete them.")).toBeVisible();
  expect(screen.getByText("Coverage starts Oct 1, 2026.")).toBeVisible();
  expect(screen.getByText("Provider spend is available for matched receipts.")).toBeVisible();
  expect(screen.getAllByText("Demo Chat").length).toBeGreaterThan(0);
  expect(screen.queryByRole("columnheader", { name: "Typical time" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "View recent requests" }));
  const receipt = screen.getByRole("button", { name: /Receipt details for Chat request/ });
  expect(receipt).toHaveTextContent("104 ms");
  expect(receipt.querySelector("p.font-medium")).toHaveTextContent(/^Chat request$/);
  expect(within(receipt).getByText("Demo Chat")).toHaveAttribute("title", "Demo Chat");
  await user.click(receipt);
  const sheet = within(screen.getByRole("dialog", { name: "Request receipt" }));
  expect(sheet.getByText("Demo Chat")).toBeVisible();
  expect(sheet.getByText("104 ms")).toBeVisible();
  await user.click(sheet.getByText("Technical details", { exact: true }));
  const technical = sheet.getByText("Technical details").closest("details")!;
  expect(technical).toHaveTextContent("request_id");
  expect(technical).toHaveTextContent("requested_model");
  expect(technical).toHaveTextContent('"demo-chat"');
});

test("Usage omits the model line when only the capability is known", async () => {
  renderUsage({
    ...seededSummary,
    by_model: [{ model: null, capability: "transcribe", requests: 6, remote_spend: null }],
  });
  const row = within(await screen.findByRole("table", { name: "Models" })).getByRole("row", {
    name: /Transcribe a recording/,
  });
  expect(row.querySelector("code")).toBeNull();
  expect(row).not.toHaveTextContent("Unknown model");
});

test("Usage failed load shows the error callout, no placeholder summary, and recovers on retry", async () => {
  let failed = true;
  renderUsage({}, async () => {
    if (failed) {
      failed = false;
      return Response.json({ error: { message: "Unavailable for this test" } }, { status: 503 });
    }
    return Response.json(seededSummary);
  });
  expect(await screen.findByText("Usage could not be loaded", { exact: true })).toBeVisible();
  expect(screen.queryByText("Unknown · Usage unavailable", { exact: true })).toBeNull();
  await userEvent.setup().click(screen.getByRole("button", { name: "Retry usage" }));
  expect((await screen.findAllByText("Demo Chat")).length).toBeGreaterThan(0);
});

test("Usage seeded view shows Spend and the Receipts panel once receipts are opened", async () => {
  renderUsage(seededSummary);
  expect(await screen.findByRole("heading", { name: "Usage" })).toBeVisible();
  await screen.findByText("Top models by requests");
  expect(screen.queryByLabelText("Loading your account")).toBeNull();
  expect(screen.getAllByText("Spend", { exact: true }).length).toBeGreaterThan(0);
  await userEvent.setup().click(screen.getByRole("button", { name: "View recent requests" }));
  expect(screen.getByText("Receipts", { exact: true })).toBeVisible();
});

test("Usage labels unavailable remote spend as Cost not recorded, never $0", async () => {
  renderUsage({
    ...seededSummary,
    remote_spend: 0,
    remote_spend_available: false,
    paid_requests: 0,
    spend_measured_requests: 0,
    spend_total_requests: 4,
  });
  const spend = within(await screen.findByLabelText("This month"))
    .getByText("Spend", { exact: true })
    .closest<HTMLElement>('[data-slot="stat"]')!;
  expect(spend).toHaveTextContent("Cost not recorded");
  expect(spend).not.toHaveTextContent("$0");
});

test("Usage person filter does not promote an email-shaped name or label to a person name", async () => {
  renderUsage({
    ...seededSummary,
    people: [
      { id: "person-email", name: "person@example.test" },
      { id: "person-second", label: "second@example.test" },
    ],
  });
  await screen.findByText("Top models by requests");
  const user = userEvent.setup();
  screen.getByRole("combobox", { name: "Person" }).focus();
  await user.keyboard("{Enter}");
  const people = await screen.findAllByRole("option", { name: /^Person name not recorded/ });
  expect(people).toHaveLength(2);
  const labels = people.map((option) => option.textContent ?? "");
  expect(new Set(labels).size).toBe(2);
  expect(labels.join(" ")).not.toContain("@example.test");
  expect(screen.queryByRole("option", { name: "person@example.test" })).toBeNull();
  await user.click(people[1]);
  expect(
    vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("person=person-second")),
  ).toBe(true);
});

const phoneSummary = {
  ...seededSummary,
  requests: 8264,
  by_model: [
    { model: GGUF, capability: "chat", requests: 3402, remote_spend: null, paid_requests: 0, unknown_paid_requests: 0 },
    { model: null, capability: "transcribe", requests: 2322, remote_spend: null, paid_requests: 0, unknown_paid_requests: 0 },
    { model: "Qwen3 (Q4)", capability: "chat", requests: 505, remote_spend: null, paid_requests: 0, unknown_paid_requests: 0 },
    { model: "default", capability: "chat", requests: 408, remote_spend: 0, paid_requests: 0, unknown_paid_requests: 0 },
    { model: "decide-quality", capability: "decide", requests: 377, remote_spend: null, paid_requests: 0, unknown_paid_requests: 0 },
    { model: "paid-remote-model", capability: "image", requests: 300, remote_spend: null, paid_requests: 0, unknown_paid_requests: 4 },
  ],
  by_day: [
    { date: "2026-10-01", chat: 5, image: 1, audio: 2, transcribe: 3, decide: 4, embeddings: 5 },
    { date: "2026-10-02", chat: 1 },
  ],
  receipts: [{ ...seededSummary.receipts[0], requested_model: "default", resolved_model: GGUF }],
};

test("Usage model table shows names, never a path or packing tag, and a Cost in every row", async () => {
  renderUsage(phoneSummary, undefined, { aliases: [{ id: "decide-quality", title: "Careful choices" }], models: [] });
  const table = await screen.findByRole("table", { name: "Models" });
  await waitFor(() => expect(table).toHaveTextContent("Careful choices"));
  expect(table.textContent).not.toMatch(/\/media\/|AI Models|\/GGUF\/|\.gguf/);
  expect(table.textContent).not.toMatch(/Q4 K M|Q4_K_M/);
  expect(table).toHaveTextContent("Qwen3.8 27B Uncensored");
  expect(table).toHaveTextContent("Transcribe a recording");
  expect(within(table).getByTitle("Qwen3.8 27B Uncensored")).toBeVisible();
  expect(table.querySelector('[title*="gguf" i], [title*="Q4_K_M"]')).toBeNull();
  // One header row and the five busiest models (the sixth is cut); none is blank in the Spend column.
  const rows = within(table).getAllByRole("row");
  expect(rows).toHaveLength(6);
  for (const row of rows.slice(1)) {
    expect(within(row).getAllByRole("cell").at(-1)!.textContent!.trim()).toMatch(/^(Cost not recorded|\$[\d.]+)/);
  }
  expect(table).toHaveTextContent("$0.00");
  expect(screen.queryByText("Local: no provider charge.")).toBeNull();
  // The legend uses the names Home and Try use, inside a visible disclosure.
  await userEvent.setup().click(screen.getByText("View daily totals", { exact: true }));
  expect(screen.getAllByText("Make a picture").length).toBeGreaterThan(0);
  expect(screen.getAllByText("Choose an option").length).toBeGreaterThan(0);
  expect(screen.queryByText("Decisions", { exact: true })).toBeNull();
  expect(screen.queryByText("Embeddings", { exact: true })).toBeNull();
});

const manySources = [
  { id: "app:1", label: "tim.nunamaker@gmail.com personal access with a rather long name", kind: "app", requests: 90, disabled: false, created_at: 1 },
  ...Array.from({ length: 20 }, (_, index) => ({
    id: `app:tmp${index}`,
    label: index < 3 ? "Temporary empathy Jev triage" : `copy-triage-17911237${index}`,
    kind: "app",
    requests: index < 3 ? 6 : 1,
    disabled: true,
    created_at: 1000 + index,
  })),
  ...Array.from({ length: 6 }, (_, index) => ({
    id: `app:live${index}`,
    label: `Household app ${index}`,
    kind: "app",
    requests: 20 - index,
    disabled: false,
    created_at: 100 + index,
  })),
];

test("Usage Source list ranks busy apps first, numbers same-named apps and puts turned-off apps last", async () => {
  renderUsage({ ...phoneSummary, sources: manySources });
  await screen.findByText("Top models by requests");
  screen.getByRole("combobox", { name: "Source" }).focus();
  await userEvent.setup().keyboard("{Enter}");
  const labels = (await screen.findAllByRole("option")).map((option) => option.textContent);
  expect(labels.slice(0, 4)).toEqual([
    "All sources",
    "tim.nunamaker@gmail.com personal access with a rather long name",
    "Household app 0",
    "Household app 1",
  ]);
  expect(labels.filter((label) => label?.startsWith("Temporary empathy"))).toEqual([
    "Temporary empathy Jev triage (1)",
    "Temporary empathy Jev triage (2)",
    "Temporary empathy Jev triage (3)",
  ]);
  expect(labels.indexOf("Household app 5")).toBeLessThan(
    labels.indexOf("Temporary empathy Jev triage (1)"),
  );
  expect(screen.getByText("Turned off or no requests")).toBeVisible();
});

test("Usage tells a partial total from a missing breakdown and from a genuine zero", async () => {
  let partial = true;
  renderUsage({}, async () =>
    Response.json(
      partial
        ? { requests: null, media_count: 0 }
        : { requests: 2, media_count: 0, by_day: [], by_model: [], receipts: [] },
    ),
  );
  const user = userEvent.setup();
  expect(await screen.findByText("Usage totals weren’t included in the response. Try again.")).toBeVisible();
  expect(screen.queryByText("No requests in this period")).toBeNull();
  partial = false;
  await user.click(screen.getByRole("button", { name: "Retry usage" }));
  expect(await screen.findByText("Daily totals weren’t recorded for these requests.")).toBeVisible();
  expect(screen.getByText("Model totals weren’t recorded.")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "View recent requests" }));
  expect(screen.getByText("No receipts are available for this view.")).toBeVisible();
  expect(screen.queryByText("$0.00")).toBeNull();
});

test("Usage reconciles an unexplained daily remainder into Other without changing the total", async () => {
  renderUsage({ requests: 9, by_day: [{ date: "2026-10-01", total: 9, chat: 2, image: 3 }] });
  const chart = await screen.findByTestId("usage-chart");
  expect(JSON.parse(chart.getAttribute("data-values")!)).toEqual([{ label: "Oct 1", total: 9 }]);
  await userEvent.setup().click(screen.getByText("View daily totals"));
  const table = screen.getByRole("table", { name: "Requests by day and task" });
  expect(within(table).getAllByRole("columnheader").map((cell) => cell.textContent))
    .toEqual(["Date", "Chat", "Make a picture", "Other", "Total"]);
  expect(within(table).getAllByRole("row").slice(1).map((row) =>
    within(row).getAllByRole("cell").map((cell) => cell.textContent),
  )).toEqual([["2", "3", "4", "9"]]);
});

test("Usage receipt duration changes units at one second and precision at ten seconds", async () => {
  renderUsage({ receipts: [999, 1000, 1250, 10000].map((duration_ms) => ({
    request_id: `duration-${duration_ms}`, capability: "chat", duration_ms,
  })) });
  await userEvent.setup().click(await screen.findByRole("button", { name: "View recent requests" }));
  const receipts = screen.getAllByRole("button", { name: "Receipt details for Chat request" });
  for (const [index, duration] of ["999 ms", "1.0 s", "1.3 s", "10 s"].entries()) {
    expect(within(receipts[index]).getByText(duration, { exact: true })).toBeVisible();
  }
});

test("Usage By task keeps a mixed known and unknown request aggregate and share unknown", async () => {
  renderUsage({
    requests: 10,
    by_model: [
      { model: "known-chat", capability: "chat", requests: 4, cost_usd: 0.25 },
      { model: "unknown-chat", capability: "chat", requests: null, cost_usd: 0.5 },
    ],
  });
  const table = await screen.findByRole("table", { name: "By task" });
  const row = within(table).getByRole("row", { name: /Chat/ });
  expect(within(row).getAllByRole("cell").map((cell) => cell.textContent))
    .toEqual(["Chat", "Unknown", "Unknown", "$0.75"]);
});

test("Usage filtered empty state clears both filters and returns to the period empty state", async () => {
  renderUsage({
    requests: 0, media_count: 0,
    people: [{ id: "person-a", label: "Mira" }],
    sources: [{ id: "app-a", label: "Open WebUI" }],
  });
  const user = userEvent.setup();
  await screen.findByText("No requests in this period");
  screen.getByRole("combobox", { name: "Person" }).focus();
  await user.keyboard("{Enter}");
  await user.click(await screen.findByRole("option", { name: "Mira" }));
  await screen.findByText("No matching requests");
  screen.getByRole("combobox", { name: "Source" }).focus();
  await user.keyboard("{Enter}");
  await user.click(await screen.findByRole("option", { name: "Open WebUI" }));
  await screen.findByText("No matching requests");
  expect(screen.queryByRole("link", { name: "Connect an app" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Clear filters" }));
  expect(await screen.findByText("No requests in this period")).toBeVisible();
  expect(screen.getByRole("combobox", { name: "Person" })).toHaveTextContent("Everyone");
  expect(screen.getByRole("combobox", { name: "Source" })).toHaveTextContent("All sources");
  expect(screen.getByRole("link", { name: "Connect an app" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "Clear filters" })).not.toBeInTheDocument();
});

test("Usage changing person resets a previously selected source in picker and request", async () => {
  renderUsage({
    people: [{ id: "person-a", label: "Mira" }],
    sources: [{ id: "app-a", label: "Open WebUI" }],
  });
  const user = userEvent.setup();
  await screen.findByText("Requests");
  screen.getByRole("combobox", { name: "Source" }).focus();
  await user.keyboard("{Enter}");
  await user.click(await screen.findByRole("option", { name: "Open WebUI" }));
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Source" })).toHaveTextContent("Open WebUI"));
  screen.getByRole("combobox", { name: "Person" }).focus();
  await user.keyboard("{Enter}");
  vi.mocked(fetch).mockClear();
  await user.click(await screen.findByRole("option", { name: "Mira" }));
  await waitFor(() => {
    const urls = vi.mocked(fetch).mock.calls.map(([url]) => String(url)).filter((url) => url.includes("/usage/summary"));
    expect(urls).toHaveLength(1);
    const params = new URL(urls[0], "https://azizam.test").searchParams;
    expect(params.get("person")).toBe("person-a");
    expect(params.get("source")).toBeNull();
  });
  expect(screen.getByRole("combobox", { name: "Source" })).toHaveTextContent("All sources");
});
