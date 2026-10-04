"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useViewer } from "@/components/shell/session";
import { Button } from "@/components/ui/button";
import { CopyButton } from "@/components/ui/copy-button";
import { RelativeTime } from "@/components/ui/relative-time";
import { Skeleton } from "@/components/ui/skeleton";
import {
  CapabilityIcon,
  type Capability,
} from "@/components/product/capability-icon";
import {
  ErrorState,
  PageHeader,
  StatusPill,
  EmptyState,
} from "@/components/product";
import { ApiError } from "@/lib/api";
import {
  productApi,
  type MemberStatusCapability,
  type MemberStatus as MemberStatusData,
} from "@/lib/product-api";
import { StatusClient } from "./status-client";

const capabilityNames: Record<string, string> = {
  chat: "Chat",
  image: "Images",
  video: "Video",
  audio: "Speech",
  voice: "Speech",
  transcribe: "Transcription",
  decide: "Decisions",
  embed: "Embeddings",
};
function iconCapability(capability: string): Capability {
  const value = capability === "audio" ? "voice" : capability;
  return [
    "chat",
    "image",
    "video",
    "voice",
    "transcribe",
    "decide",
    "embed",
  ].includes(value)
    ? (value as Capability)
    : "chat";
}

function MemberCapability({
  capability,
}: {
  capability: MemberStatusCapability;
}) {
  return (
    <li className="flex min-w-0 items-start gap-3 border-b py-5 last:border-0">
      <CapabilityIcon capability={iconCapability(capability.capability)} />
      <div className="min-w-0 space-y-2 break-words">
        <h3 className="font-medium">
          {capabilityNames[capability.capability] ?? capability.capability}
        </h3>
        {(capability.model || capability.workflow) && (
          <p className="text-sm break-all">
            {capability.model ?? capability.workflow}
          </p>
        )}
        <p className="text-muted-foreground text-xs break-all">
          {capability.intent}
        </p>
        <StatusPill status={capability.status} reason={capability.reason} />
        <p className="text-muted-foreground text-xs">
          {capability.measured ? (
            <>
              Observed{" "}
              <RelativeTime
                ts={capability.observed_at_s}
                fallback="at an unknown time"
              />
            </>
          ) : (
            "Not measured yet"
          )}
        </p>
      </div>
    </li>
  );
}

function ownerSummary(data: MemberStatusData): string {
  return [
    "Please help me check my gateway access.",
    data.status.reason,
    ...data.capabilities.map(
      (item) =>
        `${capabilityNames[item.capability] ?? item.capability}${item.model || item.workflow ? ` (${item.model ?? item.workflow})` : ""} [${item.intent}]: ${item.status}. ${item.reason}`,
    ),
    ...data.receipts.map(
      (item) =>
        `Request ${item.request_id}: ${item.status ?? "Unknown result"}.`,
    ),
  ].join("\n");
}

export function MemberStatus() {
  const { data, error, isPending, isError, isFetching, refetch } = useQuery({
    queryKey: ["account", "status"],
    queryFn: productApi.status,
    retry: (count, cause) =>
      !(cause instanceof ApiError && cause.status < 500) && count < 2,
    refetchInterval: 30_000,
  });
  const expired = error instanceof ApiError && error.status === 401;
  return (
    <div className="space-y-6">
      <PageHeader
        title="Status"
        answer="Check the capabilities available to your account."
        actions={
          <Button
            variant="outline"
            disabled={isFetching}
            onClick={() => void refetch()}
          >
            {isFetching ? "Checking…" : "Refresh status"}
          </Button>
        }
      />
      {isPending ? (
        <div
          role="status"
          aria-label="Loading your model status"
          aria-busy="true"
          className="space-y-4"
        >
          <Skeleton className="h-12 w-full" />
          <Skeleton className="h-40 w-full" />
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
          message="Your model status could not be loaded. Check your connection and try again."
          onRetry={() => void refetch()}
          retrying={isFetching}
        />
      ) : data ? (
        <>
          <div className="border-b pb-6">
            <StatusPill
              status={data.status.state}
              reason={data.status.reason}
            />
          </div>
          <section aria-labelledby="status-help-heading" className="space-y-4">
            <h2 id="status-help-heading" className="text-lg font-semibold">
              Need help with your app?
            </h2>
            <div className="flex flex-wrap gap-3">
              <Button asChild>
                <Link href="/home">Check your connection settings</Link>
              </Button>
              <Button variant="outline" asChild>
                <Link href="/try">Try a request</Link>
              </Button>
              <Button variant="outline" asChild>
                <a href="#recent-status-heading">View recent requests</a>
              </Button>
            </div>
            <details className="rounded-lg border p-4">
              <summary className="focus-visible:outline-ring flex min-h-11 cursor-pointer items-center font-medium focus-visible:outline focus-visible:outline-2">
                Ask the owner
              </summary>
              <div className="space-y-3 pt-3">
                <p className="text-muted-foreground text-sm">
                  {data.help} Send the owner this status summary and the request
                  ID from a failing receipt. Do not send your API key.
                </p>
                <pre className="bg-muted rounded-md p-3 text-xs break-all whitespace-pre-wrap">
                  {ownerSummary(data)}
                </pre>
                <CopyButton value={ownerSummary(data)} className="min-h-11">
                  Copy status summary
                </CopyButton>
              </div>
            </details>
          </section>
          <section aria-labelledby="capability-status-heading">
            <h2
              id="capability-status-heading"
              className="text-lg font-semibold"
            >
              Your capabilities
            </h2>
            {data.capabilities.length ? (
              <ul className="grid gap-x-8 sm:grid-cols-2">
                {data.capabilities.map((capability) => (
                  <MemberCapability
                    key={`${capability.capability}-${capability.intent}-${capability.model}-${capability.workflow}`}
                    capability={capability}
                  />
                ))}
              </ul>
            ) : (
              <EmptyState
                title="No models are available to your account yet"
                description="Ask the gateway owner to connect a model and grant your account access."
              />
            )}
          </section>
          <section
            aria-labelledby="recent-status-heading"
            className="space-y-3 border-t pt-6"
          >
            <h2 id="recent-status-heading" className="text-lg font-semibold">
              Your recent requests
            </h2>
            <p className="text-muted-foreground text-sm">
              Open a receipt to see what happened to a request. A past result
              does not measure availability now.
            </p>
            {data.receipts.length ? (
              <ul className="divide-y">
                {data.receipts.map((receipt) => (
                  <li
                    key={receipt.request_id}
                    className="flex min-w-0 flex-wrap items-center justify-between gap-3 py-3"
                  >
                    <div className="min-w-0 space-y-1">
                      <p className="text-sm break-all">
                        {receipt.intent ?? "Request"} ·{" "}
                        {receipt.status ?? "Unknown result"}
                      </p>
                      <p className="text-muted-foreground text-xs">
                        <RelativeTime
                          ts={receipt.created_at}
                          fallback="Time not recorded"
                        />
                      </p>
                    </div>
                    <Button variant="outline" asChild>
                      <Link
                        href={receipt.href}
                        aria-label={`View receipt ${receipt.request_id}`}
                      >
                        View receipt
                      </Link>
                    </Button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-muted-foreground text-sm">
                No receipts for your account yet. Try a request to record its
                result here.
              </p>
            )}
          </section>
        </>
      ) : null}
    </div>
  );
}

export function StatusView() {
  const viewer = useViewer();
  return viewer.admin ? <StatusClient /> : <MemberStatus />;
}
