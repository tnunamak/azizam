import { meshPollDelayMs } from "./mesh-mode";

/** What a poll of the job route says. "missing" is a job the gateway no longer knows. */
export type MeshJobStatus = {
  status: string;
  error?: string;
  /** Why a failed job failed, in the job's own word ("capacity" is a refusal for lack of room). */
  category?: string;
  retryable?: boolean;
  /** Seconds the gateway suggests waiting, from the job's `retry_after` event. */
  retryAfterS?: number;
  /** True while the job waits for the graphics card; false once it works. Unset when the events could not be read: the caller keeps what it knew. */
  waitingForGpu?: boolean;
};

/** The reason the gateway writes on a job's "retry" event when the graphics card is in use. */
export const MESH_GPU_BUSY_REASON = "gpu_busy";

/** True when the newest of the job's wait, retry, attempt and started events says it waits for the graphics card: a
 * `waiting_for_gpu` event (the job waits its turn; `gpu_ready` ends it) or a retry for the busy card. */
export function waitingForGpu(events: { event_type?: unknown; payload?: { reason?: unknown } }[]): boolean {
  let waiting = false;
  for (const event of events) {
    if (event.event_type === "waiting_for_gpu") waiting = true;
    else if (event.event_type === "retry") waiting = event.payload?.reason === MESH_GPU_BUSY_REASON;
    else if (event.event_type === "attempt" || event.event_type === "started" || event.event_type === "gpu_ready")
      waiting = false;
  }
  return waiting;
}

export type MeshJobDeps = {
  /** One poll. A dropped connection or a server fault throws what `isNetworkFailure` accepts; any other throw ends the follow. */
  poll: (id: string) => Promise<MeshJobStatus>;
  /** The finished model. Same rule for a dropped connection. */
  content: (id: string) => Promise<Blob>;
  /** Resolves after `ms`, or early when the page is shown again. */
  wait: (ms: number) => Promise<void>;
  isNetworkFailure: (error: unknown) => boolean;
  stopped: () => boolean;
  /** True while the last request got no answer; false again at the next answer. */
  onReconnecting: (reconnecting: boolean) => void;
  /** Told at each poll that could tell whether the job waits for the graphics card. */
  onGpuWait?: (waiting: boolean) => void;
  /** The clock, in milliseconds. */
  now: () => number;
  /** How long requests may fail one after another before the follow gives up asking and says so. */
  unreachableAfterMs: number;
};

export type MeshJobOutcome =
  | { kind: "done"; blob: Blob }
  /** `tryAgain` is set when the gateway refused for capacity and says asking again can work. */
  | { kind: "failed"; message: string; tryAgain?: { afterS: number | null } }
  | { kind: "canceled" }
  | { kind: "stopped" }
  /** Every request failed for `unreachableAfterMs`. The job is not touched; asking again may find it. */
  | { kind: "unreachable" };

const FAILED_FALLBACK = "The 3D model could not be made. Try again.";
const MISSING = "That 3D model is no longer available. Make it again.";

/**
 * Follow one mesh job until it ends. A dropped connection is not a failure: the job runs in the gateway whether or
 * not this page is looking, so the loop shows "Reconnecting…" and asks again. It ends only when the job ends, the
 * caller stops it, or a request fails for a reason that is not the network. A network that stays down for
 * `unreachableAfterMs` ends the follow as "unreachable", so a gateway that never answers is not shown as
 * "Reconnecting…" for ever.
 */
export async function followMeshJob(id: string, deps: MeshJobDeps): Promise<MeshJobOutcome> {
  let attempt = 0;
  let failingSince: number | null = null;
  for (;;) {
    if (deps.stopped()) return { kind: "stopped" };
    try {
      const job = await deps.poll(id);
      failingSince = null;
      deps.onReconnecting(false);
      if (job.status === "succeeded") {
        return downloadMeshContent(id, deps);
      }
      if (job.status === "failed") {
        const message = job.error?.trim() || FAILED_FALLBACK;
        return job.category === "capacity" && job.retryable === true
          ? { kind: "failed", message, tryAgain: { afterS: job.retryAfterS ?? null } }
          : { kind: "failed", message };
      }
      if (job.status === "canceled") return { kind: "canceled" };
      if (job.status === "missing") return { kind: "failed", message: MISSING };
      if (job.waitingForGpu !== undefined) deps.onGpuWait?.(job.waitingForGpu);
    } catch (error) {
      if (deps.stopped()) return { kind: "stopped" };
      if (!deps.isNetworkFailure(error)) throw error;
      failingSince ??= deps.now();
      if (deps.now() - failingSince >= deps.unreachableAfterMs) {
        deps.onReconnecting(false);
        return { kind: "unreachable" };
      }
      deps.onReconnecting(true);
    }
    await deps.wait(meshPollDelayMs(attempt));
    attempt += 1;
  }
}

/** Once succeeded, only content requests govern reconnection and its time budget. */
async function downloadMeshContent(id: string, deps: MeshJobDeps): Promise<MeshJobOutcome> {
  let attempt = 0;
  let failingSince: number | null = null;
  for (;;) {
    if (deps.stopped()) return { kind: "stopped" };
    try {
      const blob = await deps.content(id);
      failingSince = null;
      deps.onReconnecting(false);
      return deps.stopped() ? { kind: "stopped" } : { kind: "done", blob };
    } catch (error) {
      if (deps.stopped()) return { kind: "stopped" };
      if (!deps.isNetworkFailure(error)) throw error;
      failingSince ??= deps.now();
      if (deps.now() - failingSince >= deps.unreachableAfterMs) {
        deps.onReconnecting(false);
        return { kind: "unreachable" };
      }
      deps.onReconnecting(true);
    }
    await deps.wait(meshPollDelayMs(attempt));
    attempt += 1;
  }
}
