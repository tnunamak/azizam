"use client";

import { useQuery } from "@tanstack/react-query";
import { accountApi } from "@/lib/account-api";
import { formatTimestamp } from "@/lib/format";
import { ErrorState } from "@/components/product/error-state";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  keptSentence,
  routeWords,
  statusWords,
  type RequestHistoryRecord,
} from "@/lib/request-history";

/** What was kept of one request: what was sent, what came back, and what the receipt says about keeping it. */
export function HistoryDetail({
  recordId,
  onOpenChange,
  onRequestDelete,
}: {
  recordId: string | null;
  onOpenChange: (open: boolean) => void;
  onRequestDelete: (recordId: string) => void;
}) {
  const record = useQuery({
    queryKey: ["account", "request-history", "record", recordId],
    queryFn: () => accountApi.requestHistoryRecord(recordId ?? ""),
    enabled: recordId !== null,
    retry: false,
  });
  const requestId = record.data?.request_id ?? null;
  const receipt = useQuery({
    queryKey: ["account", "request-history", "receipt", requestId],
    queryFn: () => accountApi.receiptRetention(requestId ?? ""),
    enabled: requestId !== null,
    retry: false,
  });
  const sentence = keptSentence(
    receipt.data?.retention?.find((entry) => entry.kind === "request_history"),
  );

  return (
    <Sheet open={recordId !== null} onOpenChange={onOpenChange}>
      <SheetContent className="w-full overflow-y-auto sm:max-w-lg">
        <SheetHeader className="pr-14">
          <SheetTitle>
            {record.data?.summary.model ||
              routeWords(record.data?.summary.route) ||
              "Request"}
          </SheetTitle>
          <SheetDescription>
            {record.data
              ? formatTimestamp(record.data.created_at).children
              : "Kept request"}
          </SheetDescription>
        </SheetHeader>
        {record.isPending ? (
          <div role="status" aria-label="Loading this request" aria-busy="true">
            <Skeleton className="h-24 w-full" />
          </div>
        ) : record.isError ? (
          <ErrorState
            heading="Couldn’t load this request"
            error={record.error}
            onRetry={() => void record.refetch()}
            retrying={record.isRefetching}
          />
        ) : record.data ? (
          <Details
            record={record.data}
            sentence={sentence}
            receiptPending={requestId !== null && receipt.isPending}
            onRequestDelete={() => onRequestDelete(record.data.record_id)}
          />
        ) : null}
      </SheetContent>
    </Sheet>
  );
}

function Details({
  record,
  sentence,
  receiptPending,
  onRequestDelete,
}: {
  record: RequestHistoryRecord;
  sentence: string | null;
  receiptPending: boolean;
  onRequestDelete: () => void;
}) {
  const received = record.parts.received;
  const kept = record.kept_until === null
    ? "Until you delete it"
    : formatTimestamp(record.kept_until).children;
  return (
    <div className="space-y-5 px-4 pb-6">
      <p className="text-sm">
        {sentence ??
          (receiptPending
            ? "Checking the receipt…"
            : "The receipt for this request isn’t available.")}
      </p>
      <dl className="grid gap-x-4 gap-y-2 text-sm sm:grid-cols-[7rem_minmax(0,1fr)]">
        <dt className="text-muted-foreground">Route</dt>
        <dd className="break-all">{routeWords(record.summary.route) || "—"}</dd>
        <dt className="text-muted-foreground">Status</dt>
        <dd>{statusWords(record.summary.status)}</dd>
        <dt className="text-muted-foreground">Kept until</dt>
        <dd>{kept}</dd>
      </dl>
      <section aria-labelledby="history-sent" className="space-y-2">
        <h3 id="history-sent" className="text-sm font-medium">
          What was sent
        </h3>
        <pre className="bg-muted max-h-72 overflow-auto rounded-md p-3 text-xs break-words whitespace-pre-wrap">
          {JSON.stringify(record.parts.accepted ?? null, null, 2)}
        </pre>
      </section>
      {received !== undefined ? (
        <section aria-labelledby="history-received" className="space-y-2">
          <h3 id="history-received" className="text-sm font-medium">
            What came back
          </h3>
          <pre className="bg-muted max-h-72 overflow-auto rounded-md p-3 text-xs break-words whitespace-pre-wrap">
            {JSON.stringify(received, null, 2)}
          </pre>
        </section>
      ) : null}
      {record.omissions.length > 0 ? (
        <p className="text-muted-foreground text-meta">
          Part of this wasn’t kept, because it was too large or couldn’t be read.
        </p>
      ) : null}
      <div>
        <Button variant="outline" onClick={onRequestDelete}>
          Delete this request
        </Button>
      </div>
    </div>
  );
}
