import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { LongAudioControls } from "./long-audio-controls";

const request = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ adminFetch: request }));
afterEach(() => {
  cleanup();
  request.mockReset();
});
const options = {
  revision: '"os-4"',
  settings: {
    target_seconds: 30,
    search_seconds: 2,
    max_segment_seconds: 40,
    native_long_audio: false,
  },
  policy: {
    sample_ids: ["sample"],
    lengths: [20, 40],
    search_widths: [0, 2],
    deadline_seconds: 600,
    max_download_bytes: 1000000,
  },
  samples: [
    {
      id: "sample",
      title: "Meeting",
      speakers: 4,
      timing: "word",
      licence: "CC BY",
      download_bytes: 100,
    },
  ],
  job: null,
  commands: { start: "curl owner start", configure: "curl owner configure" },
};
function mount(data: unknown = options) {
  request.mockImplementation(
    async (_path: string, init?: { method?: string }) =>
      init?.method ? {} : data,
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <LongAudioControls serviceId="speech" />
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

describe("Long-audio operator controls", () => {
  it("saves all four settings independently with CAS and permits exact cuts", async () => {
    const user = mount();
    const input = await screen.findByLabelText(
      "Search either side of each cut (seconds)",
    );
    await user.clear(input);
    await user.type(input, "0");
    await user.click(
      screen.getByLabelText("Engine handles long audio natively"),
    );
    await user.click(
      screen.getByRole("button", { name: "Save long-audio settings" }),
    );
    const writes = request.mock.calls.filter(([, init]) => init?.method);
    expect(writes).toHaveLength(1);
    expect(writes[0][0]).toBe("/admin/api/engines/speech/stt-settings");
    expect(writes[0][1].headers).toEqual({ "If-Match": '"os-4"' });
    expect(JSON.parse(writes[0][1].body)).toEqual({
      search_seconds: 0,
      native_long_audio: true,
    });
  });

  it("starts measurement with manifest samples and exact-cut baseline, without saving", async () => {
    const user = mount();
    await user.click(
      await screen.findByRole("button", {
        name: "Measure long-audio settings",
      }),
    );
    const writes = request.mock.calls.filter(([, init]) => init?.method);
    expect(writes).toHaveLength(1);
    expect(writes[0][0]).toBe("/admin/api/engines/speech/stt-measurements");
    expect(JSON.parse(writes[0][1].body)).toEqual({
      sample_ids: ["sample"],
      lengths: [20, 40],
      search_widths: [0, 2],
    });
    expect(
      screen.queryByRole("button", { name: "Apply recommended settings" }),
    ).toBeNull();
  });

  it("resumes durable progress and cancels the stored job", async () => {
    const user = mount({
      ...options,
      job: {
        id: "job1",
        status: "running",
        phase: "Converting public sample",
        completed: 2,
        total: 10,
        report: null,
      },
    });
    expect(await screen.findByText(/Converting public sample/)).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Cancel measurement" }),
    );
    await waitFor(() =>
      expect(
        request.mock.calls.some(
          ([path, init]) =>
            path.endsWith("/job1/cancel") && init?.method === "POST",
        ),
      ).toBe(true),
    );
  });

  it("shows evidence and requires one explicit action to apply", async () => {
    const user = mount({
      ...options,
      job: {
        id: "job2",
        status: "succeeded",
        phase: "Complete",
        completed: 10,
        total: 10,
        report: {
          deletions_by_length: [
            {
              length: 20,
              pieces: 5,
              words: 50,
              deletion_pct: 1,
              failed: false,
            },
          ],
          seam_excess: [
            {
              search: 0,
              excess_pp: 1,
              low_pp: -0.1,
              high_pp: 2,
              points: 4,
              noise: { excess_pp: 0.1, low_pp: -0.2, high_pp: 0.2, points: 4 },
            },
          ],
          recommendation: {
            ...options.settings,
            reasons: ["Measured settings"],
          },
          note: "No settings applied.",
        },
      },
    });
    const apply = await screen.findByRole("button", {
      name: "Apply recommended settings",
    });
    expect(request.mock.calls.filter(([, init]) => init?.method)).toHaveLength(
      0,
    );
    expect(screen.getByText("1%")).toBeVisible();
    expect(screen.getByText(/1 \[-0.1, 2\]/)).toBeVisible();
    await user.click(apply);
    expect(
      request.mock.calls.find(([path]) => path.endsWith("/job2/apply"))?.[1]
        .headers,
    ).toEqual({ "If-Match": '"os-4"' });
  });
});

it("patches only an edited target and leaves derived defaults unpinned", async () => {
  const user = mount();
  const input = await screen.findByLabelText("Distance between cuts (seconds)");
  await user.clear(input);
  await user.type(input, "50");
  await user.click(
    screen.getByRole("button", { name: "Save long-audio settings" }),
  );
  const write = request.mock.calls.find(([, init]) => init?.method === "PATCH");
  expect(JSON.parse(write![1].body)).toEqual({ target_seconds: 50 });
});

it("does not pin a default when an edit is returned to its original effective value", async () => {
  const user = mount();
  const input = await screen.findByLabelText(
    "Search either side of each cut (seconds)",
  );
  await user.clear(input);
  await user.type(input, "5");
  await user.clear(input);
  await user.type(input, "2");
  expect(
    screen.getByRole("button", { name: "Save long-audio settings" }),
  ).toBeDisabled();
  expect(request.mock.calls.filter(([, init]) => init?.method)).toHaveLength(0);
});
