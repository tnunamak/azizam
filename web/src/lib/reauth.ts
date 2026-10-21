/** Step-up re-authentication: one shared prompt for every risky owner action.
 *
 * The gateway answers a risky action from a stale browser session with
 * `401 reauth_required` (gateway/step_up.py). `adminFetch` hands that answer
 * to `requestReauth`, which opens ONE prompt however many requests wait on it,
 * then retries each request once after the person confirms. This module holds
 * only the shared state; the dialog lives in components/shell/reauth-dialog.tsx
 * and is loaded the first time a prompt is needed.
 *
 * No import from api.ts: api.ts imports this file. */

import { useSyncExternalStore } from "react";

export const REAUTH_REQUIRED_CODE = "reauth_required";

export type ReauthMethod = "password" | "oidc" | "sign_in";

export interface ReauthInfo {
  methods: ReauthMethod[];
  windowSeconds: number;
  oidcUrl: string;
}

const METHODS: readonly string[] = ["password", "oidc", "sign_in"];

/** The prompt details from a `reauth_required` error body, or null when the
 * body is anything else (an ordinary 401 means the session ended). */
export function parseReauth(payload: unknown): ReauthInfo | null {
  const error = (payload as { error?: unknown } | null)?.error;
  if (!error || typeof error !== "object") return null;
  const { code, reauth } = error as { code?: unknown; reauth?: unknown };
  if (code !== REAUTH_REQUIRED_CODE) return null;
  const raw = (reauth && typeof reauth === "object" ? reauth : {}) as {
    methods?: unknown;
    window_seconds?: unknown;
    oidc_url?: unknown;
  };
  const methods = Array.isArray(raw.methods)
    ? (raw.methods.filter(
        (method): method is ReauthMethod =>
          typeof method === "string" && METHODS.includes(method),
      ) as ReauthMethod[])
    : [];
  return {
    // A body without a usable method still gets a way out: sign in again.
    methods: methods.length ? methods : ["sign_in"],
    windowSeconds:
      typeof raw.window_seconds === "number" ? raw.window_seconds : 900,
    oidcUrl:
      typeof raw.oidc_url === "string" ? raw.oidc_url : "/login?reauth=1",
  };
}

interface Pending {
  info: ReauthInfo;
  promise: Promise<boolean>;
  settle: (confirmed: boolean) => void;
}

let pending: Pending | null = null;
let invoker: HTMLElement | null = null;
const listeners = new Set<() => void>();

function emit() {
  for (const listener of listeners) listener();
}

/** Ask the person to confirm. Callers that arrive while a prompt is open share
 * it. Resolves true once they confirmed, false when they cancelled. */
export function requestReauth(info: ReauthInfo): Promise<boolean> {
  if (pending) return pending.promise;
  // What had focus when the action was refused: where focus returns afterwards.
  invoker =
    typeof document === "undefined"
      ? null
      : (document.activeElement as HTMLElement | null);
  let settle!: (confirmed: boolean) => void;
  const promise = new Promise<boolean>((resolve) => {
    settle = resolve;
  });
  const current: Pending = {
    info,
    promise,
    settle: (confirmed) => {
      if (pending !== current) return;
      pending = null;
      settle(confirmed);
      emit();
    },
  };
  pending = current;
  emit();
  return promise;
}

/** The control that had focus when the last prompt opened. */
export function reauthInvoker(): HTMLElement | null {
  return invoker;
}

/** Close the open prompt: true after a confirmed password, false on cancel. */
export function settleReauth(confirmed: boolean): void {
  pending?.settle(confirmed);
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function getPending(): ReauthInfo | null {
  return pending?.info ?? null;
}

/** The open prompt's details, or null while none is open. */
export function usePendingReauth(): ReauthInfo | null {
  return useSyncExternalStore(subscribe, getPending, () => null);
}
