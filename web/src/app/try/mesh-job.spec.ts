import { describe, expect, test, vi } from "vitest";
import { followMeshJob, type MeshJobDeps, type MeshJobStatus } from "./mesh-job";

const BLOB = new Blob(["glb"]);

/** Deps whose polls answer from a script: a status, or an Error thrown as a failed request. */
function deps(script: Array<MeshJobStatus | Error>, overrides: Partial<MeshJobDeps> = {}) {
  let poll = 0;
  let clock = 0;
  const reconnecting: boolean[] = [];
  const waits: number[] = [];
  const value: MeshJobDeps = {
    poll: vi.fn(async () => {
      const next = script[Math.min(poll, script.length - 1)];
      poll += 1;
      if (next instanceof Error) throw next;
      return next;
    }),
    content: vi.fn(async () => BLOB),
    wait: vi.fn(async (ms: number) => {
      waits.push(ms);
      clock += ms;
    }),
    isNetworkFailure: (error) => error instanceof TypeError,
    stopped: () => false,
    onReconnecting: (state) => reconnecting.push(state),
    // A clock that moves with the waits, so a bound of minutes is crossed without waiting.
    now: () => clock,
    unreachableAfterMs: 120_000,
    ...overrides,
  };
  return { value, reconnecting, waits };
}

describe("followMeshJob", () => {
  test("polls until the job succeeds, then returns the model; the delay backs off from 2 s to 5 s", async () => {
    const { value, waits } = deps([
      { status: "queued" },
      { status: "running" },
      { status: "running" },
      { status: "running" },
      { status: "running" },
      { status: "running" },
      { status: "succeeded" },
    ]);
    expect(await followMeshJob("mj_1", value)).toEqual({ kind: "done", blob: BLOB });
    expect(waits).toEqual([2000, 2500, 3000, 3500, 4000, 4500]);
    expect(value.content).toHaveBeenCalledWith("mj_1");
  });

  test("a dropped connection is a wait, shown as Reconnecting and cleared at the next answer", async () => {
    const { value, reconnecting } = deps([
      { status: "running" },
      new TypeError("Failed to fetch"),
      new TypeError("Failed to fetch"),
      { status: "succeeded" },
    ]);
    expect((await followMeshJob("mj_1", value)).kind).toBe("done");
    expect(reconnecting).toEqual([false, true, true, false, false]);
  });

  test("requests that keep failing for two minutes end the follow as unreachable, and it says so once", async () => {
    const { value, reconnecting, waits } = deps([{ status: "running" }, new TypeError("Failed to fetch")]);
    expect(await followMeshJob("mj_1", value)).toEqual({ kind: "unreachable" });
    expect(waits.reduce((sum, ms) => sum + ms, 0)).toBeGreaterThanOrEqual(120_000);
    expect(reconnecting.at(-1)).toBe(false);
    expect(value.content).not.toHaveBeenCalled();
  });

  test("an answer in between restarts the count: failures that never last two minutes in a row are not unreachable", async () => {
    const script: Array<MeshJobStatus | Error> = [];
    for (let round = 0; round < 6; round += 1) {
      script.push(new TypeError("Failed to fetch"), new TypeError("Failed to fetch"), { status: "running" });
    }
    script.push({ status: "succeeded" });
    const { value } = deps(script, { unreachableAfterMs: 30_000 });
    expect((await followMeshJob("mj_1", value)).kind).toBe("done");
  });

  test("a dropped connection while fetching the model asks again", async () => {
    let tries = 0;
    const { value } = deps([{ status: "succeeded" }], {
      content: vi.fn(async () => {
        tries += 1;
        if (tries === 1) throw new TypeError("Failed to fetch");
        return BLOB;
      }),
    });
    expect((await followMeshJob("mj_1", value)).kind).toBe("done");
    expect(tries).toBe(2);
  });

  test("a failed job returns its message, or a plain one when it has none", async () => {
    expect(
      await followMeshJob("mj_1", deps([{ status: "failed", error: " Out of memory. " }]).value),
    ).toEqual({ kind: "failed", message: "Out of memory." });
    expect(await followMeshJob("mj_1", deps([{ status: "failed" }]).value)).toEqual({
      kind: "failed",
      message: "The 3D model could not be made. Try again.",
    });
  });

  test("a capacity refusal that can be retried says so, with the gateway's delay when it has one", async () => {
    const busy = { status: "failed", error: "The graphics card is busy with other work. Try again soon.", category: "capacity", retryable: true };
    expect(await followMeshJob("mj_1", deps([{ ...busy, retryAfterS: 30 }]).value)).toEqual({
      kind: "failed",
      message: "The graphics card is busy with other work. Try again soon.",
      tryAgain: { afterS: 30 },
    });
    expect(await followMeshJob("mj_1", deps([busy]).value)).toMatchObject({ tryAgain: { afterS: null } });
  });

  test("another failure, or a capacity failure that cannot be retried, offers no retry", async () => {
    const upstream = await followMeshJob("mj_1", deps([{ status: "failed", error: "x", category: "upstream", retryable: true }]).value);
    const final = await followMeshJob("mj_1", deps([{ status: "failed", error: "x", category: "capacity", retryable: false }]).value);
    expect(upstream).not.toHaveProperty("tryAgain");
    expect(final).not.toHaveProperty("tryAgain");
  });

  test("a canceled job ends as canceled", async () => {
    expect(await followMeshJob("mj_1", deps([{ status: "canceled" }]).value)).toEqual({
      kind: "canceled",
    });
  });

  test("a job the gateway no longer knows is a plain failure", async () => {
    const outcome = await followMeshJob("mj_1", deps([{ status: "missing" }]).value);
    expect(outcome).toEqual({
      kind: "failed",
      message: "That 3D model is no longer available. Make it again.",
    });
  });

  test("an error that is not the network ends the follow", async () => {
    const { value } = deps([new RangeError("boom")]);
    await expect(followMeshJob("mj_1", value)).rejects.toThrow("boom");
  });

  test("stopping ends the loop before the next poll and never fetches the model", async () => {
    let stop = false;
    const { value } = deps([{ status: "running" }], {
      stopped: () => stop,
      wait: vi.fn(async () => {
        stop = true;
      }),
    });
    expect(await followMeshJob("mj_1", value)).toEqual({ kind: "stopped" });
    expect(value.poll).toHaveBeenCalledTimes(1);
    expect(value.content).not.toHaveBeenCalled();
  });

  test("stopping while the model downloads discards it", async () => {
    let stop = false;
    const { value } = deps([{ status: "succeeded" }], {
      stopped: () => stop,
      content: vi.fn(async () => {
        stop = true;
        return BLOB;
      }),
    });
    expect(await followMeshJob("mj_1", value)).toEqual({ kind: "stopped" });
  });
});
