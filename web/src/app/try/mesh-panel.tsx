"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { MediaInput } from "@/components/media-input/media-input";
import { Input } from "@/components/ui/input";
import {
  NetworkUnavailableError,
  isNetworkFailure,
  networkFetch,
} from "@/lib/network-error";
import { followMeshJob, type MeshJobStatus } from "./mesh-job";
import {
  MESH_BUSY_MESSAGE,
  MESH_CANCELED_MESSAGE,
  MESH_CANCELLING_MESSAGE,
  MESH_JOB_PATH,
  MESH_MEDIA_TYPE,
  MESH_PATH,
  MESH_PICTURE_TYPES,
  MESH_RECONNECTING_MESSAGE,
  MESH_STOPPED_MESSAGE,
  MESH_UNREACHABLE_MESSAGE,
  MESH_WAIT_MESSAGE,
  meshDownloadName,
  meshElapsed,
  meshFailureMessage,
  meshForm,
  meshJobFrom,
  meshSeedProblem,
  meshUnreachableAfterMs,
  readStoredMeshJob,
  writeStoredMeshJob,
  type MeshBackground,
  type MeshDetail,
  type StoredMeshJob,
} from "./mesh-mode";
import { MeshViewer } from "./mesh-viewer";

export type MeshModeProps = {
  /** The manifest's detail choices and the one selected first. */
  detail: { choices: MeshDetail[]; defaultValue: number | null };
  /** The Try key's secret, prepared on first use. Null when Try could not prepare one. */
  getSecret: () => Promise<string | null>;
  /** The GPU is busy or starting: the form stays, but a new picture cannot be sent yet. */
  busy: boolean;
  /** The Photo to 3D tab is open. The viewer mounts only then, so it is never sized while its tab is hidden. */
  active: boolean;
  /** A picture sent from Home: the panel takes it and starts the model as soon as it can. */
  initialFile?: File | null;
};

/** "unreachable": the gateway has not answered for a while; the job's id is kept and "Check again" asks once more. */
type Phase = "idle" | "running" | "unreachable" | "done" | "error";
type Result = { blob: Blob; downloadName: string; picture: string };

/** A refusal from the gateway, carrying the server's own message. */
class MeshFailure extends Error {}

const selectClass =
  "border-input bg-card rounded-control min-h-11 w-full border px-3 text-base";

const jobUrl = (id: string, suffix = "") =>
  `${MESH_JOB_PATH}${encodeURIComponent(id)}${suffix}`;
const bearer = (secret: string) => ({ authorization: `Bearer ${secret}` });

/** A server fault or a busy gateway is a wait, not an answer: the job goes on without this page. */
function transient(response: Response) {
  if (response.status >= 500 || response.status === 429) {
    throw new NetworkUnavailableError();
  }
}

async function pollJob(
  secret: string,
  id: string,
  signal: AbortSignal,
): Promise<MeshJobStatus> {
  const response = await networkFetch(jobUrl(id), {
    headers: bearer(secret),
    signal,
  });
  if (response.status === 404) return { status: "missing" };
  transient(response);
  const payload = (await response.json().catch(() => null)) as {
    status?: unknown;
    error?: unknown;
    error_category?: unknown;
    retryable?: unknown;
  } | null;
  if (!response.ok) throw new MeshFailure(meshFailureMessage(payload));
  const status: MeshJobStatus = {
    status: String(payload?.status ?? ""),
    error: typeof payload?.error === "string" ? payload.error : undefined,
  };
  if (status.status === "failed") {
    status.category = typeof payload?.error_category === "string" ? payload.error_category : undefined;
    status.retryable = payload?.retryable === true;
    if (status.category === "capacity" && status.retryable) {
      status.retryAfterS = await retryAfterSeconds(secret, id, signal);
    }
  }
  return status;
}

/** The newest delay the gateway wrote for a job it refused for capacity. The delay is a courtesy: no answer means no delay. */
async function retryAfterSeconds(secret: string, id: string, signal: AbortSignal): Promise<number | undefined> {
  try {
    const response = await networkFetch(jobUrl(id, "/events"), { headers: bearer(secret), signal });
    if (!response.ok) return undefined;
    const body = (await response.json()) as { events?: { event_type?: unknown; payload?: { step?: unknown } }[] };
    let found: number | undefined;
    for (const event of body.events ?? []) {
      const step = event.payload?.step;
      if (event.event_type === "retry_after" && typeof step === "number" && step > 0) found = step;
    }
    return found;
  } catch {
    return undefined;
  }
}

async function fetchModel(
  secret: string,
  id: string,
  signal: AbortSignal,
): Promise<Blob> {
  const response = await networkFetch(jobUrl(id, "/content"), {
    headers: bearer(secret),
    signal,
  });
  transient(response);
  if (!response.ok) {
    const payload: unknown = await response.json().catch(() => null);
    throw new MeshFailure(meshFailureMessage(payload));
  }
  return response.blob();
}

/**
 * The Photo to 3D panel: one picture in, one GLB out, shown in a viewer. It sends its own multipart request, because
 * the answer is a model, not text or a picture the generic Try result can show.
 *
 * The run takes minutes under load, so the request asks to be answered as a job (`Prefer: respond-async`) and the
 * panel follows the job. The job lives in the gateway, not in this page's connection: a dropped connection shows
 * "Reconnecting…" and goes on, and the job's id waits in sessionStorage so a reload, or a phone coming back from
 * another app, finds the job again.
 */
export function MeshMode({ detail, getSecret, busy, active, initialFile = null }: MeshModeProps) {
  const [file, setFile] = useState<File | null>(null);
  const [resolution, setResolution] = useState<number | null>(
    detail.defaultValue,
  );
  const [seed, setSeed] = useState("");
  const [background, setBackground] = useState<MeshBackground>("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [result, setResult] = useState<Result | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  // Set when the gateway refused for lack of room and says asking again can work; `afterS` is its suggested wait.
  const [retry, setRetry] = useState<{ afterS: number | null } | null>(null);
  const [reconnecting, setReconnecting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [startedAt, setStartedAt] = useState(0);
  const [now, setNow] = useState(0);
  const controller = useRef<AbortController | null>(null);
  const jobId = useRef<string | null>(null);
  const secretRef = useRef<string | null>(null);
  // Ends the current wait between two polls early: the page was shown again or the connection came back.
  const wake = useRef<(() => void) | null>(null);

  // Leaving the page stops this page's polling and nothing else: the job keeps running in the gateway, and its id
  // stays in sessionStorage for the next visit.
  useEffect(() => () => controller.current?.abort(), []);
  useEffect(() => {
    const onShown = () => {
      if (document.visibilityState !== "hidden") wake.current?.();
    };
    document.addEventListener("visibilitychange", onShown);
    window.addEventListener("online", onShown);
    window.addEventListener("pageshow", onShown);
    return () => {
      document.removeEventListener("visibilitychange", onShown);
      window.removeEventListener("online", onShown);
      window.removeEventListener("pageshow", onShown);
    };
  }, []);
  const running = phase === "running";
  const unreachable = phase === "unreachable";
  useEffect(() => {
    if (!running) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);

  /** Stop waiting: this page stops asking. The job is not touched, and a reload no longer picks it up. */
  function stopWaiting() {
    writeStoredMeshJob(window.sessionStorage, null);
    controller.current?.abort();
    jobId.current = null;
    setReconnecting(false);
    setCancelling(false);
    setPhase("idle");
    setMessage(MESH_STOPPED_MESSAGE);
  }

  /** Cancel: ask the gateway to end the job. The page keeps following it until the job says it has ended. */
  async function cancel() {
    const id = jobId.current;
    const secret = secretRef.current;
    if (!id || !secret) {
      // No job yet: the request that would make one has not been answered.
      controller.current?.abort();
      return;
    }
    setCancelling(true);
    try {
      const response = await networkFetch(jobUrl(id, "/cancel"), {
        method: "POST",
        headers: bearer(secret),
      });
      // 409: the job has already ended; the poll reports how.
      if (!response.ok && response.status !== 409) {
        throw new MeshFailure("The 3D model could not be canceled. Try again.");
      }
      wake.current?.();
    } catch (error) {
      setCancelling(false);
      setMessage(
        error instanceof MeshFailure || isNetworkFailure(error)
          ? (error as Error).message
          : "The 3D model could not be canceled. Try again.",
      );
    }
  }

  /** Check again: ask about the kept job once more, from where it left off. */
  async function checkAgain() {
    const stored = readStoredMeshJob(window.sessionStorage);
    if (!stored) {
      setPhase("idle");
      return;
    }
    const run = new AbortController();
    controller.current = run;
    setPhase("running");
    const secret = secretRef.current ?? (await getSecret());
    if (run.signal.aborted) return;
    if (!secret) {
      setPhase("unreachable");
      return;
    }
    await follow(secret, stored, run);
  }

  /** Follow one job to its end and show how it ended. `run` is this follow's own abort handle. */
  async function follow(
    secret: string,
    job: StoredMeshJob,
    run: AbortController,
  ) {
    secretRef.current = secret;
    jobId.current = job.id;
    setStartedAt(job.startedAt);
    setPhase("running");
    try {
      const outcome = await followMeshJob(job.id, {
        poll: (id) => pollJob(secret, id, run.signal),
        content: (id) => fetchModel(secret, id, run.signal),
        wait: (ms) =>
          new Promise<void>((resolve) => {
            const finish = () => {
              window.clearTimeout(timer);
              wake.current = null;
              resolve();
            };
            const timer = window.setTimeout(finish, ms);
            wake.current = finish;
            run.signal.addEventListener("abort", finish, { once: true });
          }),
        isNetworkFailure,
        stopped: () => run.signal.aborted,
        onReconnecting: setReconnecting,
        now: Date.now,
        unreachableAfterMs: meshUnreachableAfterMs(),
      });
      if (outcome.kind === "stopped") return;
      if (outcome.kind === "unreachable") {
        // The job's id stays in sessionStorage: a reload, or "Check again", asks about it again.
        setPhase("unreachable");
        return;
      }
      writeStoredMeshJob(window.sessionStorage, null);
      if (outcome.kind === "done") {
        setResult({
          blob: outcome.blob,
          downloadName: meshDownloadName(new Date()),
          picture: job.picture,
        });
        setPhase("done");
      } else if (outcome.kind === "canceled") {
        setPhase("idle");
        setMessage(MESH_CANCELED_MESSAGE);
      } else {
        setPhase("error");
        setMessage(outcome.message);
        setRetry(outcome.retry ?? null);
      }
    } catch (error) {
      if (run.signal.aborted) return;
      writeStoredMeshJob(window.sessionStorage, null);
      setPhase("error");
      setMessage(
        error instanceof MeshFailure
          ? error.message
          : "The 3D model could not be made. Try again.",
      );
    } finally {
      setReconnecting(false);
      setCancelling(false);
      if (controller.current === run) {
        controller.current = null;
        jobId.current = null;
      }
    }
  }

  // A job left running when the page was left or reloaded: follow it again.
  useEffect(() => {
    const stored = readStoredMeshJob(window.sessionStorage);
    if (!stored) return;
    const run = new AbortController();
    controller.current = run;
    setPhase("running");
    setStartedAt(stored.startedAt);
    void (async () => {
      const secret = await getSecret();
      if (run.signal.aborted) return;
      if (!secret) {
        setPhase("idle");
        return;
      }
      await follow(secret, stored, run);
    })();
    // Once per visit: follow reads the state this effect starts from.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // A picture from Home starts the model once the panel can run: its tab is showing, the GPU is free, and the
  // detail choice is known. It starts once; after that the person drives the form.
  const startOnFile = useRef(false);
  useEffect(() => {
    if (!initialFile) return;
    setFile(initialFile);
    startOnFile.current = true;
  }, [initialFile]);
  useEffect(() => {
    if (resolution === null && detail.defaultValue !== null) setResolution(detail.defaultValue);
  }, [detail.defaultValue, resolution]);
  useEffect(() => {
    if (!startOnFile.current || !file || !active || busy || resolution === null || phase !== "idle") return;
    startOnFile.current = false;
    void generate();
    // generate reads the state this effect's dependencies name.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [file, active, busy, resolution, phase]);

  async function generate(event?: FormEvent) {
    event?.preventDefault();
    if (!file) {
      setFieldError("Choose a picture first.");
      return;
    }
    const problem = meshSeedProblem(seed);
    if (problem) {
      setFieldError(problem);
      return;
    }
    if (resolution === null) return;
    setFieldError(null);
    setMessage(null);
    setRetry(null);
    setResult(null);
    setPhase("running");
    setStartedAt(Date.now());
    const run = new AbortController();
    controller.current = run;
    const picture = file.name;
    try {
      const secret = await getSecret();
      if (run.signal.aborted) return;
      if (!secret) {
        setPhase("idle");
        return;
      }
      const response = await networkFetch(MESH_PATH, {
        method: "POST",
        headers: { ...bearer(secret), prefer: "respond-async" },
        body: meshForm({ image: file, seed, resolution, background }),
        signal: run.signal,
      });
      if (!response.ok) {
        const payload: unknown = await response.json().catch(() => null);
        const code = (payload as { error?: { code?: unknown } } | null)?.error?.code;
        if (response.status === 503 && code === "resource_unavailable") {
          const wait = Number(response.headers.get("retry-after"));
          setRetry({ afterS: Number.isFinite(wait) && wait > 0 ? wait : null });
        }
        throw new MeshFailure(meshFailureMessage(payload));
      }
      if (response.headers.get("content-type")?.startsWith(MESH_MEDIA_TYPE)) {
        // A gateway that answered in one go: the model is the answer.
        const blob = await response.blob();
        if (run.signal.aborted) return;
        setResult({ blob, downloadName: meshDownloadName(new Date()), picture });
        setPhase("done");
        return;
      }
      const job = meshJobFrom(await response.json().catch(() => null));
      if (!job) throw new MeshFailure(meshFailureMessage(null));
      const stored = { id: job.id, startedAt: Date.now(), picture };
      writeStoredMeshJob(window.sessionStorage, stored);
      await follow(secret, stored, run);
    } catch (error) {
      if (run.signal.aborted) {
        if (jobId.current === null) {
          setPhase("idle");
          setMessage(MESH_STOPPED_MESSAGE);
        }
        return;
      }
      setPhase("error");
      setMessage(
        error instanceof MeshFailure || isNetworkFailure(error)
          ? (error as Error).message
          : "The 3D model could not be made. Try again.",
      );
    } finally {
      if (controller.current === run) controller.current = null;
    }
  }

  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
      <form
        noValidate
        onSubmit={(event) => void generate(event)}
        className="bg-card min-w-0 space-y-4 rounded-xl border p-5"
      >
        <p className="text-muted-foreground text-sm">
          Send one picture. Get a 3D model you can turn around and download.
        </p>
        <div role="group" aria-labelledby="mesh-picture-label" className="space-y-2">
          <span id="mesh-picture-label" className="text-sm font-medium">
            Picture
          </span>
          <MediaInput
            id="mesh-picture"
            label="Picture"
            kind="image"
            types={MESH_PICTURE_TYPES}
            accepted="a PNG, JPEG or WebP picture"
            hint="PNG, JPEG or WebP."
            file={file}
            onFileChange={setFile}
          />
        </div>
        {detail.choices.length > 0 && (
          <label className="block space-y-2">
            <span className="text-sm font-medium">Detail</span>
            <select
              className={selectClass}
              value={resolution ?? ""}
              onChange={(event) => setResolution(Number(event.target.value))}
            >
              {detail.choices.map((choice) => (
                <option key={choice.value} value={choice.value}>
                  {choice.label}
                </option>
              ))}
            </select>
            <span className="text-muted-foreground block text-xs">
              Higher detail is slower.
            </span>
          </label>
        )}
        <details className="border-t pt-3">
          <summary className="min-h-11 cursor-pointer py-3 text-sm font-medium">
            More options
          </summary>
          <div className="grid gap-3 pb-3">
            <label className="block space-y-2">
              <span className="text-sm font-medium">Variation number</span>
              <Input
                inputMode="numeric"
                value={seed}
                onChange={(event) => setSeed(event.target.value)}
                placeholder="Leave empty to let the service choose"
              />
              <span className="text-muted-foreground block text-xs">
                Reuse this number and the same picture for a similar model.
              </span>
            </label>
            <label className="block space-y-2">
              <span className="text-sm font-medium">Background removal</span>
              <select
                className={selectClass}
                value={background}
                onChange={(event) =>
                  setBackground(event.target.value as MeshBackground)
                }
              >
                <option value="">Let the service choose</option>
                <option value="threshold">threshold</option>
                <option value="birefnet">birefnet</option>
              </select>
            </label>
          </div>
        </details>
        {fieldError && (
          <p role="alert" className="text-destructive text-sm">
            {fieldError}
          </p>
        )}
        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" className="min-h-11" disabled={running || unreachable || busy}>
            Make 3D model
          </Button>
        </div>
        {busy && !running && (
          <p role="status" className="text-muted-foreground text-sm">
            {MESH_BUSY_MESSAGE}
          </p>
        )}
        {unreachable && (
          <div role="alert" className="flex flex-wrap items-center gap-3 text-sm">
            <span>{MESH_UNREACHABLE_MESSAGE}</span>
            <Button type="button" className="min-h-11" onClick={() => void checkAgain()}>
              Check again
            </Button>
            <Button type="button" variant="outline" className="min-h-11" onClick={stopWaiting}>
              Stop waiting
            </Button>
          </div>
        )}
        {running && (
          <div
            role="status"
            className="flex flex-wrap items-center gap-3 text-sm"
          >
            <Loader2 className="size-4 animate-spin" aria-hidden="true" />
            <span>{cancelling ? MESH_CANCELLING_MESSAGE : MESH_WAIT_MESSAGE}</span>
            <span className="text-muted-foreground tabular-nums">
              Elapsed {meshElapsed(now - startedAt)}
            </span>
            {reconnecting && (
              <span className="text-muted-foreground">
                {MESH_RECONNECTING_MESSAGE}
              </span>
            )}
            <Button
              type="button"
              variant="outline"
              className="min-h-11"
              onClick={stopWaiting}
            >
              Stop waiting
            </Button>
            <Button
              type="button"
              variant="outline"
              className="min-h-11"
              disabled={cancelling}
              onClick={() => void cancel()}
            >
              Cancel
            </Button>
          </div>
        )}
      </form>
      <section
        aria-label="3D model result"
        className="bg-card min-w-0 space-y-3 rounded-xl border p-5"
      >
        {phase === "done" && result && active ? (
          <MeshViewer
            blob={result.blob}
            downloadName={result.downloadName}
            description={`3D model made from the picture ${result.picture}`}
          />
        ) : (
          <p className="text-muted-foreground text-sm">
            Your 3D model appears here.
          </p>
        )}
        {message && (
          <p role="alert" className="text-destructive text-sm">
            {message}
          </p>
        )}
        {phase === "error" && retry && file && (
          <div className="space-y-2">
            {retry.afterS !== null && (
              <p className="text-muted-foreground text-sm">
                {`The gateway suggests waiting about ${retry.afterS} seconds.`}
              </p>
            )}
            <Button type="button" className="min-h-11" disabled={busy} onClick={() => void generate()}>
              Try again
            </Button>
          </div>
        )}
      </section>
    </div>
  );
}
