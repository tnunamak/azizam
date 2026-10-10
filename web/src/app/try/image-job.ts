/**
 * A picture can take a minute. On a phone, the browser drops a long fetch
 * the moment the person switches apps, and the page then reported a failure
 * for a picture the gateway went on to make. So Try submits the picture as a
 * media job, polls it, and keeps polling when the page comes back.
 *
 * Submitting carries an idempotency key. If the answer to the submit is lost,
 * submitting again with the same key returns the same job: it never makes a
 * second picture. This module owns that loop; the page supplies the requests.
 */

export type JobStatus = {
  status: string;
  error?: string;
  requestId?: string;
};

export type ImageJobDeps<Content> = {
  /** POST with the run's idempotency key. Resolves with the job id. */
  submit: () => Promise<{ id: string; status: string; requestId?: string }>;
  poll: (id: string) => Promise<JobStatus>;
  content: (id: string) => Promise<Content>;
  /** Resolves after `ms`, or early when the page is shown again. */
  wait: (ms: number) => Promise<void>;
  isNetworkFailure: (error: unknown) => boolean;
  now: () => number;
  stopped: () => boolean;
};

export type ImageJobOutcome<Content> =
  | { kind: "done"; content: Content; jobId: string; requestId?: string }
  | { kind: "failed"; message: string; requestId?: string }
  // The connection dropped and never came back in time. The job may well
  // have finished; this is not a failure.
  | { kind: "lost"; jobId?: string }
  | { kind: "stopped" };

export const POLL_MS = 2000;
export const SUBMIT_ATTEMPTS = 4;
/** No answer from the gateway for this long: say it may still be finishing. */
export const LOST_AFTER_MS = 90_000;
/** A picture that takes longer than this is reported as still finishing. */
export const GIVE_UP_AFTER_MS = 10 * 60_000;

const TERMINAL_OK = new Set(["succeeded", "completed"]);
const TERMINAL_BAD = new Set(["failed", "canceled", "cancelled"]);

export async function runImageJob<Content>(
  deps: ImageJobDeps<Content>,
): Promise<ImageJobOutcome<Content>> {
  const started = deps.now();
  let job: { id: string; status: string; requestId?: string } | null = null;
  for (let attempt = 0; attempt < SUBMIT_ATTEMPTS && !job; attempt += 1) {
    if (deps.stopped()) return { kind: "stopped" };
    try {
      job = await deps.submit();
    } catch (error) {
      if (!deps.isNetworkFailure(error)) throw error;
      if (attempt === SUBMIT_ATTEMPTS - 1) return { kind: "lost" };
      await deps.wait(POLL_MS * (attempt + 1));
    }
  }
  if (!job) return { kind: "lost" };

  let lastAnswer = deps.now();
  let status = job.status;
  let requestId = job.requestId;
  for (;;) {
    if (deps.stopped()) return { kind: "stopped" };
    if (TERMINAL_BAD.has(status)) {
      return { kind: "failed", message: failureText(undefined), requestId };
    }
    if (TERMINAL_OK.has(status)) {
      try {
        const content = await deps.content(job.id);
        return { kind: "done", content, jobId: job.id, requestId };
      } catch (error) {
        if (!deps.isNetworkFailure(error)) throw error;
        // Fall through: wait, then try the content again.
      }
    }
    if (deps.now() - started > GIVE_UP_AFTER_MS) {
      return { kind: "lost", jobId: job.id };
    }
    if (deps.now() - lastAnswer > LOST_AFTER_MS) {
      return { kind: "lost", jobId: job.id };
    }
    await deps.wait(POLL_MS);
    if (deps.stopped()) return { kind: "stopped" };
    try {
      const next = await deps.poll(job.id);
      lastAnswer = deps.now();
      status = next.status;
      requestId = next.requestId ?? requestId;
      if (TERMINAL_BAD.has(status)) {
        return {
          kind: "failed",
          message: failureText(next.error),
          requestId,
        };
      }
    } catch (error) {
      if (!deps.isNetworkFailure(error)) throw error;
    }
  }
}

function failureText(error: string | undefined) {
  return error?.trim() || "The gateway could not make this picture.";
}
