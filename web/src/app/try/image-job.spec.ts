import { expect, test, vi } from "vitest";
import { runImageJob, SUBMIT_ATTEMPTS, type ImageJobDeps } from "./image-job";

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function fixture() {
  let stopped = false;
  const deps = {
    submit: vi.fn(async () => ({ id: "job", status: "queued" })),
    poll: vi.fn(async () => ({ status: "completed" })),
    content: vi.fn(async () => "picture"),
    wait: vi.fn(async (_ms: number) => {}),
    isNetworkFailure: vi.fn(() => false),
    now: vi.fn(() => 0),
    stopped: () => stopped,
  } satisfies ImageJobDeps<string>;
  return { deps, stop: () => { stopped = true; } };
}

test.each(["submit", "content", "poll", "wait"] as const)(
  "stops after an in-flight %s resolves without a later dependency",
  async (phase) => {
    const { deps, stop } = fixture();
    const entered = deferred<void>();
    const release = deferred<void>();
    const pending = async () => { entered.resolve(); await release.promise; };
    if (phase === "submit") deps.submit.mockImplementationOnce(async () => {
      await pending();
      return { id: "job", status: "completed" };
    });
    if (phase === "content") {
      deps.submit.mockResolvedValueOnce({ id: "job", status: "completed" });
      deps.content.mockImplementationOnce(async () => { await pending(); return "picture"; });
    }
    if (phase === "poll") deps.poll.mockImplementationOnce(async () => {
      await pending();
      // A failed poll must not override the person's Stop with a failure.
      return { status: "failed" };
    });
    if (phase === "wait") deps.wait.mockImplementationOnce(pending);
    const result = runImageJob(deps);
    await entered.promise;
    const calls = [deps.submit, deps.poll, deps.content, deps.wait, deps.now, deps.isNetworkFailure];
    const counts = calls.map((call) => call.mock.calls.length);
    stop();
    release.resolve();
    await expect(result).resolves.toEqual({ kind: "stopped" });
    expect(calls.map((call) => call.mock.calls.length)).toEqual(counts);
  },
);

test("stops after the submit retry wait resolves", async () => {
  const { deps, stop } = fixture();
  const entered = deferred<void>();
  const release = deferred<void>();
  deps.submit.mockRejectedValueOnce(new Error("offline"));
  deps.isNetworkFailure.mockReturnValue(true);
  deps.wait.mockImplementationOnce(async () => { entered.resolve(); await release.promise; });
  const result = runImageJob(deps);
  await entered.promise;
  stop();
  release.resolve();
  await expect(result).resolves.toEqual({ kind: "stopped" });
  expect(deps.submit).toHaveBeenCalledTimes(1);
  expect(deps.wait).toHaveBeenCalledTimes(1);
  expect(deps.poll).not.toHaveBeenCalled();
  expect(deps.content).not.toHaveBeenCalled();
});

test("without a stop, submits, waits, polls and returns the content", async () => {
  const { deps } = fixture();
  await expect(runImageJob(deps)).resolves.toEqual({
    kind: "done", content: "picture", jobId: "job", requestId: undefined,
  });
  expect(deps.submit).toHaveBeenCalledTimes(1);
  expect(deps.wait).toHaveBeenCalledTimes(1);
  expect(deps.poll).toHaveBeenCalledTimes(1);
  expect(deps.poll).toHaveBeenCalledWith("job");
  expect(deps.content).toHaveBeenCalledTimes(1);
  expect(deps.content).toHaveBeenCalledWith("job");
});

for (const network of [false, true]) {
  for (const attempt of [1, SUBMIT_ATTEMPTS]) {
    test(`stops after submit attempt ${attempt} rejects (network=${network})`, async () => {
      const { deps, stop } = fixture();
      const entered = deferred<void>();
      const release = deferred<void>();
      const failure = new Error("pending submit failure");
      // Earlier attempts must be retried to reach the last-attempt boundary.
      deps.isNetworkFailure.mockReturnValue(true);
      for (let i = 1; i < attempt; i += 1) {
        deps.submit.mockRejectedValueOnce(new Error("earlier network failure"));
      }
      deps.submit.mockImplementationOnce(async () => {
        entered.resolve();
        await release.promise;
        throw failure;
      });
      const result = runImageJob(deps).then(
        (value) => ({ value }),
        (error: unknown) => ({ error }),
      );
      try {
        await entered.promise;
        expect(deps.submit).toHaveBeenCalledTimes(attempt);
        deps.isNetworkFailure.mockReturnValue(network);
        const calls = [deps.submit, deps.poll, deps.content, deps.wait, deps.now, deps.isNetworkFailure];
        const counts = calls.map((call) => call.mock.calls.length);
        stop();
        release.resolve();
        expect(await result).toEqual({ value: { kind: "stopped" } });
        expect(calls.map((call) => call.mock.calls.length)).toEqual(counts);
      } finally {
        stop();
        release.resolve();
        await result;
      }
    });
  }

  for (const phase of ["content", "poll"] as const) {
    test(`stops after pending ${phase} rejects (network=${network})`, async () => {
      const { deps, stop } = fixture();
      const entered = deferred<void>();
      const release = deferred<void>();
      const failure = new Error("pending read failure");
      const pending = async (): Promise<never> => {
        entered.resolve();
        await release.promise;
        throw failure;
      };
      deps.isNetworkFailure.mockReturnValue(network);
      if (phase === "content") {
        deps.submit.mockResolvedValueOnce({ id: "job", status: "completed" });
        deps.content.mockImplementationOnce(pending);
      } else {
        deps.poll.mockImplementationOnce(pending);
      }
      const result = runImageJob(deps).then(
        (value) => ({ value }),
        (error: unknown) => ({ error }),
      );
      try {
        await entered.promise;
        expect(deps[phase]).toHaveBeenCalledTimes(1);
        const calls = [deps.submit, deps.poll, deps.content, deps.wait, deps.now, deps.isNetworkFailure];
        const counts = calls.map((call) => call.mock.calls.length);
        stop();
        release.resolve();
        expect(await result).toEqual({ value: { kind: "stopped" } });
        expect(calls.map((call) => call.mock.calls.length)).toEqual(counts);
      } finally {
        stop();
        release.resolve();
        await result;
      }
    });
  }
}
