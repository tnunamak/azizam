import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ViewerContext } from "@/components/shell/session";
import {
  JobNotice,
  JobNotices,
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
afterEach(() => {
  cleanup();
  clients.forEach((client) => client.clear());
  clients.length = 0;
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

function mount(id = "alice") {
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
