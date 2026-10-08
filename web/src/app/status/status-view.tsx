"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useViewer } from "@/components/shell/session";
import { Button } from "@/components/ui/button";
import { RelativeTime } from "@/components/ui/relative-time";
import { Skeleton } from "@/components/ui/skeleton";
import { RefreshCcw } from "lucide-react";
import { ErrorState, EmptyState } from "@/components/product";
import {
  availabilityState,
  capabilityMeta,
  evidenceStamp,
  EvidenceTime,
  SectionHeading,
  StateLabel,
  Verdict,
  type EvidenceStamp,
} from "@/components/product/readiness";
import { ApiError } from "@/lib/api";
import {
  productApi,
  type MemberStatusCapability,
  type MemberStatusReceipt,
} from "@/lib/product-api";
import { availabilityReason } from "@/lib/status-copy";
import { cn } from "@/lib/cn";
import { homeVerdict, spendingNotice } from "@/app/home/presentation";
import { Notice } from "@/components/product/notice";
import type { HomeCapability } from "@/app/home/home-api";
import { StatusClient } from "./status-client";

/** Request intents map to the capability a person recognises. */
function intentCapability(intent: string | null | undefined): string | null {
  const value = String(intent ?? "").toLowerCase();
  if (!value) return null;
  if (/^(chat|responses|text\.completions)/.test(value)) return "chat";
  if (value.startsWith("image")) return "image";
  if (value.startsWith("video")) return "video";
  if (/transcri/.test(value)) return "transcribe";
  if (/^(speech|audio\.speech)/.test(value)) return "speech";
  if (/sfx/.test(value)) return "sfx";
  if (/music/.test(value)) return "music";
  if (value.startsWith("audio")) return "audio";
  if (/embed/.test(value)) return "embed";
  if (/rerank/.test(value)) return "rerank";
  if (/decision|systemone/.test(value)) return "decide";
  return null;
}

function failed(receipt: MemberStatusReceipt): boolean {
  return ["failed", "error", "rejected"].includes(
    String(receipt.status ?? "").toLowerCase(),
  );
}

function usableNames(capability: MemberStatusCapability): string[] {
  return Array.from(
    new Set(
      [...(capability.use_names ?? []), capability.use_name].filter(
        (name): name is string => Boolean(name),
      ),
    ),
  );
}

function MemberCapability({
  capability,
  stamp,
}: {
  capability: MemberStatusCapability;
  stamp: EvidenceStamp | null;
}) {
  const meta = capabilityMeta(capability.icon_key);
  const Icon = meta.icon;
  const state = availabilityState(capability.status, capability.reason_code);
  const reason = capability.available
    ? null
    : (availabilityReason(capability.status, capability.reason_code)?.replace(
        /^(Busy|Starting|Unavailable)\.\s*/,
        "",
      ) ?? null);
  return (
    <li className="flex min-w-0 items-start gap-3 py-3">
      <span
        role="img"
        aria-label={capability.capability_title ?? "Capability"}
        data-icon-key={capability.icon_key}
        className={cn(
          "inline-flex size-8 shrink-0 items-center justify-center rounded-control",
          capability.available
            ? cn(meta.tint, meta.tone)
            : "bg-muted text-muted-foreground",
        )}
      >
        <Icon aria-hidden="true" className="size-4" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-baseline justify-between gap-x-3">
          <p className="truncate font-semibold">
            {capability.capability_title ?? "Capability"}
          </p>
          <StateLabel state={state} className="shrink-0" />
        </div>
        {stamp && stamp.kind === "used" ? (
          <EvidenceTime stamp={stamp} className="block" />
        ) : null}
        {reason ? <p className="mt-1 text-sm">{reason}</p> : null}
      </div>
    </li>
  );
}

export function MemberStatus() {
  const { data, error, isPending, isError, isFetching, refetch } = useQuery({
    queryKey: ["account", "status"],
    queryFn: productApi.status,
    retry: (count, cause) =>
      !(cause instanceof ApiError && cause.status < 500) && count < 2,
    refetchInterval: 30_000,
  });
  // Home carries the evidence source, so a check is never shown as a use.
  const { data: home } = useQuery({
    queryKey: ["account", "home"],
    queryFn: productApi.home,
    retry: false,
  });
  const evidence = new Map(
    (home?.capabilities ?? []).map((row) => [
      row.capability,
      evidenceStamp(row.availability?.evidence),
    ]),
  );
  const checks = [...evidence.values()].filter(
    (stamp): stamp is EvidenceStamp => stamp?.kind === "checked",
  );
  const oldest = checks.length
    ? checks.reduce((a, b) => (b.at < a.at ? b : a))
    : null;
  const expired = error instanceof ApiError && error.status === 401;
  const notice = home ? spendingNotice(home.receipts ?? []) : null;
  const verdict = data
    ? homeVerdict(data.capabilities as unknown as HomeCapability[])
    : null;
  const unavailable = new Set(
    (data?.capabilities ?? [])
      .filter((capability) => !capability.available)
      .map((capability) => capability.capability),
  );
  const recentFailure = (data?.receipts ?? []).find((receipt) => {
    const capability = intentCapability(receipt.intent);
    return failed(receipt) && capability && unavailable.has(capability);
  });
  const failureCapability = recentFailure
    ? (data?.capabilities ?? []).find(
        (capability) =>
          capability.capability === intentCapability(recentFailure.intent),
      )
    : null;
  return (
    <div className="flex min-w-0 flex-col gap-10">
      <header className="flex flex-col gap-6">
        <div className="flex items-center justify-between gap-3">
          <h1 className="text-muted-foreground text-base font-medium">Status</h1>
          <Button
            variant="outline"
            size="sm"
            disabled={isFetching}
            onClick={() => void refetch()}
          >
            <RefreshCcw aria-hidden="true" />
            <span className="max-sm:sr-only">{isFetching ? "Checking…" : "Refresh status"}</span>
          </Button>
        </div>
        {verdict ? (
          <Verdict
            tone={verdict.tone}
            detail={oldest ? <EvidenceTime stamp={oldest} className="text-base" /> : null}
          >
            {verdict.text}
          </Verdict>
        ) : null}
      </header>
      {isPending ? (
        <div
          role="status"
          aria-label="Loading your model status"
          aria-busy="true"
          className="space-y-4"
        >
          <Skeleton className="h-10 w-2/3" />
          <Skeleton className="h-64 w-full rounded-panel" />
        </div>
      ) : expired ? (
        <EmptyState
          title="Sign in to see your status"
          description="Your session has ended. Sign in to view the models available to your account."
          action={
            <Button asChild>
              <Link href="/sign-in/?return_to=%2Fui%2Fstatus%2F">Sign in</Link>
            </Button>
          }
        />
      ) : isError ? (
        <ErrorState
          heading="Couldn’t check your status"
          message={
            error instanceof ApiError && error.status >= 400
              ? `The gateway answered with an error (HTTP ${error.status}). The owner can see details. Try again.`
              : "We couldn’t tell where this stopped. Try again."
          }
          onRetry={() => void refetch()}
          retrying={isFetching}
        />
      ) : data ? (
        <div className="grid items-start gap-x-12 gap-y-10 lg:grid-cols-[minmax(0,1fr)_minmax(0,20rem)]">
          <div className="min-w-0 space-y-10">
            {notice ? (
              <Notice
                title="Your paid requests are being refused."
                action={
                  <a
                    href={`/r/${encodeURIComponent(notice.requestId)}`}
                    className="text-link inline-flex min-h-11 items-center font-medium underline-offset-4 hover:underline sm:min-h-0"
                  >
                    Open receipt
                  </a>
                }
              >
                {notice.message} Last refused{" "}
                <RelativeTime ts={notice.at} fallback="" />. Ask the gateway
                owner to raise the cap.
              </Notice>
            ) : null}
            {recentFailure && failureCapability ? (
              <section
                aria-labelledby="your-failure-heading"
                className="bg-card border-warning/40 space-y-2 rounded-panel border p-5"
              >
                <h2 id="your-failure-heading" className="text-lg font-semibold">
                  Your{" "}
                  {(
                    failureCapability.capability_title ??
                    capabilityMeta(failureCapability.capability).label
                  ).toLowerCase()}{" "}
                  request failed{" "}
                  <RelativeTime ts={recentFailure.created_at} fallback="" />.
                </h2>
                <p className="text-sm">
                  Your app reached the gateway.{" "}
                  {failureCapability.capability_title ??
                    capabilityMeta(failureCapability.capability).label}{" "}
                  is{" "}
                  {availabilityState(
                    failureCapability.status,
                    failureCapability.reason_code,
                  ).label.toLowerCase()}{" "}
                  right now.{" "}
                  {availabilityReason(
                    failureCapability.status,
                    failureCapability.reason_code,
                  )?.replace(/^[^.]*\.\s*/, "") || "Try again later."}
                </p>
                <a
                  href={recentFailure.href}
                  className="text-link inline-flex min-h-11 items-center text-sm font-medium underline-offset-4 hover:underline"
                >
                  Open receipt
                </a>
              </section>
            ) : null}
            <section aria-labelledby="capability-status-heading">
              <SectionHeading
                id="capability-status-heading"
                title="Your capabilities"
              />
              {data.capabilities.length ? (
                <ul className="grid gap-x-10 sm:grid-cols-2 [&>li]:border-b">
                  {data.capabilities.map((capability) => (
                    <MemberCapability
                      key={capability.capability}
                      capability={capability}
                      stamp={evidence.get(capability.capability) ?? null}
                    />
                  ))}
                </ul>
              ) : null}
              {data.capabilities.length ? (
                <details className="mt-4 text-sm">
                  <summary className="text-muted-foreground hover:text-foreground flex min-h-11 w-fit cursor-pointer items-center font-medium sm:min-h-8">
                    Model names
                  </summary>
                  <dl className="text-muted-foreground mt-2 space-y-2">
                    {data.capabilities.map((capability) => (
                      <div key={capability.capability}>
                        <dt className="text-foreground">
                          {capability.capability_title ?? "Capability"}
                          {" · "}
                          <span>
                            {capability.recommended_model ??
                              capability.title ??
                              "Model details unavailable"}
                          </span>
                        </dt>
                        {usableNames(capability).length ? (
                          <dd>
                            <span className="sr-only">Names you can use: </span>
                            <span className="font-mono text-xs break-all">
                              {usableNames(capability).join(", ")}
                            </span>
                          </dd>
                        ) : null}
                        {capability.summary ? (
                          <dd>{capability.summary}</dd>
                        ) : null}
                      </div>
                    ))}
                  </dl>
                </details>
              ) : null}
              {data.capabilities.length ? null : (
                <EmptyState
                  title="No models are available to your account yet"
                  description="Ask the gateway owner to connect a model and give you access."
                />
              )}
            </section>
          </div>
          <section aria-labelledby="recent-status-heading" className="min-w-0">
            <SectionHeading
              id="recent-status-heading"
              title="Your recent requests"
            />
            {data.receipts.length ? (
              <ul className="divide-y">
                {data.receipts.map((receipt) => {
                  const capability = intentCapability(receipt.intent);
                  const meta = capabilityMeta(capability);
                  const Icon = meta.icon;
                  const bad = failed(receipt);
                  const running = [
                    "running",
                    "accepted",
                    "queued",
                    "in_progress",
                    "pending",
                  ].includes(String(receipt.status ?? "").toLowerCase());
                  return (
                    <li key={receipt.request_id}>
                      {/* The gateway sends an absolute /ui path; next/link would prefix /ui again. */}
                      <a
                        href={receipt.href}
                        aria-label={`View receipt ${receipt.request_id}`}
                        className="group flex min-h-11 items-center gap-3 py-2.5"
                      >
                        <Icon
                          aria-hidden="true"
                          className="text-muted-foreground size-4 shrink-0"
                        />
                        <span className="min-w-0 flex-1 truncate text-sm font-medium group-hover:underline">
                          {capability ? meta.label : "Request"}
                        </span>
                        <span
                          className={cn(
                            "text-sm",
                            bad
                              ? "text-danger"
                              : running
                                ? "text-info"
                                : "text-muted-foreground",
                          )}
                        >
                          {bad ? "Failed" : running ? "In progress" : ""}
                        </span>
                        <span className="text-muted-foreground shrink-0 text-xs">
                          <RelativeTime ts={receipt.created_at} fallback="" />
                        </span>
                      </a>
                    </li>
                  );
                })}
              </ul>
            ) : (
              <p className="text-muted-foreground text-sm">
                No receipts yet.
              </p>
            )}
            <p className="text-muted-foreground mt-4 text-sm">
              App still not working?{" "}
              <Link
                href="/home"
                className="text-link font-medium underline-offset-4 hover:underline"
              >
                Check your connection
              </Link>
            </p>
          </section>
        </div>
      ) : null}
    </div>
  );
}

export function StatusView() {
  const viewer = useViewer();
  return viewer.admin ? <StatusClient /> : <MemberStatus />;
}
