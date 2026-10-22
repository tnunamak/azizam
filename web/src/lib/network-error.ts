/**
 * A browser reports a dropped connection as a bare TypeError: "Failed to
 * fetch" (Chrome), "NetworkError when attempting to fetch resource."
 * (Firefox), "Load failed" (Safari). A phone does this whenever the person
 * switches apps mid-request. That text is the browser's, not ours, and it
 * says nothing about whether the gateway finished the work, so it never
 * reaches the screen.
 */
import { fetchEarly } from "@/lib/early-fetch";
import {
  SessionEnded,
  sessionAccepts,
  sessionEpoch,
  sessionIsClosed,
  sessionSignal,
} from "@/lib/session-end";
export const NETWORK_FAILURE_MESSAGE =
  "Can’t reach Azizam. Check your connection, then try again.";

export class NetworkUnavailableError extends Error {
  readonly code = "network_unavailable";
  readonly status = 0;
  constructor(options?: { cause?: unknown }) {
    super(NETWORK_FAILURE_MESSAGE, options);
    this.name = "NetworkUnavailableError";
  }
}

/** True for a fetch that never got an HTTP response. */
export function isNetworkFailure(error: unknown): boolean {
  if (error instanceof NetworkUnavailableError) return true;
  return (
    error instanceof TypeError &&
    /failed to fetch|networkerror|load failed|network request failed|fetch failed/i.test(
      error.message,
    )
  );
}

/**
 * `fetch`, except a dropped connection becomes a NetworkUnavailableError, and the page's session
 * governs every request. After the session ends the request is refused (SessionEnded); the session
 * signal aborts it while it is in flight; and an answer that arrives after the session ended, or for
 * a person the page no longer shows, is dropped (session-end.ts). Every fetch helper goes through
 * here, so none can forget the guard.
 */
export async function networkFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
  early = false,
): Promise<Response> {
  if (sessionIsClosed()) throw new SessionEnded();
  const started = sessionEpoch();
  const joined = joinSignals(init?.signal ?? undefined, sessionSignal());
  try {
    const request = { ...init, signal: joined.signal };
    // `early`: a GET the document head may already have started (lib/early-fetch.ts).
    const response =
      early && typeof input === "string" ? await fetchEarly(input, request) : await fetch(input, request);
    if (!sessionAccepts(started)) throw new SessionEnded();
    return response;
  } catch (error) {
    if (error instanceof SessionEnded || sessionIsClosed()) throw new SessionEnded();
    if (isNetworkFailure(error)) throw new NetworkUnavailableError({ cause: error });
    throw error;
  } finally {
    joined.release();
  }
}

/** The caller's abort signal and the session's, as one signal. */
function joinSignals(
  own: AbortSignal | undefined,
  session: AbortSignal,
): { signal: AbortSignal; release: () => void } {
  if (!own) return { signal: session, release: () => {} };
  if (typeof AbortSignal.any === "function") {
    return { signal: AbortSignal.any([own, session]), release: () => {} };
  }
  // Older browsers: forward the aborts, and remove the listeners when the answer is in.
  const joined = new AbortController();
  const abort = () => joined.abort();
  if (own.aborted || session.aborted) joined.abort();
  own.addEventListener("abort", abort, { once: true });
  session.addEventListener("abort", abort, { once: true });
  return {
    signal: joined.signal,
    release: () => {
      own.removeEventListener("abort", abort);
      session.removeEventListener("abort", abort);
    },
  };
}
