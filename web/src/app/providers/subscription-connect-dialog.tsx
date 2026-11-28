"use client";

import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import {
  adminFetch,
  ApiError,
  type ConnectionStatus,
  type SubscriptionStart,
} from "@/lib/api";

const POLL_MS = 3_000;
const POLL_TICKS = 40; // 2 minutes

type Phase = "start" | "paste" | "done";

export interface SubscriptionKindRef {
  kind: string;
  display_name?: string;
  sign_in?: "browser";
  paste_kind?: "code";
  install_page_url?: string;
  runner?: ConnectionStatus["runner"];
}

/** The owner's noun for one kind: "ChatGPT subscription", from the kind's own name. */
export function subscriptionName(kind: SubscriptionKindRef): string {
  return `${kind.display_name || kind.kind} subscription`;
}

/** The gateway's own listener can only answer a browser on the computer running Azizam. */
export function isLoopbackHost(hostname: string): boolean {
  return hostname === "127.0.0.1" || hostname === "localhost";
}

function errorMessage(error: unknown): { code: string; message: string } {
  if (error instanceof ApiError) return { code: error.code, message: error.message };
  return { code: "", message: "Something went wrong. Try again." };
}

/**
 * Connect or reconnect one subscription: sign in on the provider's page, then
 * either the gateway's own listener finishes it (a page on the gateway
 * computer) or the owner pastes the address the browser ended on. The pasted
 * address lives in this component's state only, until the request leaves.
 */
export function SubscriptionConnectDialog({
  open,
  onOpenChange,
  ...rest
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  kind: SubscriptionKindRef;
  /** The row as listed now: a reconnect compares against it. */
  current?: ConnectionStatus;
  reconnect?: boolean;
  onConnected: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* Mounted only while open: every close drops the pasted address. */}
      {open && <ConnectBody onOpenChange={onOpenChange} {...rest} />}
    </Dialog>
  );
}

function ConnectBody({
  onOpenChange,
  kind,
  current,
  reconnect = false,
  onConnected,
}: {
  onOpenChange: (open: boolean) => void;
  kind: SubscriptionKindRef;
  current?: ConnectionStatus;
  reconnect?: boolean;
  onConnected: () => void;
}) {
  const noun = subscriptionName(kind);
  const codeFlow = kind.paste_kind === "code";
  const base = `/admin/api/subscriptions/${encodeURIComponent(kind.kind)}`;
  const [phase, setPhase] = useState<Phase>("start");
  const [attempt, setAttempt] = useState<SubscriptionStart | null>(null);
  const [listening, setListening] = useState(false);
  const [pasted, setPasted] = useState("");
  const [runner, setRunner] = useState(current?.runner ?? kind.runner);
  const action = useRef<HTMLButtonElement>(null);
  const pasteInput = useRef<HTMLInputElement>(null);
  // The progress poll could not read the gateway; the install itself goes on there.
  const [pollLost, setPollLost] = useState(false);
  const previousRunnerState = useRef(runner?.state);
  const runnerName = runner?.name ?? `${kind.display_name || kind.kind} Code`;
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<{ code: string; message: string } | null>(null);
  const [account, setAccount] = useState<string | null>(null);
  const alive = useRef(true);
  const baseline = useRef(current);
  const connectedNotice = useRef(onConnected);
  // Keep the latest props for the poll; declared before the poll effect so it runs first.
  useEffect(() => {
    baseline.current = current;
    connectedNotice.current = onConnected;
  });

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  function succeed(view: ConnectionStatus) {
    setListening(false);
    setPasted("");
    setAccount(view.account_email ?? null);
    setProblem(null);
    setPhase("done");
    connectedNotice.current();
  }

  // Poll CLI completion, or the list while the local callback listener waits.
  useEffect(() => {
    if (!listening) return;
    let cancelled = false;
    let inFlight = false;
    let ticks = 0;
    const before = baseline.current;
    const id = setInterval(async () => {
      if (inFlight) return;
      inFlight = true;
      ticks += 1;
      try {
        if (codeFlow && attempt) {
          const answer = await adminFetch<{ subscription: ConnectionStatus | { status: "pending" } }>(`${base}/finish`, {
            method: "POST", body: { attempt_id: attempt.attempt_id, callback: "" },
          });
          if (!cancelled && answer.subscription.status === "connected") succeed(answer.subscription);
        } else {
          const answer = await adminFetch<{ subscriptions: ConnectionStatus[] }>("/admin/api/subscriptions");
          if (cancelled) return;
          const row = answer.subscriptions.find((entry) => entry.kind === kind.kind);
          const fresh =
            row?.status === "connected" &&
            (before?.status !== "connected" ||
              row.generation !== before.generation ||
              row.connected_at !== before.connected_at);
          if (row && fresh) {
            succeed(row);
            return;
          }
        }
      } catch (error) {
        if (codeFlow && !cancelled && error instanceof ApiError && error.status >= 400 && error.status < 500) {
          setProblem(errorMessage(error));
          setListening(false);
        }
      } finally {
        inFlight = false;
      }
      const maxTicks = codeFlow ? Math.ceil((attempt?.expires_in ?? 600) * 1000 / POLL_MS) : POLL_TICKS;
      if (!cancelled && ticks >= maxTicks) setListening(false);
    }, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [listening, kind.kind, codeFlow, attempt, base]);

  async function startSignIn() {
    setBusy(true);
    setProblem(null);
    try {
      const answer = await adminFetch<{ subscription: SubscriptionStart | ConnectionStatus }>(`${base}/connect`, {
        method: "POST",
        body: isLoopbackHost(window.location.hostname) ? { listen: true } : {},
      });
      if (!alive.current) return;
      if ("status" in answer.subscription) {
        if (answer.subscription.status === "connected") succeed(answer.subscription);
        return;
      }
      setAttempt(answer.subscription);
      setListening(codeFlow || answer.subscription.listening === true);
      setPhase("paste");
      window.open(answer.subscription.authorize_url, "_blank", "noopener,noreferrer");
    } catch (error) {
      if (alive.current) {
        const failure = errorMessage(error);
        if (failure.code === "plan_runner_unavailable" && runner) setRunner({ ...runner, state: "missing" });
        setProblem(failure);
      }
    } finally {
      if (alive.current) setBusy(false);
    }
  }

  async function finish() {
    if (!attempt || !pasted.trim()) return;
    const callback = pasted.trim();
    setPasted("");
    setListening(false);
    setBusy(true);
    setProblem(null);
    try {
      const answer = await adminFetch<{ subscription: ConnectionStatus }>(`${base}/finish`, {
        method: "POST",
        body: { attempt_id: attempt.attempt_id, callback },
      });
      if (alive.current) succeed(answer.subscription);
    } catch (error) {
      if (alive.current) setProblem(errorMessage(error));
    } finally {
      if (alive.current) setBusy(false);
    }
  }

  async function installRunner() {
    setBusy(true);
    setProblem(null);
    try {
      const answer = await adminFetch<{ subscription: ConnectionStatus }>(`${base}/install-runner`, { method: "POST", body: {} });
      if (alive.current) setRunner(answer.subscription.runner);
    } catch (error) {
      if (alive.current) setProblem(errorMessage(error));
    } finally {
      if (alive.current) setBusy(false);
    }
  }

  useEffect(() => {
    if (runner?.state !== "installing") return;
    let cancelled = false;
    let inFlight = false;
    const id = setInterval(async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const answer = await adminFetch<{ subscriptions: ConnectionStatus[] }>("/admin/api/subscriptions");
        const next = answer.subscriptions.find((row) => row.kind === kind.kind)?.runner;
        if (cancelled) return;
        setPollLost(!next);
        if (next) setRunner(next);
      } catch {
        // A missed read does not stop an installation on the gateway: keep checking, and say so.
        if (!cancelled) setPollLost(true);
      } finally {
        inFlight = false;
      }
    }, POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, [runner?.state, kind.kind]);

  useEffect(() => {
    if (phase === "paste") pasteInput.current?.focus();
  }, [phase]);

  useEffect(() => {
    if (previousRunnerState.current === "installing" && runner?.state !== "installing") action.current?.focus();
    previousRunnerState.current = runner?.state;
  }, [runner?.state]);

  function startAgain() {
    setAttempt(null);
    setListening(false);
    setPasted("");
    setProblem(null);
    setPhase("start");
  }

  const title = `${reconnect ? "Reconnect" : "Connect"} ${noun}`;
  const stepLabel = phase === "start" ? "Step 1 of 2 · Sign in" : phase === "paste" ? "Step 2 of 2 · Finish" : null;

  return (
    <DialogContent className="sm:max-w-md" aria-describedby={undefined}>
      <DialogHeader className="text-left">
        <DialogTitle>{title}</DialogTitle>
        {stepLabel && <DialogDescription>{stepLabel}</DialogDescription>}
      </DialogHeader>

      {phase === "start" && (
        <div className="space-y-4 text-body">
          <p>
            Connecting signs in to your subscription. Only the people and apps in the access plans you choose can use it.
          </p>
          {runner && runner.state !== "ready" ? (
            <>
              <p>{runnerName} is needed to sign in. Azizam downloads it from {runner?.source ?? "the provider"}.</p>
              {runner.state === "installing" && <p role="status"><Spinner className="mr-2 inline size-4" />Installing {runnerName}…{typeof runner.percent === "number" ? ` ${runner.percent}%` : ""}</p>}
              {runner.state === "installing" && pollLost && <p className="text-meta text-muted-foreground">{"The download is still running. Azizam can't show its progress right now and will keep checking."}</p>}
              {runner.error && <p role="alert" className="break-words">{runner.error.message}</p>}
              <Button ref={action} type="button" onClick={() => void installRunner()} disabled={busy || runner.state === "installing"}>
                {runner.state === "failed" ? "Try again" : runner.state === "installing" ? "Installing…" : `Install ${runnerName}`}
              </Button>
              {runner.state === "failed" && kind.install_page_url && <p><a href={kind.install_page_url} target="_blank" rel="noopener noreferrer" className="underline">Installation help</a></p>}
            </>
          ) : (
            <Button ref={action} type="button" onClick={() => void startSignIn()} disabled={busy}>
              {busy && <Spinner className="size-4" />}
              Open {kind.display_name || kind.kind} sign-in
            </Button>
          )}
        </div>
      )}

      {phase === "paste" && (
        <div className="space-y-4 text-body">
          {listening ? (
            <p className="text-meta text-muted-foreground" role="status">
              <Spinner className="mr-2 inline size-4" />
              Waiting for the sign-in to finish. This page continues by itself.
            </p>
          ) : null}
          {codeFlow ? <p>{kind.display_name || kind.kind} shows a code after you sign in. Paste it here.</p> : <p>
            The provider will send you to a page that does not load (
            <code>{attempt?.redirect_uri ? new URL(attempt.redirect_uri).host : "127.0.0.1"}</code>). That is expected. Copy
            the whole address from the browser bar and paste it here.
          </p>}
          <Field label={codeFlow ? `Code from ${kind.display_name || kind.kind}` : "Address from the browser bar"}>
            <Input
              ref={pasteInput}
              value={pasted}
              onChange={(event) => setPasted(event.target.value)}
              autoComplete="off"
              spellCheck={false}
              disabled={busy}
            />
          </Field>
          {attempt && (
            <p className="text-meta text-muted-foreground">
              The sign-in page did not open?{" "}
              <a className="underline underline-offset-2" href={attempt.authorize_url} target="_blank" rel="noopener noreferrer">
                Open it again
              </a>
            </p>
          )}
        </div>
      )}

      {phase === "done" && (
        <p className="text-body" role="status">
          {account ? `Connected as ${account}.` : `${noun[0].toUpperCase()}${noun.slice(1)} connected.`} Nobody else can use it
          until you add it to an access plan.
        </p>
      )}

      {problem && (
        <div role="alert" className="text-meta text-danger">
          <p>{problem.message}</p>
          {problem.code === "plan_runner_unavailable" && kind.install_page_url && (
            <a href={kind.install_page_url} target="_blank" rel="noopener noreferrer" className="underline">Installation help</a>
          )}
        </div>
      )}

      <DialogFooter>
        {phase === "done" ? (
          <Button type="button" onClick={() => onOpenChange(false)}>
            Done
          </Button>
        ) : (
          <>
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            {(problem?.code === "attempt_not_found" || problem?.code === "state_mismatch" || problem?.code === "plan_sign_in_refused") && (
              <Button type="button" onClick={startAgain}>
                Start again
              </Button>
            )}
            {phase === "paste" && (
              <>
                <Button type="button" variant="ghost" onClick={startAgain} disabled={busy}>
                  Back
                </Button>
                <Button type="button" onClick={() => void finish()} disabled={busy || !pasted.trim()}>
                  {busy && <Spinner className="size-4" />}
                  {codeFlow ? "Connect" : "Finish"}
                </Button>
              </>
            )}

          </>
        )}
      </DialogFooter>
    </DialogContent>
  );
}
