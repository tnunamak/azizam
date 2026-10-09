import { describe, expect, it, vi } from "vitest";
import {
  GIVE_UP_AFTER_MS,
  LOST_AFTER_MS,
  runImageJob,
  type ImageJobDeps,
} from "@/app/try/image-job";
import { isNetworkFailure, networkFetch } from "@/lib/network-error";

const dropped = () => new TypeError("Failed to fetch");

function harness(overrides: Partial<ImageJobDeps<string>> = {}) {
  let clock = 0;
  const waits: number[] = [];
  const deps: ImageJobDeps<string> = {
    submit: vi.fn(async () => ({ id: "job_1", status: "queued" })),
    poll: vi.fn(async () => ({ status: "succeeded" })),
    content: vi.fn(async () => "picture"),
    wait: vi.fn(async (ms: number) => {
      waits.push(ms);
      clock += ms;
    }),
    isNetworkFailure,
    now: () => clock,
    stopped: () => false,
    ...overrides,
  };
  return { deps, waits, advance: (ms: number) => (clock += ms) };
}

describe("a picture run as a media job", () => {
  it("submits once, polls until it succeeds, then fetches the picture", async () => {
    const states = ["running", "running", "succeeded"];
    const { deps } = harness({
      poll: vi.fn(async () => ({ status: states.shift()! })),
    });
    const outcome = await runImageJob(deps);
    expect(outcome).toMatchObject({ kind: "done", content: "picture" });
    expect(deps.submit).toHaveBeenCalledTimes(1);
    expect(deps.poll).toHaveBeenCalledTimes(3);
    expect(deps.content).toHaveBeenCalledWith("job_1");
  });

  it("keeps polling through a dropped connection and still finishes", async () => {
    // The phone switched apps: two polls get no answer, the third does.
    const answers: Array<() => Promise<{ status: string }>> = [
      async () => {
        throw dropped();
      },
      async () => {
        throw dropped();
      },
      async () => ({ status: "succeeded" }),
    ];
    const { deps } = harness({
      poll: vi.fn(async () => answers.shift()!()),
    });
    const outcome = await runImageJob(deps);
    expect(outcome.kind).toBe("done");
    expect(deps.submit).toHaveBeenCalledTimes(1);
  });

  it("re-sends a lost submit, which the gateway answers with the same job", async () => {
    let calls = 0;
    const { deps } = harness({
      submit: vi.fn(async () => {
        calls += 1;
        if (calls === 1) throw dropped();
        return { id: "job_1", status: "queued" };
      }),
    });
    const outcome = await runImageJob(deps);
    expect(outcome.kind).toBe("done");
    expect(deps.submit).toHaveBeenCalledTimes(2);
    expect(deps.poll).toHaveBeenCalledTimes(1);
  });

  it("says lost, never failed, when the connection never comes back", async () => {
    const { deps } = harness({
      submit: vi.fn(async () => {
        throw dropped();
      }),
    });
    expect(await runImageJob(deps)).toEqual({ kind: "lost" });
  });

  it("says lost after a long silence, and names the job", async () => {
    const { deps, advance } = harness({
      poll: vi.fn(async () => {
        advance(LOST_AFTER_MS / 4);
        throw dropped();
      }),
    });
    expect(await runImageJob(deps)).toEqual({ kind: "lost", jobId: "job_1" });
  });

  it("reports a picture that outlasts ten minutes as still finishing", async () => {
    const { deps } = harness({
      poll: vi.fn(async () => ({ status: "running" })),
      wait: vi.fn(async function wait() {
        return undefined;
      }),
      now: (() => {
        let t = 0;
        return () => (t += GIVE_UP_AFTER_MS / 5);
      })(),
    });
    expect(await runImageJob(deps)).toEqual({ kind: "lost", jobId: "job_1" });
  });

  it("reports a job the gateway failed, with its own words", async () => {
    const { deps } = harness({
      poll: vi.fn(async () => ({
        status: "failed",
        error: "The engine ran out of memory.",
      })),
    });
    expect(await runImageJob(deps)).toMatchObject({
      kind: "failed",
      message: "The engine ran out of memory.",
    });
  });

  it("does not hide an HTTP error from the gateway", async () => {
    const { deps } = harness({
      submit: vi.fn(async () => {
        throw new Error("This key cannot use image.");
      }),
    });
    await expect(runImageJob(deps)).rejects.toThrow("This key cannot use image.");
    expect(deps.submit).toHaveBeenCalledTimes(1);
  });

  it("stops quietly when the page goes away", async () => {
    let stopped = false;
    const { deps } = harness({
      stopped: () => stopped,
      poll: vi.fn(async () => {
        stopped = true;
        return { status: "running" };
      }),
    });
    expect(await runImageJob(deps)).toEqual({ kind: "stopped" });
  });
});

describe("a dropped connection never shows the browser's words", () => {
  it("recognises each browser's wording", () => {
    for (const text of [
      "Failed to fetch",
      "NetworkError when attempting to fetch resource.",
      "Load failed",
      "Network request failed",
    ])
      expect(isNetworkFailure(new TypeError(text)), text).toBe(true);
    expect(isNetworkFailure(new Error("Failed to fetch"))).toBe(false);
    expect(isNetworkFailure(new TypeError("x is not a function"))).toBe(false);
  });

  it("networkFetch turns the browser's TypeError into plain words", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(dropped()));
    try {
      const failure = await networkFetch("/x").catch((error: unknown) => error);
      expect(failure).toBeInstanceOf(Error);
      expect((failure as Error).message).not.toMatch(/fetch/i);
      expect((failure as Error).message).toMatch(/Can’t reach Azizam/);
      expect(isNetworkFailure(failure)).toBe(true);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
