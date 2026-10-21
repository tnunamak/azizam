import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ViewerContext } from "@/components/shell/session";
import {
  JobNotice,
  JobNotices,
  JobResult,
  jobPollInterval,
  type LongJob,
} from "./job-notices";

const job: LongJob = {
  job_id: "j1",
  intent: "video.generate",
  status: "running",
  created_at: 1,
  updated_at: 1,
  assets: [],
};
const clients: QueryClient[] = [];
// The component floors Date.now() to whole seconds when it mounts and then
// treats a job older than that as history. A test that reads the wall clock
// for a job timestamp and mounts later can cross a second boundary on a
// loaded machine. Freeze only Date (timers and promises stay real) at a
// mid-second instant so the ordering is the same on every run.
const FROZEN_NOW_MS = 1_800_000_000_500;
beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(FROZEN_NOW_MS);
});
afterEach(() => {
  vi.useRealTimers();
  cleanup();
  clients.forEach((client) => client.clear());
  clients.length = 0;
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

function mount(id = "alice", detail = false) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  clients.push(client);
  const result = render(
    <QueryClientProvider client={client}>
      <ViewerContext.Provider
        value={{
          authenticated: true,
          id,
          name: id,
          email: null,
          admin: false,
          role: "member",
        }}
      >
        <JobNotices />
        {detail ? <JobResult jobId={job.job_id} /> : null}
      </ViewerContext.Provider>
    </QueryClientProvider>,
  );
  return { ...result, client };
}

test("announces completion once, links a stable ID, and dismisses without stealing focus", async () => {
  let jobs: unknown[] = [job];
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify({ jobs }))),
  );
  const { client } = mount();
  await waitFor(() =>
    expect(client.getQueryData(["person-jobs", "alice"])).toBeDefined(),
  );
  jobs = [
    { ...job, status: "succeeded", assets: [{ asset_id: "vid_finished" }] },
  ];
  await act(() =>
    client.refetchQueries({ queryKey: ["person-jobs", "alice"] }),
  );
  const link = await screen.findByRole("link", { name: "Open result" });
  expect(link).toHaveAttribute("href", "/library?asset=vid_finished");
  expect(screen.getByLabelText("Job updates")).toHaveAttribute(
    "aria-live",
    "polite",
  );
  expect(link).not.toHaveFocus();
  fireEvent.click(screen.getByRole("button", { name: "Dismiss video update" }));
  // Another poll of the same finished job must not bring the notice back.
  await act(() =>
    client.refetchQueries({ queryKey: ["person-jobs", "alice"] }),
  );
  expect(screen.queryByText("Video is ready")).not.toBeInTheDocument();
});

test("old completed jobs stay quiet; a new failure gets a durable details link", async () => {
  let jobs: unknown[] = [{ ...job, status: "succeeded" }];
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify({ jobs }))),
  );
  const { client } = mount();
  await waitFor(() =>
    expect(client.getQueryData(["person-jobs", "alice"])).toBeDefined(),
  );
  expect(screen.queryByRole("link")).not.toBeInTheDocument();
  jobs = [
    { ...job, job_id: "new", created_at: Date.now() / 1000, status: "failed" },
  ];
  await act(() =>
    client.refetchQueries({ queryKey: ["person-jobs", "alice"] }),
  );
  expect(
    await screen.findByRole("link", { name: "See details" }),
  ).toHaveAttribute("href", "/library?job=new");
});

test.each([
  "image.generate",
  "audio.generate",
  "music.generate",
  "video.generate",
])("%s has a usable notice", (intent) => {
  render(
    <JobNotice
      job={{ ...job, intent, status: "failed" }}
      onDismiss={() => {}}
    />,
  );
  expect(screen.getByRole("link", { name: "See details" })).toBeVisible();
  expect(screen.getByRole("button")).toHaveClass("min-h-11", "min-w-11");
});

test("polling backs off for unchanged work, idle sessions, and failures", () => {
  expect([0, 1, 2, 3].map((n) => jobPollInterval(true, n, 0))).toEqual([
    5000, 10000, 20000, 30000,
  ]);
  expect(jobPollInterval(false, 0, 0)).toBe(60000);
  expect(jobPollInterval(true, 0, 3)).toBe(120000);
});

test("notices find legacy IDs after empty assets", () => {
  render(
    <JobNotice
      job={{ ...job, status: "succeeded", assets: [{}, { id: "img_legacy" }] }}
      onDismiss={() => {}}
    />,
  );
  expect(screen.getByRole("link", { name: "Open result" })).toHaveAttribute(
    "href",
    "/library?asset=img_legacy",
  );
});

test("polls deltas and retains old active jobs through unchanged polls", async () => {
  const requests: string[] = [];
  let round = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      requests.push(url);
      const jobs =
        round === 0
          ? [job]
          : round === 2
            ? [
                {
                  ...job,
                  status: "succeeded",
                  assets: [{ id: "vid_finished" }],
                },
              ]
            : [];
      return new Response(JSON.stringify({ jobs, polled_at: 100 + round }));
    }),
  );
  const { client } = mount();
  await waitFor(() =>
    expect(client.getQueryData(["person-jobs", "alice"])).toBeDefined(),
  );
  expect(requests[0]).toContain("watch=1");
  round = 1;
  await act(() =>
    client.refetchQueries({ queryKey: ["person-jobs", "alice"] }),
  );
  expect(requests[1]).toContain("since=100");
  expect(
    client
      .getQueryData<LongJob[]>(["person-jobs", "alice"])
      ?.some((item) => item.job_id === job.job_id),
  ).toBe(true);
  round = 2;
  await act(() =>
    client.refetchQueries({ queryKey: ["person-jobs", "alice"] }),
  );
  expect(
    await screen.findByRole("link", { name: "Open result" }),
  ).toHaveAttribute("href", "/library?asset=vid_finished");
});

test("job detail opens an id-only result", async () => {
  const { JobResult } = await import("./job-notices");
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            job: {
              ...job,
              status: "succeeded",
              assets: [{ id: "img_legacy" }],
            },
          }),
        ),
    ),
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  clients.push(client);
  render(
    <QueryClientProvider client={client}>
      <JobResult jobId={job.job_id} />
    </QueryClientProvider>,
  );
  expect(
    await screen.findByRole("link", { name: "Open result" }),
  ).toHaveAttribute("href", "/library?asset=img_legacy");
});

test("a failed delta page retains its watermark for retry", async () => {
  const requests: string[] = [];
  let round = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      requests.push(url);
      if (round === 1 && url.includes("cursor="))
        return new Response("failed", { status: 503 });
      return new Response(
        JSON.stringify({
          jobs: round === 0 ? [job] : [],
          polled_at: 100 + round,
          next_cursor: round === 1 ? "page2" : null,
        }),
      );
    }),
  );
  const { client } = mount();
  await waitFor(() =>
    expect(client.getQueryData(["person-jobs", "alice"])).toBeDefined(),
  );
  round = 1;
  await act(() =>
    client.refetchQueries({ queryKey: ["person-jobs", "alice"] }),
  );
  round = 2;
  await act(() =>
    client.refetchQueries({ queryKey: ["person-jobs", "alice"] }),
  );
  expect(requests.at(-1)).toContain("since=100");
});

test("following Open result dismisses across a remount in the same session", async () => {
  const now = Date.now() / 1000;
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ jobs: [{ ...job, created_at: now, updated_at: now, status: "succeeded", assets: [{ asset_id: "ready" }] }] })));
  const first = mount();
  fireEvent.click(await screen.findByRole("link", { name: "Open result" }));
  expect(screen.queryByText("Video is ready")).toBeNull();
  first.unmount();
  const second = mount();
  await waitFor(() => expect(second.client.getQueryData(["person-jobs", "alice"])).toBeDefined());
  expect(screen.queryByText("Video is ready")).toBeNull();
});

test.each([true, false])("opening a Library creation acknowledges it before or after polling: %s", async (beforePoll) => {
  const { markCreationOpened } = await import("./opened-creations");
  const now = Date.now() / 1000;
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ jobs: [{ ...job, created_at: now, updated_at: now, status: "succeeded", assets: [{ asset_id: "viewed" }] }] })));
  if (beforePoll) markCreationOpened("alice", "viewed");
  const first = mount();
  await waitFor(() => expect(first.client.getQueryData(["person-jobs", "alice"])).toBeDefined());
  if (!beforePoll) {
    expect(screen.getByText("Video is ready")).toBeVisible();
    act(() => markCreationOpened("alice", "viewed"));
  }
  expect(screen.queryByText("Video is ready")).toBeNull();
  first.unmount();
  const second = mount();
  await waitFor(() => expect(second.client.getQueryData(["person-jobs", "alice"])).toBeDefined());
  expect(screen.queryByText("Video is ready")).toBeNull();
  second.unmount();
  mount("bob");
  expect(await screen.findByText("Video is ready")).toBeVisible();
});

test("job result keeps one polite atomic status through state, error and unchanged updates", async () => {
  let current: LongJob = { ...job, status: "queued" };
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ job: current })));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  clients.push(client);
  render(<QueryClientProvider client={client}><JobResult jobId={job.job_id} /></QueryClientProvider>);
  const status = screen.getByRole("status");
  expect(status).toHaveAttribute("aria-live", "polite");
  expect(status).toHaveAttribute("aria-atomic", "true");
  await waitFor(() => expect(status).toHaveTextContent("Video is waiting"));
  for (const state of ["running", "canceled", "failed", "succeeded"]) {
    current = { ...job, status: state, error: state === "failed" ? "Provider unavailable" : undefined };
    await act(() => client.refetchQueries({ queryKey: ["person-job"] }));
    await waitFor(() => expect(screen.getByRole("status")).toBe(status));
    await waitFor(() => expect(status).toHaveTextContent(state === "running" ? "is being made" : state === "failed" ? "Provider unavailable" : state === "succeeded" ? "is ready" : "canceled"));
  }
  const changes: MutationRecord[] = [];
  const observer = new MutationObserver((records) => changes.push(...records));
  observer.observe(status, { childList: true, characterData: true, subtree: true });
  await act(() => client.refetchQueries({ queryKey: ["person-job"] }));
  expect(changes).toHaveLength(0);
  observer.disconnect();
  expect(status.querySelector("a, button")).toBeNull();
});

test("a mounted job result owns its completion announcement instead of the shell", async () => {
  let current = job;
  vi.stubGlobal("fetch", vi.fn(async (url: string) =>
    Response.json(url.includes("/jobs/") ? { job: current } : { jobs: [current] })));
  const { client } = mount("alice", true);
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Video is being made"));
  current = { ...job, status: "succeeded", assets: [{ id: "finished" }] };
  await act(() => client.refetchQueries());
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Video is ready"));
  expect(screen.getByRole("region", { name: "Job updates" })).toBeEmptyDOMElement();
});

test("dismissing a focused notice moves focus to the next notice then the persistent region", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ jobs: [
    { ...job, created_at: Date.now() / 1000, status: "failed" },
    { ...job, job_id: "j2", intent: "image.generate", created_at: Date.now() / 1000, status: "failed" },
  ] })));
  mount();
  const first = await screen.findByRole("button", { name: "Dismiss video update" });
  act(() => first.focus());
  fireEvent.click(first);
  expect(screen.getByRole("link", { name: "See details" })).toHaveFocus();
  const last = screen.getByRole("button", { name: "Dismiss picture update" });
  act(() => last.focus());
  fireEvent.click(last);
  expect(screen.getByRole("region", { name: "Job updates" })).toHaveFocus();
});

test("job fetch retry stays outside the same polite status", async () => {
  let failed = true;
  vi.stubGlobal("fetch", vi.fn(async () => failed
    ? new Response("unavailable", { status: 503 })
    : Response.json({ job })));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  clients.push(client);
  render(<QueryClientProvider client={client}><JobResult jobId={job.job_id} /></QueryClientProvider>);
  const status = screen.getByRole("status");
  await waitFor(() => expect(status).toHaveTextContent("Couldn’t load this job."));
  const retry = screen.getByRole("button", { name: "Try again" });
  expect(retry).toHaveClass("min-h-11", "min-w-11");
  expect(status).not.toContainElement(retry);
  failed = false;
  fireEvent.click(retry);
  await waitFor(() => expect(status).toHaveTextContent("Video is being made"));
  expect(screen.getByRole("status")).toBe(status);
});

function useToastClock() {
  // Replace the Date-only clock from beforeEach with a clock that also owns
  // timers; calling useFakeTimers twice keeps Vitest's already-installed clock.
  vi.useRealTimers();
  vi.useFakeTimers();
  vi.setSystemTime(FROZEN_NOW_MS);
}

function readyJob(id: string, intent = "video.generate"): LongJob {
  return {
    ...job,
    job_id: id,
    intent,
    created_at: Date.now() / 1000,
    updated_at: Date.now() / 1000,
    status: "succeeded",
    assets: [{ asset_id: `asset_${id}` }],
  };
}

test("keeps at most three fixed job toasts and promotes queued groups", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ jobs: [
    readyJob("picture", "image.generate"),
    readyJob("video"),
    readyJob("audio", "audio.generate"),
    readyJob("music", "music.generate"),
  ] })));
  mount();
  await screen.findByText("Picture is ready");
  const region = screen.getByRole("region", { name: "Job updates" });
  expect(screen.getAllByRole("region", { name: "Job updates" })).toHaveLength(1);
  expect(region).toHaveClass("fixed", "inset-x-3", "sm:right-4", "sm:bottom-4");
  expect(region).toHaveAttribute("aria-live", "polite");
  expect(region).toHaveAttribute("aria-relevant", "additions text");
  expect(region.children).toHaveLength(3);
  expect(screen.queryByText("Music is ready")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Dismiss picture update" }));
  expect(region.children).toHaveLength(3);
  expect(screen.getByText("Music is ready")).toBeVisible();
});

test("collapses identical notices, retains every item link, and ignores repeated polls", async () => {
  const jobs = [readyJob("one"), readyJob("two"), readyJob("three")];
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ jobs })));
  const { client } = mount();
  await screen.findByText("3 videos are ready");
  const region = screen.getByRole("region", { name: "Job updates" });
  expect(region.children).toHaveLength(1);
  expect(screen.getByRole("link", { name: "Open result" })).toHaveAttribute("href", "/library?asset=asset_three");
  fireEvent.click(screen.getByText("View all 3 results"));
  for (const [index, item] of jobs.entries()) {
    expect(screen.getByRole("link", { name: `Open result ${index + 1}` }))
      .toHaveAttribute("href", `/library?asset=asset_${item.job_id}`);
  }
  await act(() => client.refetchQueries({ queryKey: ["person-jobs", "alice"] }));
  expect(screen.getByText("3 videos are ready")).toBeVisible();
  expect(region.children).toHaveLength(1);
  fireEvent.click(screen.getByRole("link", { name: "Open result 1" }));
  expect(screen.getByText("2 videos are ready")).toBeVisible();
  expect(region.querySelector('a[href="/library?asset=asset_one"]')).toBeNull();
  expect(region.querySelector('a[href="/library?asset=asset_two"]')).not.toBeNull();
  expect(region.querySelector('a[href="/library?asset=asset_three"]')).not.toBeNull();
});

test("separates ready and failed groups and persists dismissal of every grouped job", async () => {
  const jobs = [readyJob("one"), readyJob("two"), { ...readyJob("failed"), status: "failed" }];
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ jobs })));
  const first = mount();
  await screen.findByText("2 videos are ready");
  expect(screen.getByText("Video couldn’t finish")).toBeVisible();
  expect(screen.getByRole("link", { name: "See details" })).toHaveAttribute("href", "/library?job=failed");
  const ready = screen.getByRole("region", { name: "2 videos are ready" });
  fireEvent.click(ready.querySelector("button")!);
  expect(screen.queryByText("2 videos are ready")).toBeNull();
  expect(screen.getByText("Video couldn’t finish")).toBeVisible();
  first.unmount();
  const second = mount();
  await waitFor(() => expect(second.client.getQueryData(["person-jobs", "alice"])).toBeDefined());
  expect(screen.queryByText("2 videos are ready")).toBeNull();
  expect(screen.queryByText("Video is ready")).toBeNull();
});

test("auto-dismisses at eight seconds without stealing focus and clears its timer on unmount", () => {
  useToastClock();
  const onDismiss = vi.fn();
  const result = render(<><button>Elsewhere</button><JobNotice job={readyJob("one")} onDismiss={onDismiss} /></>);
  act(() => screen.getByRole("button", { name: "Elsewhere" }).focus());
  act(() => vi.advanceTimersByTime(7_999));
  expect(onDismiss).not.toHaveBeenCalled();
  act(() => vi.advanceTimersByTime(1));
  expect(onDismiss).toHaveBeenCalledOnce();
  expect(screen.getByRole("button", { name: "Elsewhere" })).toHaveFocus();
  result.rerender(<JobNotice job={readyJob("two")} onDismiss={onDismiss} />);
  act(() => vi.advanceTimersByTime(1_000));
  result.unmount();
  act(() => vi.advanceTimersByTime(8_000));
  expect(onDismiss).toHaveBeenCalledOnce();
});

test("pauses the remaining lifetime while hovered or focused, including focus within the toast", () => {
  useToastClock();
  const onDismiss = vi.fn();
  render(<><button>Elsewhere</button><JobNotice job={readyJob("one")} onDismiss={onDismiss} /></>);
  const toast = screen.getByRole("region", { name: "Video ready" });
  act(() => vi.advanceTimersByTime(3_000));
  fireEvent.mouseEnter(toast);
  act(() => vi.advanceTimersByTime(20_000));
  expect(onDismiss).not.toHaveBeenCalled();
  act(() => screen.getByRole("link", { name: "Open result" }).focus());
  fireEvent.mouseLeave(toast);
  act(() => vi.advanceTimersByTime(20_000));
  expect(onDismiss).not.toHaveBeenCalled();
  act(() => screen.getByRole("button", { name: "Dismiss video update" }).focus());
  act(() => vi.advanceTimersByTime(20_000));
  expect(onDismiss).not.toHaveBeenCalled();
  act(() => screen.getByRole("button", { name: "Elsewhere" }).focus());
  act(() => vi.advanceTimersByTime(4_999));
  expect(onDismiss).not.toHaveBeenCalled();
  act(() => vi.advanceTimersByTime(1));
  expect(onDismiss).toHaveBeenCalledOnce();
});

test("refreshes the interval for a new grouped item but not an unchanged rerender", () => {
  useToastClock();
  const first = readyJob("one");
  const second = readyJob("two");
  const onDismiss = vi.fn();
  const result = render(<JobNotice job={first} onDismiss={onDismiss} />);
  act(() => vi.advanceTimersByTime(7_000));
  result.rerender(<JobNotice job={second} jobs={[first, second]} onDismiss={onDismiss} />);
  act(() => vi.advanceTimersByTime(7_000));
  expect(onDismiss).not.toHaveBeenCalled();
  result.rerender(<JobNotice job={second} jobs={[first, second]} onDismiss={() => onDismiss()} />);
  act(() => vi.advanceTimersByTime(999));
  expect(onDismiss).not.toHaveBeenCalled();
  act(() => vi.advanceTimersByTime(1));
  expect(onDismiss).toHaveBeenCalledOnce();
});

test("auto-dismissal is session scoped and queued notices receive a full lifetime", async () => {
  useToastClock();
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ jobs: [
    readyJob("picture", "image.generate"),
    readyJob("video"),
    readyJob("audio", "audio.generate"),
    readyJob("music", "music.generate"),
  ] })));
  const first = mount();
  await act(async () => {
    await first.client.refetchQueries({ queryKey: ["person-jobs", "alice"] });
    await vi.advanceTimersByTimeAsync(0);
  });
  expect(screen.getByRole("region", { name: "Job updates" }).children).toHaveLength(3);
  await act(() => vi.advanceTimersByTimeAsync(8_000));
  expect(screen.queryByText("Video is ready")).toBeNull();
  expect(screen.getByText("Music is ready")).toBeVisible();
  await act(() => vi.advanceTimersByTimeAsync(7_999));
  expect(screen.getByText("Music is ready")).toBeVisible();
  await act(() => vi.advanceTimersByTimeAsync(1));
  expect(screen.getByRole("region", { name: "Job updates" })).toBeEmptyDOMElement();
  first.unmount();
  const second = mount();
  await act(async () => {
    await second.client.refetchQueries({ queryKey: ["person-jobs", "alice"] });
    await vi.advanceTimersByTimeAsync(0);
  });
  expect(screen.getByRole("region", { name: "Job updates" })).toBeEmptyDOMElement();
});
