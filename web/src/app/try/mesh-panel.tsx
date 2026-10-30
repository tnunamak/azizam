"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { isNetworkFailure, networkFetch } from "@/lib/network-error";
import {
  MESH_BUSY_MESSAGE,
  MESH_PATH,
  MESH_PICTURE_TYPES,
  MESH_WAIT_MESSAGE,
  meshDownloadName,
  meshFailureMessage,
  meshForm,
  meshSeedProblem,
  type MeshBackground,
  type MeshDetail,
} from "./mesh-mode";
import { MeshViewer } from "./mesh-viewer";
import { useUnsavedWork } from "@/lib/unsaved-work";

export type MeshModeProps = {
  /** The manifest's detail choices and the one selected first. */
  detail: { choices: MeshDetail[]; defaultValue: number | null };
  /** The Try key's secret, prepared on first use. Null when Try could not prepare one. */
  getSecret: () => Promise<string | null>;
  /** The GPU is busy or starting: the form stays, but a new picture cannot be sent yet. */
  busy: boolean;
  /** The Photo to 3D tab is open. The viewer mounts only then, so it is never sized while its tab is hidden. */
  active: boolean;
};

type Phase = "idle" | "running" | "done" | "error";
type Result = { blob: Blob; downloadName: string; picture: string };

/** A refusal from the gateway, carrying the server's own message. */
class MeshFailure extends Error {}

const selectClass =
  "border-input bg-card rounded-control min-h-11 w-full border px-3 text-base";

/**
 * The Photo to 3D panel: one picture in, one GLB out, shown in a viewer. It sends its own multipart request, because
 * the answer is a model, not text or a picture the generic Try result can show.
 */
export function MeshMode({ detail, getSecret, busy, active }: MeshModeProps) {
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
  const controller = useRef<AbortController | null>(null);

  useUnsavedWork({ kind: "try-waiting", active: phase === "running" });
  // A run in flight ends with the page: nothing is left running after navigation.
  useEffect(() => () => controller.current?.abort(), []);

  function cancel() {
    controller.current?.abort();
  }

  async function generate(event: FormEvent) {
    event.preventDefault();
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
    setResult(null);
    setPhase("running");
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
        headers: { authorization: `Bearer ${secret}` },
        body: meshForm({ image: file, seed, resolution, background }),
        signal: run.signal,
      });
      if (!response.ok) {
        const payload: unknown = await response.json().catch(() => null);
        throw new MeshFailure(meshFailureMessage(payload));
      }
      const blob = await response.blob();
      if (run.signal.aborted) return;
      setResult({ blob, downloadName: meshDownloadName(new Date()), picture });
      setPhase("done");
    } catch (error) {
      if (run.signal.aborted) {
        setPhase("idle");
        setMessage(
          "You stopped waiting. The server may still finish the model.",
        );
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

  const running = phase === "running";

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
        <label className="block space-y-2">
          <span className="text-sm font-medium">Picture</span>
          <input
            type="file"
            accept={MESH_PICTURE_TYPES.join(",")}
            className="block w-full text-sm"
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          />
          <span className="text-muted-foreground block text-xs">
            PNG, JPEG or WebP.
          </span>
        </label>
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
          <Button type="submit" className="min-h-11" disabled={running || busy}>
            Make 3D model
          </Button>
        </div>
        {busy && !running && (
          <p role="status" className="text-muted-foreground text-sm">
            {MESH_BUSY_MESSAGE}
          </p>
        )}
        {running && (
          <div
            role="status"
            className="flex flex-wrap items-center gap-3 text-sm"
          >
            <Loader2 className="size-4 animate-spin" aria-hidden="true" />
            <span>{MESH_WAIT_MESSAGE}</span>
            <Button
              type="button"
              variant="outline"
              className="min-h-11"
              onClick={cancel}
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
      </section>
    </div>
  );
}
