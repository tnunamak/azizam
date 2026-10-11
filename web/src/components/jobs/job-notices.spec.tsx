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
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify({ jobs: [job] }))),
  );
  const { client } = mount();
  await waitFor(() =>
    expect(client.getQueryData(["person-jobs", "alice"])).toBeDefined(),
  );
  const completed = {
    ...job,
    status: "succeeded",
    assets: [{ asset_id: "vid_finished" }],
  };
  act(() => client.setQueryData(["person-jobs", "alice"], [completed]));
  const link = await screen.findByRole("link", { name: "Open result" });
  expect(link).toHaveAttribute("href", "/library?asset=vid_finished");
  expect(screen.getByLabelText("Job updates")).toHaveAttribute(
    "aria-live",
    "polite",
  );
  expect(link).not.toHaveFocus();
  fireEvent.click(screen.getByRole("button", { name: "Dismiss video update" }));
  act(() => client.setQueryData(["person-jobs", "alice"], [{ ...completed }]));
  expect(screen.queryByText("Video is ready")).not.toBeInTheDocument();
});

test("old completed jobs stay quiet; a new failure gets a durable details link", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(
          JSON.stringify({ jobs: [{ ...job, status: "succeeded" }] }),
        ),
    ),
  );
  const { client } = mount();
  await waitFor(() =>
    expect(client.getQueryData(["person-jobs", "alice"])).toBeDefined(),
  );
  expect(screen.queryByRole("link")).not.toBeInTheDocument();
  act(() =>
    client.setQueryData(
      ["person-jobs", "alice"],
      [
        {
          ...job,
          job_id: "new",
          created_at: Date.now() / 1000,
          status: "failed",
        },
      ],
    ),
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
