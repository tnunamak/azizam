"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { RefreshCcw } from "lucide-react";
import { useViewer } from "@/components/shell/session";
import { Button } from "@/components/ui/button";
import { RelativeTime } from "@/components/ui/relative-time";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState, EmptyState } from "@/components/product";
import { capabilityMeta, SectionHeading } from "@/components/product/readiness";
import {
  Callout,
  CapabilityList,
  StateWord,
  StatusLine,
} from "@/components/product/status-ui";
import { CapNotice } from "@/components/product/cap-notice";
import { PageHeader } from "@/components/product/page-header";
import { ApiError } from "@/lib/api";
import { productApi, type MemberStatusCapability } from "@/lib/product-api";
import {
  activeCapRefusal,
  CAP_WARNING,
  capabilityView,
  verdictFor,
  isInFlight,
  isSuccess,
  isTerminalFailure,
  requestCapability,
} from "@/lib/capability-state";
import {
  capabilityLabel,
  useCapabilityViews,
} from "@/lib/use-capability-views";
import { askOwner, useOwnerName } from "@/lib/use-owner-name";
import { appPlumbingCapabilities, memberTerms } from "@/lib/terms";
import { StatusClient } from "./status-client";

function usableNames(capability: MemberStatusCapability): string[] {
  return Array.from(
    new Set(
      [...(capability.use_names ?? []), capability.use_name].filter(
        (name): name is string => Boolean(name),
      ),
    ),
  );
}

/** Receipt states use the same words everywhere: Failed, Refused, In progress. */
function receiptWord(status: string | null) {
  if (isTerminalFailure(status))
    return { word: "Failed", tone: "bad" as const };
  if (String(status ?? "").toLowerCase() === "rejected")
    return { word: "Refused", tone: "warn" as const };
  if (isInFlight(status)) return { word: "In progress", tone: "busy" as const };
  return null;
}

export function MemberStatus() {
  const ownerName = useOwnerName();
  const { data, error, isPending, isError, isFetching, refetch } = useQuery({
    queryKey: ["account", "status"],
    queryFn: productApi.status,
    retry: (count, cause) =>
      !(cause instanceof ApiError && cause.status < 500) && count < 2,
    refetchInterval: 30_000,
  });
  const model = useCapabilityViews(false);
  const expired = error instanceof ApiError && error.status === 401;
  const refusal = activeCapRefusal(model.requests);
  // Home and Status read the same snapshot. If Home has no rows (an older
  // gateway), the Status rows go through the same model instead.
  const memberRows = (data?.capabilities ?? []).filter(
    (row) => !appPlumbingCapabilities.has(row.icon_key ?? row.capability),
  );
  const capabilityViews = model.views.length
    ? model.views
    : memberRows.map((row) =>
        capabilityView(
          {
            key: row.icon_key ?? row.capability,
            label: capabilityLabel(
              row.icon_key ?? row.capability,
              row.capability_title,
            ),
            status: row.status,
            reason_code: row.reason_code,
            available: row.available,
            evidence:
              typeof row.observed_at_s === "number"
                ? { source: "status", observed_at_s: row.observed_at_s }
                : null,
          },
          { now: Date.now() / 1000, requests: model.requests },
        ),
      );
  const modelVerdict = model.views.length
    ? model.verdict
    : verdictFor(capabilityViews);
  const views = new Map(capabilityViews.map((view) => [view.key, view]));

  // A failure still matters only while no later request for the same
  // capability has succeeded.
  const receipts = data?.receipts ?? [];
  const unresolved = receipts.find((receipt) => {
    if (!isTerminalFailure(receipt.status)) return false;
    const capability = requestCapability(receipt.intent);
    return !receipts.some(
      (other) =>
        requestCapability(other.intent) === capability &&
        isSuccess(other.status) &&
        (other.created_at ?? 0) > (receipt.created_at ?? 0),
    );
  });
  const unresolvedView = unresolved
    ? views.get(requestCapability(unresolved.intent) ?? "")
    : undefined;

  // A failed refresh never keeps an old verdict on screen.
  const verdict =
    isPending || model.isPending
      ? { tone: "unknown" as const, text: "Checking" }
      : isError || model.isError
        ? { tone: "unknown" as const, text: "Couldn’t check status" }
        : modelVerdict;
  // No check time while checking or after a failed check (red team round 2).
  const checkedAt =
    isPending || model.isPending || isError || model.isError
      ? null
      : modelVerdict.checkedAt;

  return (
    <div className="page-read min-w-0">
      <PageHeader
        title="Status"
        actions={
          <Button
            variant="outline"
            size="sm"
            disabled={isFetching || model.isFetching}
            onClick={() => {
              void refetch();
              model.refetch();
            }}
          >
            <RefreshCcw aria-hidden="true" />
            <span className="max-sm:sr-only">
              {isFetching ? "Checking…" : "Refresh status"}
            </span>
          </Button>
        }
      >
        <StatusLine
          tone={verdict.tone}
          text={verdict.text}
          checkedAt={checkedAt}
          unconfirmed={
            isPending || model.isPending ? 0 : modelVerdict.unconfirmed
          }
        />
      </PageHeader>
      <div className="flex min-w-0 flex-col gap-(--space-section)">
        {isPending ? (
          <div
            role="status"
            aria-label="Loading your model status"
            aria-busy="true"
            className="space-y-4"
          >
            <Skeleton className="h-10 w-2/3" />
            <Skeleton className="rounded-panel h-64 w-full" />
          </div>
        ) : expired ? (
          <EmptyState
            title="Sign in to see your status"
            description="Your session has ended. Sign in to view the models available to your account."
            action={
              <Button asChild>
                <Link href="/sign-in/?return_to=%2Fui%2Fstatus%2F">
                  Sign in
                </Link>
              </Button>
            }
          />
        ) : isError ? (
          <ErrorState
            heading="Couldn’t check your status"
            error={error}
            // Only an HTTP answer proves the gateway was reached.
            message={
              error instanceof ApiError
                ? "The gateway returned an error. Try again."
                : "We couldn’t tell where this stopped. Try again."
            }
            onRetry={() => void refetch()}
            retrying={isFetching}
          />
        ) : data ? (
          <div className="grid items-start gap-(--space-section)">
            <div className="min-w-0 space-y-(--space-section)">
              {refusal ? (
                <CapNotice
                  warning={
                    (refusal.warnings ?? []).find((text) =>
                      CAP_WARNING.test(text),
                    ) ?? ""
                  }
                  at={refusal.at}
                  owner={false}
                />
              ) : null}
              {unresolved ? (
                <Callout
                  severity="attention"
                  title={`Your ${(unresolvedView?.label ?? "latest").toLowerCase()} request failed`}
                  meta={<RelativeTime ts={unresolved.created_at} fallback="" />}
                  actions={
                    <a
                      href={unresolved.href}
                      className="text-link inline-flex min-h-8 max-sm:min-h-11 items-center text-meta font-medium underline-offset-4 hover:underline sm:min-h-0"
                    >
                      Open receipt
                    </a>
                  }
                >
                  <p>
                    Your app reached the gateway.
                    {unresolvedView
                      ? ` ${unresolvedView.label} is ${unresolvedView.word.toLowerCase()} now${unresolvedView.detail ? `: ${unresolvedView.detail.charAt(0).toLowerCase()}${unresolvedView.detail.slice(1)}` : "."}`
                      : ""}
                    {unresolvedView?.state === "ready" ? " Try again." : ""}
                  </p>
                </Callout>
              ) : null}
              <section aria-labelledby="capability-status-heading">
                <SectionHeading
                  id="capability-status-heading"
                  title={memberTerms.whatYouCanUse}
                />
                {capabilityViews.length ? (
                  <CapabilityList views={capabilityViews} columns={2} />
                ) : (
                  <EmptyState
                    title="No models are available to your account yet"
                    description={`${askOwner(ownerName)} to connect a model and give you access.`}
                  />
                )}
                {memberRows.length ? (
                  <details className="mt-4 text-meta">
                    <summary className="text-muted-foreground hover:text-foreground flex min-h-8 max-sm:min-h-11 w-fit cursor-pointer items-center font-medium sm:min-h-8">
                      Model names
                    </summary>
                    <dl className="text-muted-foreground mt-2 space-y-2">
                      {memberRows.map((capability) => (
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
                              <span className="sr-only">
                                Names you can use:{" "}
                              </span>
                              <span className="font-mono text-meta break-all">
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
              </section>
            </div>
            <section
              aria-labelledby="recent-status-heading"
              className="min-w-0"
            >
              <SectionHeading
                id="recent-status-heading"
                // The gateway sends the latest five; the title says so, so a
                // request the owner sees is not "missing" from this list.
                title="Your latest requests"
              />
              {receipts.length ? (
                <ul className="divide-y">
                  {receipts.map((receipt) => {
                    const capability = requestCapability(receipt.intent);
                    const meta = capabilityMeta(capability);
                    const Icon = meta.icon;
                    const state = receiptWord(receipt.status);
                    const describedBy = `receipt-${receipt.request_id}`;
                    return (
                      <li key={receipt.request_id}>
                        {/* The gateway sends an absolute /ui path; next/link would prefix /ui again. */}
                        <a
                          href={receipt.href}
                          aria-label="View request receipt"
                          aria-describedby={describedBy}
                          className="group flex min-h-(--row-1) max-sm:min-h-(--row-phone-1) items-center gap-3 py-1.5"
                        >
                          <Icon
                            aria-hidden="true"
                            className="text-muted-foreground size-4 shrink-0"
                          />
                          <span
                            id={describedBy}
                            className="flex min-w-0 flex-1 items-center gap-3"
                          >
                            <span className="min-w-0 flex-1 truncate text-meta font-medium group-hover:underline">
                              {capability ? meta.label : "Request"}
                            </span>
                            {state ? (
                              <StateWord tone={state.tone} word={state.word} />
                            ) : null}
                            <span className="text-muted-foreground shrink-0 text-meta">
                              <RelativeTime
                                ts={receipt.created_at}
                                fallback=""
                              />
                            </span>
                          </span>
                        </a>
                      </li>
                    );
                  })}
                </ul>
              ) : (
                <p className="text-muted-foreground text-meta">
                  No requests yet.
                </p>
              )}
              {/* The next step follows the failure shown (red team round 2):
                  a spending limit is the owner's to lift, a gateway problem
                  is not the member's connection. */}
              <p className="text-muted-foreground mt-4 text-meta">
                {refusal ? (
                  `${askOwner(ownerName)} if you need paid models before the limit resets.`
                ) : modelVerdict.problems.length ? (
                  `${askOwner(ownerName)} if it does not recover.`
                ) : (
                  <>
                    App still not working?{" "}
                    <Link
                      href="/home"
                      className="text-link font-medium underline-offset-4 hover:underline"
                    >
                      Check your connection
                    </Link>
                  </>
                )}
              </p>
            </section>
          </div>
        ) : null}
      </div>
    </div>
  );
}

export function StatusView() {
  const viewer = useViewer();
  return viewer.admin ? <StatusClient /> : <MemberStatus />;
}
