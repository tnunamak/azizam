import { meshPollDelayMs } from "./mesh-mode";

/** What a poll of the job route says. "missing" is a job the gateway no longer knows. */
export type MeshJobStatus = { status: string; error?: string };

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
  /** The clock, in milliseconds. */
  now: () => number;
  /** How long requests may fail one after another before the follow gives up asking and says so. */
  unreachableAfterMs: number;
};

export type MeshJobOutcome =
  | { kind: "done"; blob: Blob }
  | { kind: "failed"; message: string }
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
        const blob = await deps.content(id);
        failingSince = null;
        deps.onReconnecting(false);
        return deps.stopped() ? { kind: "stopped" } : { kind: "done", blob };
      }
      if (job.status === "failed") {
        return { kind: "failed", message: job.error?.trim() || FAILED_FALLBACK };
      }
      if (job.status === "canceled") return { kind: "canceled" };
      if (job.status === "missing") return { kind: "failed", message: MISSING };
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
