/**
 * This page's session guard: whether its session has ended, the abort signal of its requests, and the
 * session it shows (its view id). network-error.ts enforces it on every request; session-end.ts (the
 * sign-out) changes it. This module imports nothing, so both can depend on it without a cycle.
 */

/** Rejects a request that the session ended before, or during, its answer. */
export class SessionEnded extends Error {
  readonly code = "session_ended";
  constructor() {
    super("This session has ended.");
    this.name = "SessionEnded";
  }
}

let closed = false;
let scope = new AbortController();
let shownEpoch: string | null = null;

export function sessionIsClosed(): boolean {
  return closed;
}

/** The abort signal every request of this page carries; it aborts when the session ends. */
export function sessionSignal(): AbortSignal {
  return scope.signal;
}

/** A response is current while this tab is open and the person it shows has not changed since the request began. */
export function sessionAccepts(startedEpoch: string | null): boolean {
  return !closed && (startedEpoch === null || startedEpoch === shownEpoch);
}

export function sessionEpoch(): string | null {
  return shownEpoch;
}

/** Ends this page's session: its requests abort, and new ones are refused from now on. */
export function closeSession(): void {
  if (closed) return;
  closed = true;
  scope.abort();
}

/** Records the session this page shows (its view id), or clears it. */
export function setSessionEpoch(next: string | null): void {
  shownEpoch = next;
}

/** Test-only: a live page with no session shown. */
export function resetSessionGuardForTests(): void {
  closed = false;
  scope = new AbortController();
  shownEpoch = null;
}
