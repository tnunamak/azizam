"use client";

import { useQuery } from "@tanstack/react-query";
import { accountApi } from "@/lib/account-api";
import { ErrorState } from "@/components/product/error-state";
import { TechnicalDetails } from "@/components/product/technical-details";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { useModelName } from "@/lib/use-model-name";
import { contentOf, hasRemovedDetails } from "@/lib/request-content";
import {
  keptSentence,
  requestKind,
  routeWords,
  statusWords,
  type RequestHistoryRecord,
} from "@/lib/request-history";
import { RequestBody } from "./history-kinds";
import { RequestTime } from "./request-time";

/**
 * One kept request, as its kind reads it: the model's name and when it was made at the top, what was sent and what came
 * back in the middle, and the raw record folded away at the bottom.
 */
export function HistoryDetail({
  recordId,
  app,
  onOpenChange,
  onRequestDelete,
}: {
  recordId: string | null;
  /** The app the request came through, from its list row; null when it came from the browser. */
  app: string | null;
  onOpenChange: (open: boolean) => void;
  onRequestDelete: (recordId: string) => void;
}) {
  const nameModel = useModelName();
  const {
    data,
    error: recordError,
    isError: recordFailed,
    isPending: recordPending,
    isRefetching: recordRefetching,
    refetch: retryRecord,
  } = useQuery({
    queryKey: ["account", "request-history", "record", recordId],
    queryFn: () => accountApi.requestHistoryRecord(recordId ?? ""),
    enabled: recordId !== null,
    retry: false,
  });
  const requestId = data?.request_id ?? null;
  const { data: receiptData, isPending: receiptIsPending } = useQuery({
    queryKey: ["account", "request-history", "receipt", requestId],
    queryFn: () => accountApi.receiptRetention(requestId ?? ""),
    enabled: requestId !== null,
    retry: false,
  });
  const sentence = keptSentence(
    receiptData?.retention?.find((entry) => entry.kind === "request_history"),
  );

  return (
    <Sheet open={recordId !== null} onOpenChange={onOpenChange}>
      {/* The sheet's base classes (w-3/4, sm:max-w-sm) do not merge with these: cn only joins, so the ! forms win. */}
      <SheetContent className="!w-full min-w-0 overflow-x-hidden overflow-y-auto sm:!max-w-lg">
        <SheetHeader className="pr-14">
          <SheetTitle className="break-words">
            {data
              ? data.summary.model
                ? nameModel(data.summary.model)
                : requestKind(data.summary.route).label
              : "Request"}
          </SheetTitle>
          <SheetDescription>
            {data ? <RequestTime at={data.created_at} /> : "Kept request"}
          </SheetDescription>
        </SheetHeader>
        {recordPending ? (
          <div role="status" aria-label="Loading this request" aria-busy="true">
            <Skeleton className="h-24 w-full" />
          </div>
        ) : recordFailed ? (
          <ErrorState
            heading="Couldn’t load this request"
            error={recordError}
            onRetry={() => void retryRecord()}
            retrying={recordRefetching}
          />
        ) : data ? (
          <Details
            record={data}
            app={app}
            sentence={sentence}
            receiptPending={requestId !== null && receiptIsPending}
            onRequestDelete={() => onRequestDelete(data.record_id)}
          />
        ) : null}
      </SheetContent>
    </Sheet>
  );
}

function Details({
  record,
  app,
  sentence,
  receiptPending,
  onRequestDelete,
}: {
  record: RequestHistoryRecord;
  app: string | null;
  sentence: string | null;
  receiptPending: boolean;
  onRequestDelete: () => void;
}) {
  return (
    <div className="min-w-0 space-y-5 px-4 pb-6">
      <RequestBody content={contentOf(record)} requestId={record.request_id} />
      <div className="text-muted-foreground text-meta space-y-1">
        {hasRemovedDetails(record) ? (
          <p>Some details weren’t kept (keys and links are never saved).</p>
        ) : null}
        {record.omissions.length > 0 ? (
          <p>Part of this wasn’t kept, because it was too large or couldn’t be read.</p>
        ) : null}
        <p>
          {sentence ??
            (receiptPending
              ? "Checking the receipt…"
              : "The receipt for this request isn’t available.")}
        </p>
      </div>
      <div>
        <Button variant="outline" onClick={onRequestDelete}>
          Delete this request
        </Button>
      </div>
      <TechnicalDetails text={technicalWords(record, app)} />
    </div>
  );
}

/** The app, route and status, then the record as it was kept, headers included. Only Technical details shows them. */
function technicalWords(record: RequestHistoryRecord, app: string | null): string {
  const sections = [
    `App: ${app ?? "Browser"}`,
    `Route: ${routeWords(record.summary.route) || "—"}`,
    `Status: ${statusWords(record.summary.status)}`,
    `Sent:\n${JSON.stringify(record.parts.accepted ?? null, null, 2)}`,
  ];
  if (record.parts.received !== undefined)
    sections.push(`Came back:\n${JSON.stringify(record.parts.received, null, 2)}`);
  return sections.join("\n\n");
}
