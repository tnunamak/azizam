/**
 * A browser reports a dropped connection as a bare TypeError: "Failed to
 * fetch" (Chrome), "NetworkError when attempting to fetch resource."
 * (Firefox), "Load failed" (Safari). A phone does this whenever the person
 * switches apps mid-request. That text is the browser's, not ours, and it
 * says nothing about whether the gateway finished the work, so it never
 * reaches the screen.
 */
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

/** `fetch`, except a dropped connection becomes a NetworkUnavailableError. */
export async function networkFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  try {
    return await fetch(input, init);
  } catch (error) {
    if (isNetworkFailure(error)) throw new NetworkUnavailableError({ cause: error });
    throw error;
  }
}
