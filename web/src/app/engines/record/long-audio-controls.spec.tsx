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
    native_long_audio_offline: false,
    native_long_audio_streaming: false,
  },
  policy: {
    sample_ids: ["sample"],
    lengths: [20, 40],
    search_widths: [0, 2],
    recommendation: { target_margin: 0.75 },
    deadline_seconds: 600,
    max_download_bytes: 1000000,
  },
  trial_lengths: [20, 30, 40],
  samples: [
    {
      id: "sample",
      title: "Meeting",
      speakers: 4,
      timing: "word",
      licence: "CC BY",
      audio_rights_unstated: true,
      audio_rights_note: "No separate audio licence is stated by the source.",
      rights_unstated: true,
      rights_note: "No separate audio licence is stated by the source.",
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
  it("shows unstated transcript rights beside Measure with trials collapsed", async () => {
    mount({
      ...options,
      samples: [{ ...options.samples[0], audio_rights_unstated: false,
        audio_rights_note: "", rights_note: "Transcript reuse permission is not stated." }],
    });
    const warning = await screen.findByRole("note");
    expect(warning).toHaveTextContent("Transcript reuse permission is not stated.");
    expect(warning).toBeVisible();
    expect(warning.closest("details:not([open])")).toBeNull();
  });

  it("saves exact cuts with CAS without changing native modes", async () => {
    const user = mount();
    const input = await screen.findByLabelText(
      "Search either side of each cut (seconds)",
    );
    await user.clear(input);
    await user.type(input, "0");
    await user.click(
      screen.getByRole("button", { name: "Save long-audio settings" }),
    );
    const writes = request.mock.calls.filter(([, init]) => init?.method);
    expect(writes).toHaveLength(1);
    expect(writes[0][0]).toBe("/admin/api/engines/speech/stt-settings");
    expect(writes[0][1].headers).toEqual({ "If-Match": '"os-4"' });
    expect(JSON.parse(writes[0][1].body)).toEqual({
      search_seconds: 0,
    });
  });

  it("saves native long-audio handling separately for offline and streaming modes", async () => {
    const user = mount();
    await user.click(await screen.findByLabelText("Offline mode uses native long audio"));
    await user.click(screen.getByLabelText("Streaming mode uses native long audio"));
    await user.click(screen.getByRole("button", { name: "Save long-audio settings" }));
    const writes = request.mock.calls.filter(([, init]) => init?.method);
    expect(JSON.parse(writes[0][1].body)).toEqual({
      native_long_audio_offline: true,
      native_long_audio_streaming: true,
    });
  });

  it("starts measurement with manifest samples and exact-cut baseline, without saving", async () => {
    const user = mount();
    const warning = await screen.findByText(/Sample rights not stated for/);
    expect(warning).toHaveTextContent(options.samples[0].rights_note);
    expect(warning).toBeVisible();
    expect(warning.closest("details:not([open])")).toBeNull();
    await user.click(screen.getByText("Measurement samples and trials"));
    await user.click(screen.getByRole("checkbox", { name: /Meeting/ }));
    expect(screen.queryByText(/Sample rights not stated for/)).toBeNull();
    await user.click(screen.getByRole("checkbox", { name: /Meeting/ }));
    await user.click(screen.getByText("Measurement samples and trials"));
    expect(screen.getByText(/Sample rights not stated for/)).toBeVisible();
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
      lengths: [20, 30, 40],
      search_widths: [0, 2],
    });
    expect(
      screen.queryByRole("button", { name: "Apply recommended settings" }),
    ).toBeNull();
  });

  it("offers only offline and streaming native controls", async () => {
    mount();
    expect(await screen.findByLabelText("Offline mode uses native long audio")).toBeVisible();
    expect(screen.getByLabelText("Streaming mode uses native long audio")).toBeVisible();
    expect(screen.queryByLabelText("Engine handles long audio natively")).toBeNull();
    expect(screen.queryByText(/legacy fallback/i)).toBeNull();
  });

  it("explains missing resources in plain words beside disabled Measure", async () => {
    mount({ ...options, measurement_ready: false });
    const measure = await screen.findByRole("button", { name: "Measure long-audio settings" });
    expect(measure).toHaveAccessibleDescription(/Measurement is unavailable.*graphics processor.*memory/i);
    expect(screen.getByText(/Measurement is unavailable/)).toBeVisible();
    expect(screen.queryByText(/resources\.gpu_required|resources\.vram_gb_estimate/)).toBeNull();
  });

  it("cannot start measurement when resource requirements are missing", async () => {
    const user = mount({ ...options, measurement_ready: false });
    const measure = await screen.findByRole("button", { name: "Measure long-audio settings" });
    expect(measure).toBeDisabled();
    await user.click(measure);
    expect(request.mock.calls.filter(([, init]) => init?.method)).toHaveLength(0);
  });

  it.each([false, true])("allows ready measurement with a %s known limit", async (known_limit) => {
    mount({ ...options, measurement_ready: true, known_limit });
    expect(await screen.findByRole("button", { name: "Measure long-audio settings" })).toBeEnabled();
  });

  it.each([
    [false, false],
    [true, false],
    [false, true],
  ])("warns beside Measure without a limit for native modes %s/%s", async (offline, streaming) => {
    mount({
      ...options,
      known_limit: false,
      settings: {
        ...options.settings,
        native_long_audio_offline: offline,
        native_long_audio_streaming: streaming,
      },
    });
    const measure = await screen.findByRole("button", { name: "Measure long-audio settings" });
    const warning = screen.getByText(/Long files that cannot be cut are sent whole and may fail at the engine/);
    expect(warning).toBeVisible();
    expect(warning.closest("details")).toBeNull();
    expect(measure.parentElement).toContainElement(warning);
    expect(warning).toHaveTextContent(/Measure long-audio settings to set a limit/);
  });

  it("hides no-limit advice when both modes are native", async () => {
    mount({
      ...options,
      known_limit: false,
      settings: {
        ...options.settings,
        native_long_audio_offline: true,
        native_long_audio_streaming: true,
      },
    });
    await screen.findByRole("button", { name: "Measure long-audio settings" });
    expect(screen.queryByText(/No measured hard limit|No hard maximum|sent whole/)).toBeNull();
  });

  it("hides no-limit advice when a hard limit is recorded", async () => {
    mount({ ...options, known_limit: true });
    await screen.findByRole("button", { name: "Measure long-audio settings" });
    expect(screen.queryByText(/No measured hard limit|No hard maximum|sent whole/)).toBeNull();
  });

  it("accepts a hard maximum equal to target and validates search width against half the target", async () => {
    const user = mount();
    const maximum = await screen.findByLabelText("Longest piece (seconds)");
    await user.clear(maximum);
    await user.type(maximum, "30");
    expect(screen.getByRole("button", { name: "Save long-audio settings" })).toBeEnabled();
    const search = screen.getByLabelText("Search either side of each cut (seconds)");
    await user.clear(search);
    await user.type(search, "16");
    expect(screen.getByRole("button", { name: "Save long-audio settings" })).toBeDisabled();
  });

  it("limits measurement trials to the current hard maximum", async () => {
    const user = mount();
    const lengths = await screen.findByLabelText("Piece lengths, seconds (comma-separated)");
    expect(lengths).toHaveValue("20, 30, 40");
    await user.clear(lengths);
    await user.type(lengths, "20, 45");
    expect(screen.getByRole("button", { name: "Measure long-audio settings" })).toBeDisabled();
  });

  it("explains when fewer than two configured trial lengths fit the current limit", async () => {
    const user = mount({
      ...options,
      trial_lengths: [],
      trial_lengths_note: "There are fewer than two default trial lengths within the current safe piece limit.",
    });
    await user.click(await screen.findByText("Measurement samples and trials"));
    expect(await screen.findByText(/fewer than two default trial lengths within/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Measure long-audio settings" })).toBeDisabled();
    const lengths = screen.getByLabelText("Piece lengths, seconds (comma-separated)");
    await user.clear(lengths);
    await user.type(lengths, "20, 30");
    expect(screen.getByRole("button", { name: "Measure long-audio settings" })).toBeEnabled();
  });

  it("limits trial search widths to half the target", async () => {
    const user = mount();
    const widths = await screen.findByLabelText("Search widths, seconds (comma-separated)");
    await user.clear(widths);
    await user.type(widths, "0, 16");
    expect(screen.getByRole("button", { name: "Measure long-audio settings" })).toBeDisabled();
  });

  it("caps search widths using the conservative target implied by trial lengths", async () => {
    const user = mount();
    await user.click(await screen.findByText("Measurement samples and trials"));
    expect(screen.getByText(/Search width is capped at half the conservative trial target/)).toBeVisible();
    expect(screen.getByText(/Maximum width: 7\.5 seconds/)).toBeVisible();
    const widths = screen.getByLabelText("Search widths, seconds (comma-separated)");
    await user.clear(widths);
    await user.type(widths, "0, 8");
    expect(screen.getByRole("button", { name: "Measure long-audio settings" })).toBeDisabled();
  });

  it("applies the policy search-width cap without halving it", async () => {
    const user = mount({
      ...options,
      policy: { ...options.policy, max_trial_seconds: 5 },
    });
    await user.click(await screen.findByText("Measurement samples and trials"));
    expect(screen.getByText(/Maximum width: 5 seconds/)).toBeVisible();
    const widths = screen.getByLabelText("Search widths, seconds (comma-separated)");
    await user.clear(widths);
    await user.type(widths, "0, 4");
    expect(screen.queryByText("Choose at least two distinct search widths within the planner limit.")).toBeNull();
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
  const user = mount({
    ...options,
    settings: { ...options.settings, max_segment_seconds: 60 },
  });
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

it("discards draft settings when the selected engine changes", async () => {
  request.mockResolvedValue(options);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = (serviceId: string) => (
    <QueryClientProvider client={client}>
      <LongAudioControls serviceId={serviceId} />
    </QueryClientProvider>
  );
  const { rerender } = render(view("speech"));
  const user = userEvent.setup();
  const input = await screen.findByLabelText("Distance between cuts (seconds)");
  await user.clear(input);
  await user.type(input, "25");
  expect(screen.getByRole("button", { name: "Save long-audio settings" })).toBeEnabled();

  rerender(view("another-speech"));
  await waitFor(() => expect(request).toHaveBeenCalledWith(
    "/admin/api/engines/another-speech/stt-measurements",
  ));
  expect(await screen.findByLabelText("Distance between cuts (seconds)")).toHaveValue(30);
  expect(screen.getByRole("button", { name: "Save long-audio settings" })).toBeDisabled();
});
