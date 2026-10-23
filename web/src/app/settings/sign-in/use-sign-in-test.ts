"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "@/lib/toast";
import {
  cancelSignInTest,
  getSignInTest,
  startSignInTest,
  type ProviderConnection,
  type TestFailureCause,
  type TestStatus,
} from "@/lib/signin-api";
import { connectionSig } from "./form";
import type { LocalCause } from "./copy";

export const POLL_MS = 1500;
export const TEST_TIMEOUT_MS = 5 * 60_000;

type Passed = Extract<TestStatus, { status: "passed" }>;

/** What the result card shows. */
export type TestResult =
  | { kind: "pass"; testId: string; status: Passed }
  | {
      kind: "fail";
      cause: TestFailureCause | LocalCause;
      /** Plain words from the server; local causes get their text from copy. */
      message?: string;
      details?: string;
    };

/** The last test that passed, and the connection it passed for. */
export interface PassedTest {
  testId: string;
  signature: string;
  /** Seconds since epoch. */
  at: number;
  noGroups: boolean;
  /** The test's sign-in came back fresh: the provider asked for a password just now. */
  freshLogin: boolean;
}

interface Run {
  id: string | null;
  popup: Window;
  signature: string;
  startedAt: number;
  previous: TestResult | null;
  cancelled: boolean;
  sawClosed: boolean;
  timer: ReturnType<typeof setTimeout> | null;
}

/**
 * The pop-up flow of Test sign-in. The pop-up opens in the click (so a browser
 * does not block it), then the server starts the test and the page polls for
 * the answer. The server tells passed from failed; the page adds three causes
 * only it can see: the pop-up was blocked, the window closed, nothing came back.
 */
export function useSignInTest() {
  const [waiting, setWaiting] = useState(false);
  const [result, setResultState] = useState<TestResult | null>(null);
  const [passed, setPassed] = useState<PassedTest | null>(null);
  const resultRef = useRef<TestResult | null>(null);
  const run = useRef<Run | null>(null);

  const setResult = useCallback((next: TestResult | null) => {
    resultRef.current = next;
    setResultState(next);
  }, []);

  const stop = useCallback(() => {
    const current = run.current;
    if (!current) return;
    current.cancelled = true;
    if (current.timer) clearTimeout(current.timer);
    run.current = null;
    setWaiting(false);
  }, []);

  useEffect(() => stop, [stop]);

  const finish = useCallback(
    (current: Run, status: TestStatus) => {
      if (current.cancelled || status.status === "waiting") return;
      try {
        current.popup.close();
      } catch {
        // The window may already be gone.
      }
      stop();
      if (status.status === "passed") {
        setPassed({
          testId: current.id ?? "",
          signature: current.signature,
          at: status.tested_at,
          noGroups: status.warnings.includes("no_groups"),
          freshLogin: status.fresh_login_supported,
        });
        setResult({ kind: "pass", testId: current.id ?? "", status });
      } else {
        setResult({
          kind: "fail",
          cause: status.cause,
          message: status.message,
          details: status.details,
        });
      }
    },
    [setResult, stop],
  );

  const fail = useCallback(
    (current: Run, cause: LocalCause) => {
      if (current.cancelled) return;
      try {
        current.popup.close();
      } catch {
        // The window may already be gone.
      }
      if (current.id) void cancelSignInTest(current.id).catch(() => undefined);
      stop();
      setResult({ kind: "fail", cause });
    },
    [setResult, stop],
  );

  const poll = useCallback(
    async (current: Run) => {
      if (current.cancelled || !current.id) return;
      let status: TestStatus | null = null;
      try {
        status = await getSignInTest(current.id);
      } catch {
        // A missed poll is not an answer. The next one asks again.
      }
      if (current.cancelled) return;
      if (status && status.status !== "waiting") return finish(current, status);
      if (Date.now() - current.startedAt >= TEST_TIMEOUT_MS)
        return fail(current, "timeout");
      if (current.popup.closed) {
        if (current.sawClosed) return fail(current, "closed");
        // Look once more: the answer may have arrived as the window closed.
        current.sawClosed = true;
        return poll(current);
      }
      current.timer = setTimeout(() => void poll(current), POLL_MS);
    },
    [fail, finish],
  );

  /** Call from the click handler. `window.open` must run in the click. */
  const start = useCallback(
    (provider: ProviderConnection) => {
      const popup = window.open("", "_blank");
      if (!popup) {
        setResult({ kind: "fail", cause: "blocked" });
        return;
      }
      stop();
      const current: Run = {
        id: null,
        popup,
        signature: connectionSig(provider),
        startedAt: Date.now(),
        previous: resultRef.current,
        cancelled: false,
        sawClosed: false,
        timer: null,
      };
      run.current = current;
      setWaiting(true);
      startSignInTest(provider).then(
        ({ test_id, start_url }) => {
          if (current.cancelled) {
            void cancelSignInTest(test_id).catch(() => undefined);
            return;
          }
          current.id = test_id;
          popup.location.href = start_url;
          current.timer = setTimeout(() => void poll(current), POLL_MS);
        },
        (error: unknown) => {
          if (current.cancelled) return;
          try {
            popup.close();
          } catch {
            // The window may already be gone.
          }
          stop();
          setResult({
            kind: "fail",
            cause: "start_failed",
            message: error instanceof Error ? error.message : undefined,
          });
        },
      );
    },
    [poll, setResult, stop],
  );

  const cancel = useCallback(() => {
    const current = run.current;
    if (!current) return;
    stop();
    if (current.id) void cancelSignInTest(current.id).catch(() => undefined);
    try {
      current.popup.close();
    } catch {
      // The window may already be gone.
    }
    setResult(current.previous);
    toast.info("Test canceled.");
  }, [setResult, stop]);

  return {
    waiting,
    result,
    passed,
    start,
    cancel,
    dismiss: () => setResult(null),
  };
}
