"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useViewer } from "@/components/shell/session";
import { Button } from "@/components/ui/button";
import { RelativeTime } from "@/components/ui/relative-time";
import { Skeleton } from "@/components/ui/skeleton";
import {
  AudioLines,
  GitBranch,
  Image,
  Layers,
  ListFilter,
  MessageSquare,
  Mic,
  Music2,
  Video,
  Waves,
  CircleHelp,
} from "lucide-react";
import {
  ErrorState,
  PageHeader,
  StatusPill,
  EmptyState,
} from "@/components/product";
import { ApiError } from "@/lib/api";
import { productApi, type MemberStatusCapability } from "@/lib/product-api";
import { availabilityReason, statusSummaryCopy } from "@/lib/status-copy";
import { StatusClient } from "./status-client";

const capabilityIcons = {
  chat: {
    icon: MessageSquare,
    color: "text-capability-chat bg-capability-chat/10",
  },
  image: { icon: Image, color: "text-capability-image bg-capability-image/10" },
  video: { icon: Video, color: "text-capability-video bg-capability-video/10" },
  music: {
    icon: Music2,
    color: "text-capability-voice bg-capability-voice/10",
  },
  sfx: { icon: Waves, color: "text-capability-voice bg-capability-voice/10" },
  speech: {
    icon: AudioLines,
    color: "text-capability-voice bg-capability-voice/10",
  },
  transcribe: {
    icon: Mic,
    color: "text-capability-transcribe bg-capability-transcribe/10",
  },
  decide: {
    icon: GitBranch,
    color: "text-capability-decide bg-capability-decide/10",
  },
  embed: {
    icon: Layers,
    color: "text-capability-embed bg-capability-embed/10",
  },
  rerank: {
    icon: ListFilter,
    color: "text-capability-embed bg-capability-embed/10",
  },
};

function MemberCapability({
  capability,
}: {
  capability: MemberStatusCapability;
}) {
  const usableNames = Array.from(
    new Set(
      [...(capability.use_names ?? []), capability.use_name].filter(
        (name): name is string => Boolean(name),
      ),
    ),
  );
  const { icon: Icon, color } = capabilityIcons[
    capability.icon_key as keyof typeof capabilityIcons
  ] ?? {
    icon: CircleHelp,
    color: "text-muted-foreground bg-muted",
  };
  return (
    <li className="flex min-w-0 items-start gap-3 border-b py-5 last:border-0">
      <span
        role="img"
        aria-label={capability.capability_title ?? "Capability"}
        data-icon-key={capability.icon_key}
        className={`inline-flex size-8 shrink-0 items-center justify-center rounded-md ${color}`}
      >
        <Icon aria-hidden="true" className="size-4" />
      </span>
      <div className="min-w-0 flex-1 space-y-2 break-words">
        <h3 className="font-medium">
          {capability.capability_title ?? "Capability"}
        </h3>
        <p className="text-muted-foreground text-sm">
          {capability.recommended_model ??
            capability.title ??
            "Model details unavailable"}
        </p>
        <StatusPill status={capability.status} reason={availabilityReason(capability.status, capability.reason_code)} />
        {(usableNames.length > 0 || capability.summary) && (
          <details className="text-sm">
            <summary className="focus-visible:outline-ring text-muted-foreground flex min-h-11 cursor-pointer items-center focus-visible:outline focus-visible:outline-2">
              Details
            </summary>
            <div className="text-muted-foreground space-y-2 pb-2">
              {usableNames.length > 0 && (
                <>
                  <h4 className="text-foreground text-sm font-medium">
                    Names you can use
                  </h4>
                  <p className="break-all">{usableNames.join(", ")}</p>
                </>
              )}
              {capability.summary && <p>{capability.summary}</p>}
            </div>
          </details>
        )}
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
              reason={statusSummaryCopy(data.status)}
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
                    key={capability.capability}
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
                      {(receipt.intent || receipt.status) && (
                        <p className="text-sm break-all">
                          {[receipt.intent, receipt.status]
                            .filter(Boolean)
                            .join(" · ")}
                        </p>
                      )}
                      <p className="text-muted-foreground text-xs">
                        <RelativeTime ts={receipt.created_at} fallback="" />
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
