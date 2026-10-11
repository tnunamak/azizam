"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Check, CircleAlert, X } from "lucide-react";
import { useViewer } from "@/components/shell/session";

export type LongJob = {
  job_id: string;
  intent: string;
  status: string;
  cancel_requested?: boolean;
  created_at: number;
  updated_at: number;
  error?: string;
  assets: { asset_id?: string }[];
};

export const terminalJob = (job: LongJob) =>
  ["succeeded", "failed", "canceled"].includes(job.status);

export function jobLabel(job: LongJob) {
  const kind = job.intent.split(".")[0];
  return (
    (
      {
        image: "Picture",
        video: "Video",
        audio: "Audio",
        music: "Music",
        sfx: "Sound",
      } as Record<string, string>
    )[kind] ?? "Creation"
  );
}

export function jobLink(job: LongJob) {
  const assetId = job.status === "succeeded" && job.assets[0]?.asset_id;
  return assetId
    ? `/library/?asset=${encodeURIComponent(assetId)}`
    : `/library/?job=${encodeURIComponent(job.job_id)}`;
}

export async function jobRequest<T>(
  path: string,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    cache: "no-store",
    signal,
  });
  if (!response.ok) throw new Error("Couldn’t check your jobs. Try again.");
  return response.json();
}

export function jobPollInterval(
  active: boolean,
  unchanged: number,
  failed: number,
) {
  if (failed) return Math.min(120_000, 60_000 * 2 ** Math.min(failed - 1, 1));
  return active
    ? Math.min(30_000, 5_000 * 2 ** Math.min(unchanged, 3))
    : 60_000;
}

/** One observer for the shell. IDs and states only; never prompts or signed URLs. */
export function JobNotices() {
  const viewer = useViewer();
  return viewer.authenticated && viewer.id ? (
    <PersonJobNotices key={viewer.id} personId={viewer.id} />
  ) : null;
}

function PersonJobNotices({ personId }: { personId: string }) {
  const [startedAt] = useState(() => Math.floor(Date.now() / 1000));
  const seen = useRef(new Map<string, string>());
  const unchanged = useRef(0);
  const failures = useRef(0);
  const [notices, setNotices] = useState<LongJob[]>([]);
  const storageKey = `azizam:job-notices:v1:${personId}`;
  const dismissed = useRef(new Set<string>());
  useEffect(() => {
    try {
      dismissed.current = new Set(
        JSON.parse(sessionStorage.getItem(storageKey) ?? "[]"),
      );
    } catch {
      /* A disabled store does not disable in-app notices. */
    }
  }, [storageKey]);
  // Finished jobs are events: record them when a poll arrives, not by
  // syncing state to the query result in an effect.
  function recordPoll(jobs: LongJob[]) {
    let changed = false;
    const completed: LongJob[] = [];
    for (const job of jobs) {
      const previous = seen.current.get(job.job_id);
      changed ||= previous !== job.status;
      if (
        terminalJob(job) &&
        job.status !== "canceled" &&
        previous !== job.status &&
        (previous !== undefined || job.created_at >= startedAt) &&
        !dismissed.current.has(job.job_id)
      ) {
        completed.push(job);
      }
      seen.current.set(job.job_id, job.status);
    }
    unchanged.current = changed ? 0 : unchanged.current + 1;
    if (completed.length)
      setNotices((current) => [
        ...current.filter(
          (job) => !completed.some((next) => next.job_id === job.job_id),
        ),
        ...completed,
      ]);
  }
  useQuery({
    queryKey: ["person-jobs", personId],
    queryFn: async ({ signal }) => {
      try {
        const jobs: LongJob[] = [];
        let cursor: string | null = null;
        do {
          const page: { jobs: LongJob[]; next_cursor?: string | null } =
            await jobRequest(
              `/me/api/jobs${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`,
              signal,
            );
          jobs.push(...page.jobs);
          cursor = page.next_cursor ?? null;
        } while (cursor);
        failures.current = 0;
        recordPoll(jobs);
        return jobs;
      } catch (error) {
        failures.current += 1;
        throw error;
      }
    },
    staleTime: 5_000,
    retry: false,
    refetchIntervalInBackground: false,
    refetchInterval: (query) =>
      jobPollInterval(
        Boolean(query.state.data?.some((job) => !terminalJob(job))),
        unchanged.current,
        failures.current,
      ),
  });
  function dismiss(id: string) {
    dismissed.current.add(id);
    setNotices((current) => current.filter((job) => job.job_id !== id));
    try {
      sessionStorage.setItem(
        storageKey,
        JSON.stringify([...dismissed.current].slice(-200)),
      );
    } catch {
      /* Optional storage. */
    }
  }
  return (
    <div
      role="region"
      aria-live="polite"
      aria-relevant="additions"
      aria-label="Job updates"
    >
      {notices.map((job) => (
        <JobNotice
          key={job.job_id}
          job={job}
          onDismiss={() => dismiss(job.job_id)}
        />
      ))}
    </div>
  );
}

export function JobNotice({
  job,
  onDismiss,
}: {
  job: LongJob;
  onDismiss: () => void;
}) {
  const done = job.status === "succeeded";
  const Icon = done ? Check : CircleAlert;
  return (
    <section
      className="border-border bg-card rounded-panel mb-3 flex flex-wrap items-center gap-3 border p-3"
      aria-label={`${jobLabel(job)} ${done ? "ready" : "failed"}`}
    >
      <Icon
        aria-hidden="true"
        className={
          done ? "text-success size-5 shrink-0" : "text-danger size-5 shrink-0"
        }
      />
      <p className="min-w-0 flex-1">
        {jobLabel(job)} {done ? "is ready" : "couldn’t finish"}
      </p>
      <Link
        className="text-link inline-flex min-h-11 items-center rounded-md px-3 underline underline-offset-4"
        href={jobLink(job)}
      >
        {done ? "Open result" : "See details"}
      </Link>
      <button
        type="button"
        className="hover:bg-muted inline-flex min-h-11 min-w-11 items-center justify-center rounded-md"
        aria-label={`Dismiss ${jobLabel(job).toLowerCase()} update`}
        onClick={onDismiss}
      >
        <X aria-hidden="true" className="size-5" />
      </button>
    </section>
  );
}

export function JobResult({ jobId }: { jobId: string }) {
  const viewer = useViewer();
  const { data, isError, refetch } = useQuery({
    queryKey: ["person-job", viewer.id, jobId],
    queryFn: ({ signal }) =>
      jobRequest<{ job: LongJob }>(
        `/me/api/jobs/${encodeURIComponent(jobId)}`,
        signal,
      ),
    retry: false,
    refetchInterval: (query) =>
      query.state.data && !terminalJob(query.state.data.job) ? 30_000 : false,
  });
  if (isError)
    return (
      <p role="status">
        Couldn’t load this job.{" "}
        <button
          className="text-link min-h-11 px-3 underline"
          onClick={() => void refetch()}
        >
          Try again
        </button>
      </p>
    );
  if (!data) return <p role="status">Checking your job…</p>;
  const job = data.job;
  return (
    <section
      className="border-border mb-6 border-b pb-4"
      aria-label="Job result"
    >
      <h2 className="text-lg font-semibold">
        {jobLabel(job)}{" "}
        {job.cancel_requested
          ? "cancel requested"
          : (
              {
                succeeded: "is ready",
                failed: "couldn’t finish",
                canceled: "canceled",
                queued: "is waiting",
                running: "is being made",
              } as Record<string, string>
            )[job.status]}
      </h2>
      {job.error ? <p>{job.error}</p> : null}
      {job.assets[0]?.asset_id ? (
        <Link
          className="text-link inline-flex min-h-11 items-center underline"
          href={jobLink(job)}
        >
          Open result
        </Link>
      ) : (
        <Link
          className="text-link inline-flex min-h-11 items-center underline"
          href="/try/"
        >
          Back to Try
        </Link>
      )}
    </section>
  );
}
