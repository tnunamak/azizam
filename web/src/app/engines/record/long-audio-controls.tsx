"use client";

import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { adminFetch } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { CopyButton } from "@/components/ui/copy-button";

interface Settings {
  target_seconds: number;
  search_seconds: number;
  max_segment_seconds: number;
  native_long_audio: boolean;
}
interface Estimate {
  excess_pp: number;
  low_pp: number;
  high_pp: number;
  points: number;
}
interface Job {
  id: string;
  status: string;
  phase: string;
  completed: number;
  total: number;
  download_completed?: number;
  download_total?: number;
  report: null | {
    deletions_by_length: {
      length: number;
      pieces: number;
      words: number;
      deletion_pct: number | null;
      failed: boolean;
    }[];
    seam_excess: (Estimate & { search: number; noise: Estimate })[];
    recommendation: (Settings & { reasons: string[] }) | null;
    note: string;
    recommendation_note?: string;
  };
}
interface Options {
  revision: string;
  settings: Settings;
  settings_note?: string;
  policy: {
    sample_ids: string[];
    lengths: number[];
    search_widths: number[];
    deadline_seconds: number;
    max_download_bytes: number;
  };
  samples: {
    id: string;
    title: string;
    speakers: number;
    timing: string;
    licence: string;
    download_bytes: number;
  }[];
  job: Job | null;
  commands: Record<string, string>;
}
const terminal = new Set(["succeeded", "failed", "cancelled", "interrupted"]);
const fields = [
  ["target_seconds", "Distance between cuts (seconds)"],
  ["search_seconds", "Search either side of each cut (seconds)"],
  ["max_segment_seconds", "Longest piece (seconds)"],
] as const;

export function LongAudioControls({
  serviceId,
  locked = false,
}: {
  serviceId: string;
  locked?: boolean;
}) {
  const base = `/admin/api/engines/${encodeURIComponent(serviceId)}`;
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["sttMeasurement", serviceId],
    queryFn: () => adminFetch<Options>(`${base}/stt-measurements`),
    refetchInterval: (query) => {
      const job = query.state.data?.job;
      return job && !terminal.has(job.status) ? 1000 : false;
    },
  });
  const [draft, setDraft] = useState<Partial<Settings> | null>(null);
  const [samples, setSamples] = useState<string[] | null>(null);
  const [lengths, setLengths] = useState<string | null>(null);
  const [widths, setWidths] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");
  const [jobCommands, setJobCommands] = useState<Record<string, string> | null>(
    null,
  );
  useEffect(() => {
    setDraft(null);
    setSamples(null);
    setLengths(null);
    setWidths(null);
    setSaved("");
  }, [serviceId]);
  const options = query.data;
  const job = options?.job;
  const running = !!job && !terminal.has(job.status);
  const settings = options ? { ...options.settings, ...draft } : undefined;
  const settingChanges = Object.fromEntries(
    Object.entries(draft ?? {}).filter(
      ([key, value]) => value !== options?.settings[key as keyof Settings],
    ),
  );
  const hasSettingChanges = Object.keys(settingChanges).length > 0;

  async function action(
    path: string,
    method: "PATCH" | "POST",
    body: unknown,
    registry = false,
  ) {
    setBusy(true);
    setError("");
    setSaved("");
    try {
      await adminFetch(`${base}${path}`, {
        method,
        body: JSON.stringify(body),
        headers: registry ? { "If-Match": options!.revision } : undefined,
      });
      setDraft(null);
      await Promise.all([
        query.refetch(),
        client.invalidateQueries({ queryKey: ["engineView", serviceId] }),
        client.invalidateQueries({ queryKey: ["serviceRegistry"] }),
      ]);
      setSaved(registry ? "Long-audio settings saved." : "");
    } catch (error) {
      setError(
        error instanceof Error
          ? error.message
          : "The request failed. Refresh and try again.",
      );
    } finally {
      setBusy(false);
    }
  }
  useEffect(() => {
    if (!job?.id) return;
    let alive = true;
    void adminFetch<{ commands: Record<string, string> }>(
      `${base}/stt-measurements/${job.id}`,
    )
      .then((result) => {
        if (alive) setJobCommands(result.commands);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [base, job?.id, job?.status]);

  return (
    <section
      aria-labelledby="long-audio-controls"
      className="flex flex-col gap-4 border-t p-4"
    >
      <h3 id="long-audio-controls" className="text-heading">
        Long-audio settings
      </h3>
      {query.isPending && <p role="status">Loading long-audio settings…</p>}
      {query.error && <p role="alert">{query.error.message}</p>}
      {options && settings && (
        <>
          {options.settings_note && (
            <p className="text-meta text-muted-foreground">
              {options.settings_note}
            </p>
          )}
          <fieldset
            disabled={locked || busy || running}
            className="flex flex-col gap-3"
          >
            {fields.map(([key, label]) => (
              <label key={key} className="text-meta flex flex-col gap-1">
                {label}
                <Input
                  type="number"
                  min={key === "search_seconds" ? "0" : "0.001"}
                  step="any"
                  value={settings[key]}
                  onChange={(event) =>
                    setDraft({ ...draft, [key]: Number(event.target.value) })
                  }
                />
              </label>
            ))}
            <label className="text-meta flex items-center gap-2">
              <input
                type="checkbox"
                checked={settings.native_long_audio}
                onChange={(event) =>
                  setDraft({
                    ...draft,
                    native_long_audio: event.target.checked,
                  })
                }
              />
              Engine handles long audio natively
            </label>
            <p className="text-meta text-muted-foreground">
              Save these values directly, or measure public speech to get a
              recommendation. Saving does not run a measurement.
            </p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={!hasSettingChanges}
              onClick={() =>
                void action("/stt-settings", "PATCH", settingChanges, true)
              }
            >
              Save long-audio settings
            </Button>
          </fieldset>
          <details>
            <summary className="text-link cursor-pointer">
              Measurement samples and trials
            </summary>
            <fieldset
              disabled={busy || running || locked}
              className="flex flex-col gap-3 pt-3"
            >
              {options.samples.map((sample) => (
                <label
                  key={sample.id}
                  className="text-meta flex items-start gap-2"
                >
                  <input
                    type="checkbox"
                    checked={(samples ?? options.policy.sample_ids).includes(
                      sample.id,
                    )}
                    onChange={(event) => {
                      const chosen = samples ?? options.policy.sample_ids;
                      setSamples(
                        event.target.checked
                          ? [...chosen, sample.id]
                          : chosen.filter((id) => id !== sample.id),
                      );
                    }}
                  />
                  <span>
                    {sample.title} · {sample.speakers} speakers ·{" "}
                    {sample.licence} ·{" "}
                    {Math.ceil(sample.download_bytes / 1048576)} MB ·{" "}
                    {sample.timing} timing
                  </span>
                </label>
              ))}
              <label className="text-meta">
                Piece lengths, seconds (comma-separated)
                <Input
                  value={lengths ?? options.policy.lengths.join(", ")}
                  onChange={(event) => setLengths(event.target.value)}
                />
              </label>
              <label className="text-meta">
                Search widths, seconds (comma-separated)
                <Input
                  value={widths ?? options.policy.search_widths.join(", ")}
                  onChange={(event) => setWidths(event.target.value)}
                />
              </label>
            </fieldset>
          </details>
          <p className="text-meta text-muted-foreground">
            Downloads checksummed public audio on demand, then transcribes
            through this gateway. Checks declared GPU memory needs, current GPU
            load and the shared media queue before each request. This check does
            not reserve the GPU. Stops after{" "}
            {Math.ceil(options.policy.deadline_seconds / 60)} minutes; download
            limit {Math.round(options.policy.max_download_bytes / 1048576)} MB.
            Settings stay unchanged until you apply a recommendation.
          </p>
          <div className="flex flex-wrap gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={busy || running || locked || hasSettingChanges}
              onClick={() =>
                void action("/stt-measurements", "POST", {
                  sample_ids: samples ?? options.policy.sample_ids,
                  lengths:
                    lengths === null
                      ? options.policy.lengths
                      : lengths.split(",").map(Number),
                  search_widths:
                    widths === null
                      ? options.policy.search_widths
                      : widths.split(",").map(Number),
                })
              }
            >
              Measure long-audio settings
            </Button>
            {running && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={busy}
                onClick={() =>
                  void action(`/stt-measurements/${job.id}/cancel`, "POST", {})
                }
              >
                Cancel measurement
              </Button>
            )}
          </div>
          {job && (
            <p role="status" className="text-meta">
              {job.phase} · {job.completed}/{job.total} transcriptions
              {!!job.download_total &&
                ` · ${Math.round((100 * (job.download_completed ?? 0)) / job.download_total)}% downloaded`}
            </p>
          )}
          {job?.report && (
            <div className="flex flex-col gap-3">
              <h4 className="font-medium">Deletions by piece length</h4>
              <div className="overflow-x-auto">
                <table className="text-meta w-full text-left">
                  <thead>
                    <tr>
                      <th>Seconds</th>
                      <th>Pieces</th>
                      <th>Reference words</th>
                      <th>Deleted</th>
                    </tr>
                  </thead>
                  <tbody>
                    {job.report.deletions_by_length.map((row) => (
                      <tr key={row.length}>
                        <td>{row.length}</td>
                        <td>{row.pieces}</td>
                        <td>{row.words}</td>
                        <td>
                          {row.failed
                            ? "Engine refused"
                            : row.deletion_pct === null
                              ? "No evidence"
                              : `${row.deletion_pct}%`}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <h4 className="font-medium">Excess seam WER versus noise</h4>
              <p className="text-meta">
                Percentage points, with 95% intervals. No paired places means no
                estimate.
              </p>
              <div className="overflow-x-auto">
                <table className="text-meta w-full text-left">
                  <thead>
                    <tr>
                      <th>Search (s)</th>
                      <th>Seam excess [95%]</th>
                      <th>Noise [95%]</th>
                    </tr>
                  </thead>
                  <tbody>
                    {job.report.seam_excess.map((row) => (
                      <tr key={row.search}>
                        <td>{row.search}</td>
                        <td>{estimate(row)}</td>
                        <td>{estimate(row.noise)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="text-meta text-muted-foreground">
                {job.report.note}
              </p>
              {job.report.recommendation ? (
                <>
                  <p className="text-meta">
                    Recommended: target{" "}
                    {job.report.recommendation.target_seconds} s, search ±
                    {job.report.recommendation.search_seconds} s, maximum{" "}
                    {job.report.recommendation.max_segment_seconds} s.
                  </p>
                  <ul className="text-meta list-disc pl-5">
                    {job.report.recommendation.reasons.map((reason) => (
                      <li key={reason}>{reason}</li>
                    ))}
                  </ul>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    disabled={
                      locked ||
                      busy ||
                      hasSettingChanges ||
                      job.status !== "succeeded"
                    }
                    onClick={() =>
                      void action(
                        `/stt-measurements/${job.id}/apply`,
                        "POST",
                        {},
                        true,
                      )
                    }
                  >
                    Apply recommended settings
                  </Button>
                </>
              ) : (
                <p className="text-meta">
                  {job.report.recommendation_note ??
                    "Not enough paired speech evidence to recommend settings. Choose longer samples or smaller trial lengths."}
                </p>
              )}
            </div>
          )}
          <details>
            <summary className="text-link cursor-pointer">
              Owner REST commands
            </summary>
            <p className="text-meta">
              Set AZIZAM_BASE and AZIZAM_OWNER_KEY in your shell. The configure
              command contains current settings; edit its values before running
              it.
            </p>
            {Object.entries({ ...options.commands, ...jobCommands }).map(
              ([name, command]) => (
                <div key={name} className="py-2">
                  <span className="text-meta">{name}</span>
                  <CopyButton
                    value={command}
                    aria-label={`Copy ${name} command`}
                  />
                  <pre className="text-meta overflow-x-auto break-all whitespace-pre-wrap">
                    {command}
                  </pre>
                </div>
              ),
            )}
          </details>
        </>
      )}
      {error && (
        <p role="alert" className="text-meta text-danger">
          {error}
        </p>
      )}
      {saved && (
        <p role="status" className="text-meta">
          {saved}
        </p>
      )}
    </section>
  );
}
function estimate(row: Estimate) {
  return row.points
    ? `${row.excess_pp} [${row.low_pp}, ${row.high_pp}] (${row.points} places)`
    : "No evidence";
}
