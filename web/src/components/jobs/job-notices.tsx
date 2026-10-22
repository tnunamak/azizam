"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Check, CircleAlert, X } from "lucide-react";
import { useViewer } from "@/components/shell/session";
import { networkFetch } from "@/lib/network-error";
import { CREATION_OPENED, openedCreations } from "./opened-creations";

export type LongJob = {
  job_id: string;
  intent: string;
  status: string;
  cancel_requested?: boolean;
  created_at: number;
  updated_at: number;
  error?: string;
  assets: { asset_id?: string; id?: string }[];
};

// A mounted result owns announcements for its job; the shell handles the rest.
const activeResults = new Map<string, number>();
const resultKey = (personId: string | undefined | null, jobId: string) =>
  JSON.stringify([personId, jobId]);

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

export function jobAssetId(job: LongJob) {
  return job.assets.map((asset) => asset.asset_id || asset.id).find(Boolean);
}

export function jobLink(job: LongJob) {
  const assetId = job.status === "succeeded" && jobAssetId(job);
  return assetId
    ? `/library/?asset=${encodeURIComponent(assetId)}`
    : `/library/?job=${encodeURIComponent(job.job_id)}`;
}

export async function jobRequest<T>(
  path: string,
  signal?: AbortSignal,
): Promise<T> {
  const response = await networkFetch(path, {
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
  const activeJobs = useRef(new Map<string, LongJob>());
  const since = useRef<number | null>(null);
  const unchanged = useRef(0);
  const failures = useRef(0);
  const noticesRef = useRef<HTMLDivElement>(null);
  const [notices, setNotices] = useState<LongJob[]>([]);
  const storageKey = `azizam:job-notices:v1:${personId}`;
  const dismissed = useRef(new Set<string>());
  const opened = useRef(new Set<string>());
  useEffect(() => {
    opened.current = openedCreations(personId);
    const acknowledge = (event: Event) => {
      const detail = (event as CustomEvent<{ personId: string; assetId: string }>).detail;
      if (detail.personId !== personId) return;
      opened.current.add(detail.assetId);
      setNotices((current) => current.filter((job) => !job.assets.some((asset) => (asset.asset_id || asset.id) === detail.assetId)));
    };
    window.addEventListener(CREATION_OPENED, acknowledge);
    try {
      dismissed.current = new Set(
        JSON.parse(sessionStorage.getItem(storageKey) ?? "[]"),
      );
    } catch {
      /* A disabled store does not disable in-app notices. */
    }
    return () => window.removeEventListener(CREATION_OPENED, acknowledge);
  }, [storageKey, personId]);
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
        (previous !== undefined ||
          job.created_at >= startedAt ||
          job.updated_at >= startedAt) &&
        !activeResults.has(resultKey(personId, job.job_id)) &&
        !dismissed.current.has(job.job_id) &&
        !job.assets.some((asset) => opened.current.has(asset.asset_id || asset.id || ""))
      ) {
        completed.push(job);
      }
      seen.current.set(job.job_id, job.status);
      if (terminalJob(job)) activeJobs.current.delete(job.job_id);
      else activeJobs.current.set(job.job_id, job);
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
        let watermark: number | null = null;
        do {
          const params = new URLSearchParams({ watch: "1" });
          if (since.current !== null)
            params.set("since", String(since.current));
          if (cursor) params.set("cursor", cursor);
          const page: {
            jobs: LongJob[];
            next_cursor?: string | null;
            polled_at?: number;
          } = await jobRequest(`/me/api/jobs?${params}`, signal);
          watermark ??= page.polled_at ?? null;
          jobs.push(...page.jobs);
          cursor = page.next_cursor ?? null;
        } while (cursor);
        failures.current = 0;
        recordPoll(jobs);
        // Advance only after all pages succeed. Inclusive seconds replay ties.
        since.current = watermark;
        return [...activeJobs.current.values()];
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
  function dismiss(ids: string[], restoreFocus = false) {
    const region = noticesRef.current;
    const sections = region ? Array.from(region.children) : [];
    const index = sections.findIndex((section) => ids.includes(section.getAttribute("data-job-id") ?? ""));
    if (restoreFocus && index >= 0 && sections[index].contains(document.activeElement)) {
      const successor = sections[index + 1] ?? sections[index - 1];
      (successor?.querySelector<HTMLElement>("a, button") ?? region)?.focus();
    }
    ids.forEach((id) => dismissed.current.add(id));
    setNotices((current) => current.filter((job) => !ids.includes(job.job_id)));
    try {
      sessionStorage.setItem(
        storageKey,
        JSON.stringify([...dismissed.current].slice(-200)),
      );
    } catch {
      /* Optional storage. */
    }
  }
  // Keep overflow groups queued: their eight-second lifetime starts only when
  // they become visible. Replayed polls never add a second copy of a job.
  const groups = new Map<string, LongJob[]>();
  for (const job of notices) {
    const key = `${jobLabel(job)}:${job.status}`;
    const group = groups.get(key) ?? [];
    group.push(job);
    groups.set(key, group);
  }
  return (
    <div
      ref={noticesRef}
      tabIndex={-1}
      role="region"
      aria-live="polite"
      aria-relevant="additions text"
      aria-label="Job updates"
      className="pointer-events-none fixed inset-x-3 bottom-[calc(5rem+env(safe-area-inset-bottom))] z-50 flex max-h-[calc(100dvh-7rem)] flex-col gap-3 overflow-y-auto sm:inset-x-auto sm:right-4 sm:bottom-4 sm:w-96"
    >
      {[...groups].slice(0, 3).map(([key, jobs]) => (
        <JobNotice
          key={key}
          job={jobs[jobs.length - 1]}
          jobs={jobs}
          onOpen={(id) => dismiss([id])}
          onDismiss={(restoreFocus) => dismiss(jobs.map((job) => job.job_id), restoreFocus)}
        />
      ))}
    </div>
  );
}

const TOAST_DURATION_MS = 8_000;

export function JobNotice({
  job,
  jobs = [job],
  onOpen,
  onDismiss,
}: {
  job: LongJob;
  jobs?: LongJob[];
  onOpen?: (jobId: string) => void;
  onDismiss: (restoreFocus?: boolean) => void;
}) {
  const done = job.status === "succeeded";
  const Icon = done ? Check : CircleAlert;
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const remaining = useRef(TOAST_DURATION_MS);
  const identity = JSON.stringify(jobs.map((item) => item.job_id));
  const previousIdentity = useRef(identity);
  const dismissRef = useRef(onDismiss);
  useEffect(() => {
    dismissRef.current = onDismiss;
  }, [onDismiss]);
  useEffect(() => {
    // A new completion in a group gets a fresh reading interval. Hover and
    // keyboard focus independently pause the remaining time, like Radix Toast.
    if (previousIdentity.current !== identity) {
      remaining.current = TOAST_DURATION_MS;
      previousIdentity.current = identity;
    }
    if (hovered || focused) return;
    const started = Date.now();
    const timer = window.setTimeout(() => dismissRef.current(), remaining.current);
    return () => {
      window.clearTimeout(timer);
      remaining.current = Math.max(0, remaining.current - (Date.now() - started));
    };
  }, [identity, hovered, focused]);
  const label = jobLabel(job);
  const plural = ({
    Picture: "pictures",
    Video: "videos",
    Audio: "audio creations",
    Music: "music creations",
    Sound: "sounds",
    Creation: "creations",
  } as Record<string, string>)[label];
  const message = jobs.length === 1
    ? `${label} ${done ? "is ready" : "couldn’t finish"}`
    : `${jobs.length} ${plural} ${done ? "are ready" : "couldn’t finish"}`;
  const open = (id: string) => onOpen ? onOpen(id) : onDismiss();
  return (
    <section
      className="pointer-events-auto border-border bg-card rounded-panel flex flex-wrap items-center gap-3 border p-3 shadow-lg"
      data-job-id={job.job_id}
      aria-label={jobs.length === 1 ? `${label} ${done ? "ready" : "failed"}` : message}
      aria-atomic="true"
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onFocusCapture={() => setFocused(true)}
      onBlurCapture={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null))
          setFocused(false);
      }}
    >
      <Icon
        aria-hidden="true"
        className={
          done ? "text-success size-5 shrink-0" : "text-danger size-5 shrink-0"
        }
      />
      <p className="min-w-0 flex-1 break-words">{message}</p>
      <Link prefetch={false}
        className="text-link inline-flex min-h-11 items-center rounded-md px-3 underline underline-offset-4"
        href={jobLink(job)}
        onClick={() => open(job.job_id)}
      >
        {done ? "Open result" : "See details"}
      </Link>
      <button
        type="button"
        className="hover:bg-muted inline-flex min-h-11 min-w-11 items-center justify-center rounded-md"
        aria-label={`Dismiss ${label.toLowerCase()} update`}
        onClick={() => onDismiss(true)}
      >
        <X aria-hidden="true" className="size-5" />
      </button>
      {jobs.length > 1 && (
        <details className="w-full">
          <summary className="text-link min-h-11 cursor-pointer rounded-md px-3 py-3 underline underline-offset-4">
            View all {jobs.length} {done ? "results" : "jobs"}
          </summary>
          <ul className="max-h-40 overflow-y-auto">
            {jobs.map((item, index) => (
              <li key={item.job_id}>
                <Link prefetch={false}
                  className="text-link flex min-h-11 items-center rounded-md px-3 underline underline-offset-4"
                  href={jobLink(item)}
                  onClick={() => open(item.job_id)}
                >
                  {done ? "Open result" : "See details"} {index + 1}
                </Link>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

export function JobResult({ jobId }: { jobId: string }) {
  const viewer = useViewer();
  useEffect(() => {
    const key = resultKey(viewer.id, jobId);
    activeResults.set(key, (activeResults.get(key) ?? 0) + 1);
    return () => {
      const count = (activeResults.get(key) ?? 1) - 1;
      if (count) activeResults.set(key, count);
      else activeResults.delete(key);
    };
  }, [viewer.id, jobId]);
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
  const job = data?.job;
  const heading = job
    ? `${jobLabel(job)} ${job.cancel_requested
        ? "cancel requested"
        : ({
            succeeded: "is ready",
            failed: "couldn’t finish",
            canceled: "canceled",
            queued: "is waiting",
            running: "is being made",
          } as Record<string, string>)[job.status] ?? job.status}`
    : "";
  const status = isError
    ? "Couldn’t load this job."
    : job
      ? [heading, job.error].filter(Boolean).join(". ")
      : "Checking your job…";
  return (
    <>
      <p role="status" aria-live="polite" aria-atomic="true" className={job && !isError ? "sr-only" : undefined}>
        {status}
      </p>
      {isError ? (
        <button
          className="text-link min-h-11 min-w-11 px-3 underline"
          onClick={() => void refetch()}
        >
          Try again
        </button>
      ) : job ? (
        <section
          className="border-border mb-6 border-b pb-4"
          aria-label="Job result"
        >
          <h2 className="text-lg font-semibold">{heading}</h2>
          {job.error ? <p>{job.error}</p> : null}
          {job.status === "succeeded" && jobAssetId(job) ? (
            <Link prefetch={false}
              className="text-link inline-flex min-h-11 min-w-11 items-center underline"
              href={jobLink(job)}
            >
              Open result
            </Link>
          ) : (
            <Link prefetch={false}
              className="text-link inline-flex min-h-11 min-w-11 items-center underline"
              href="/try/"
            >
              Back to Try
            </Link>
          )}
        </section>
      ) : null}
    </>
  );
}
