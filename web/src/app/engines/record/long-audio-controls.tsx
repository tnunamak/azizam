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
  native_long_audio_offline: boolean;
  native_long_audio_streaming: boolean;
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
  measurement_ready?: boolean;
  known_limit?: boolean;
  trial_lengths?: number[];
  trial_lengths_note?: string | null;
  settings: Settings;
  settings_note?: string;
  policy: {
    sample_ids: string[];
    lengths: number[];
    min_trial_seconds?: number;
    max_trial_seconds?: number;
    recommendation?: { target_margin: number };
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
    rights_unstated: boolean;
    rights_note: string;
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
  return <LongAudioControlsForService key={serviceId} serviceId={serviceId} locked={locked} />;
}

function LongAudioControlsForService({ serviceId, locked }: { serviceId: string; locked: boolean }) {
  const base = `/admin/api/engines/${encodeURIComponent(serviceId)}`;
  const client = useQueryClient();
  const { data: options, isPending, error: queryError, refetch } = useQuery({
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
  const job = options?.job;
  const measurementUnavailable = options?.measurement_ready === false;
  const noHardLimit = options?.known_limit === false
    && !(options.settings.native_long_audio_offline && options.settings.native_long_audio_streaming);
  const running = !!job && !terminal.has(job.status);
  const settings = options ? { ...options.settings, ...draft } : undefined;
  const settingChanges = Object.fromEntries(
    Object.entries(draft ?? {}).filter(
      ([key, value]) => value !== options?.settings[key as keyof Settings],
    ),
  );
  const hasSettingChanges = Object.keys(settingChanges).length > 0;
  const settingsValid = !!settings
    && Number.isFinite(settings.target_seconds)
    && settings.target_seconds > 0
    && Number.isFinite(settings.search_seconds)
    && settings.search_seconds >= 0
    && settings.search_seconds <= settings.target_seconds / 2
    && Number.isFinite(settings.max_segment_seconds)
    && settings.max_segment_seconds >= settings.target_seconds;
  const safeTrialSeconds = settings?.max_segment_seconds ?? 0;
  const defaultLengths = options?.trial_lengths ?? [];
  const trialLengthValues = lengths === null
    ? defaultLengths
    : lengths.split(",").map((value) => Number(value.trim()));
  const trialLengthsValid = !!options && trialLengthValues.length >= 2
    && new Set(trialLengthValues).size === trialLengthValues.length
    && trialLengthValues.every((length) => Number.isFinite(length)
      && length >= (options.policy.min_trial_seconds ?? 1)
      && length <= (options.policy.max_trial_seconds ?? 120)
      && length <= safeTrialSeconds);
  const conservativeTrialTarget = trialLengthValues.length > 0
    && trialLengthValues.every(Number.isFinite)
    ? Math.max(1, Math.floor(Math.min(...trialLengthValues)
      * (options?.policy.recommendation?.target_margin ?? 0.75)))
    : settings?.target_seconds ?? 0;
  const maxTrialSearchWidth = Math.min(
    options?.policy.max_trial_seconds ?? 120,
    (settings?.target_seconds ?? 0) / 2,
    conservativeTrialTarget / 2,
  );
  const searchWidthValues = widths === null
    ? options?.policy.search_widths ?? []
    : widths.split(",").map((value) => Number(value.trim()));
  const searchWidthsValid = !!settings && searchWidthValues.length >= 2
    && new Set(searchWidthValues).size === searchWidthValues.length
    && searchWidthValues.every((width) => Number.isFinite(width)
      && width >= 0
      && width <= maxTrialSearchWidth
      && width <= (options?.policy.max_trial_seconds ?? 120));

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
        refetch(),
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
      {isPending && <p role="status">Loading long-audio settings…</p>}
      {queryError && <p role="alert">{queryError.message}</p>}
      {options && settings && (
        <>
          {options.settings_note && (
            <p className="text-meta text-muted-foreground">
              {options.settings_note}
            </p>
          )}
          {hasSettingChanges && !settingsValid && (
            <p role="alert" className="text-meta text-destructive">
              The hard maximum must be at least the target. Search width cannot exceed half the target.
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
                  max={key === "search_seconds"
                    ? settings.target_seconds / 2
                    : key === "target_seconds"
                      ? settings.max_segment_seconds
                      : undefined}
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
                checked={settings.native_long_audio_offline}
                onChange={(event) => setDraft({ ...draft, native_long_audio_offline: event.target.checked })}
              />
              Offline mode uses native long audio
            </label>
            <label className="text-meta flex items-center gap-2">
              <input
                type="checkbox"
                checked={settings.native_long_audio_streaming}
                onChange={(event) => setDraft({ ...draft, native_long_audio_streaming: event.target.checked })}
              />
              Streaming mode uses native long audio
            </label>
            <p className="text-meta text-muted-foreground">
              Save these values directly, or measure public speech to get a
              recommendation. Saving does not run a measurement.
            </p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={!hasSettingChanges || !settingsValid}
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
                    {sample.rights_unstated && (
                      <span className="block font-medium">
                        Sample rights not stated. {sample.rights_note}
                      </span>
                    )}
                  </span>
                </label>
              ))}
              <label className="text-meta">
                Piece lengths, seconds (comma-separated)
                <Input
                  value={lengths ?? defaultLengths.join(", ")}
                  aria-label="Piece lengths, seconds (comma-separated)"
                  aria-describedby="stt-measurement-length-limit"
                  onChange={(event) => setLengths(event.target.value)}
                />
                <span id="stt-measurement-length-limit" className="text-meta text-muted-foreground">
                  Trials cannot exceed the current planner&apos;s safe piece limit of{" "}
                  {safeTrialSeconds} seconds.
                </span>
                {options.trial_lengths_note && (
                  <span className="text-meta text-muted-foreground">
                    {options.trial_lengths_note}
                  </span>
                )}
                {!trialLengthsValid && (
                  <span role="alert" className="text-meta text-destructive">
                    Choose at least two distinct trial lengths within the current safe piece limit.
                  </span>
                )}
              </label>
              <label className="text-meta">
                Search widths, seconds (comma-separated)
                <Input
                  value={widths ?? options.policy.search_widths.join(", ")}
                  aria-label="Search widths, seconds (comma-separated)"
                  onChange={(event) => setWidths(event.target.value)}
                />
                <span className="text-meta text-muted-foreground">
                  Search width is capped at half the conservative trial target
                  ({conservativeTrialTarget} seconds), derived from the shortest
                  trial length and recommendation margin. Maximum width: {maxTrialSearchWidth} seconds.
                </span>
                {!searchWidthsValid && (
                  <span role="alert" className="text-meta text-destructive">
                    Choose at least two distinct search widths within the planner limit.
                  </span>
                )}
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
          {options.samples
            .filter((sample) => sample.rights_unstated
              && (samples ?? options.policy.sample_ids).includes(sample.id))
            .map((sample) => (
              <p key={sample.id} role="note" className="text-meta font-medium">
                Sample rights not stated for {sample.title}. {sample.rights_note}
              </p>
            ))}
          <div className="flex flex-wrap gap-2">
            {noHardLimit && (
              <p id="stt-no-hard-limit" className="text-meta text-muted-foreground w-full">
                No hard maximum is recorded for this engine. Long files that cannot be cut are sent whole and may fail at the engine. Measure long-audio settings to set a limit.
              </p>
            )}
            {measurementUnavailable && (
              <p id="stt-measurement-unavailable" className="text-meta text-muted-foreground w-full">
                Measurement is unavailable. Say whether the engine needs a graphics processor (GPU) and, if so, how much memory it needs before measuring.
              </p>
            )}
            <Button
              type="button"
              variant="outline"
              size="sm"
              aria-describedby={[
                noHardLimit && "stt-no-hard-limit",
                measurementUnavailable && "stt-measurement-unavailable",
              ].filter(Boolean).join(" ") || undefined}
              disabled={measurementUnavailable || busy || running || locked || hasSettingChanges || !settingsValid || !trialLengthsValid || !searchWidthsValid}
              onClick={() =>
                void action("/stt-measurements", "POST", {
                  sample_ids: samples ?? options.policy.sample_ids,
                  lengths: trialLengthValues,
                  search_widths: searchWidthValues,
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
