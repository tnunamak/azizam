"use client";

import { useEffect, useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { ArrowRight, ReceiptText } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import { EmptyState, ErrorState, RelativeTime } from "@/components/product";

type Receipt = {
  request_id: string;
  intent?: string;
  requested_model?: string;
  resolved_model?: string;
  status?: string;
  latency_ms?: number | null;
  path?: string;
  parent_request_id?: string;
  warnings?: string[];
  [key: string]: unknown;
};
type RequestRecord = {
  receipt: Receipt;
  person_label: string;
  app_label: string;
  created_at: number;
  source?: { label: string; kind: string };
};
type RequestPage = {
  records: RequestRecord[];
  next_cursor: string | null;
  people: { id: string; label: string }[];
  sources?: { id: string; label: string }[];
};

function requestSummary(receipt: Receipt) {
  const intent = receipt.intent?.replace(/[._-]/g, " ") || "AI request";
  return intent.charAt(0).toUpperCase() + intent.slice(1);
}
function outcome(status?: string) {
  return (
    (
      {
        succeeded: "Completed",
        ok: "Completed",
        failed: "Failed",
        error: "Failed",
        rejected: "Failed",
        cancelled: "Cancelled",
        disconnected: "Disconnected",
        timed_out: "Timed out",
        accepted: "Accepted",
        running: "In progress",
        in_progress: "In progress",
        stale_incomplete: "Outcome not recorded",
        incomplete: "Outcome not recorded",
      } as Record<string, string>
    )[status ?? ""] ?? "Outcome not recorded"
  );
}
function duration(ms?: number | null) {
  return typeof ms === "number"
    ? ms < 1000
      ? `${Math.round(ms)} ms`
      : `${(ms / 1000).toFixed(1)} s`
    : "Not measured yet";
}

export function Requests() {
  const [person, setPerson] = useState("");
  const [source, setSource] = useState("all");
  const [record, setRecord] = useState<RequestRecord | null>(null);
  const {
    data,
    isPending,
    isError,
    isFetchNextPageError,
    isFetchingNextPage,
    hasNextPage,
    fetchNextPage,
    refetch,
  } = useInfiniteQuery({
    queryKey: ["activity", "requests", person, source],
    refetchOnWindowFocus: "always",
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }): Promise<RequestPage> => {
      const params = new URLSearchParams({ limit: "50" });
      if (person) params.set("person_id", person);
      if (source !== "all") params.set("source", source);
      if (pageParam) params.set("before", pageParam);
      const response = await fetch(`/admin/api/activity/requests?${params}`, {
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error("Requests could not be loaded.");
      return response.json();
    },
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  });
  useEffect(() => {
    const refresh = () => void refetch();
    window.addEventListener("focus", refresh);
    return () => window.removeEventListener("focus", refresh);
  }, [refetch]);
  const rows = data?.pages.flatMap((page) => page.records) ?? [];
  const people = data?.pages[0]?.people ?? [];
  return (
    <div className="space-y-6">
      <p className="text-muted-foreground text-sm">
        Request receipts for everyone in your gateway. Receipts record routing
        and outcomes, without request bodies.
      </p>
      <div className="flex flex-wrap items-end gap-4">
        <div className="min-w-0 space-y-2">
          <label htmlFor="request-person" className="block text-sm font-medium">
            Person
          </label>
          <Select
            value={person || "everyone"}
            onValueChange={(value) =>
              setPerson(value === "everyone" ? "" : value)
            }
          >
            <SelectTrigger id="request-person" className="w-[180px] max-w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="everyone">Everyone</SelectItem>
              {people.map((item) => (
                <SelectItem key={item.id} value={item.id}>
                  {item.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="min-w-0 space-y-2">
          <label htmlFor="request-source" className="block text-sm font-medium">
            Source
          </label>
          <Select value={source} onValueChange={setSource}>
            <SelectTrigger id="request-source" className="w-[210px] max-w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All sources</SelectItem>
              {(data?.pages[0]?.sources ?? []).map((item) => (
                <SelectItem key={item.id} value={item.id}>
                  {item.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        {(person || source !== "all") && (
          <Button
            variant="ghost"
            onClick={() => {
              setPerson("");
              setSource("all");
            }}
          >
            Clear filters
          </Button>
        )}
      </div>
      {isPending ? (
        <div
          aria-label="Loading requests"
          aria-busy="true"
          className="space-y-4"
        >
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-20 w-full" />
          ))}
        </div>
      ) : isError && rows.length === 0 ? (
        <ErrorState
          message="Requests could not be loaded. Try again."
          onRetry={() => void refetch()}
        />
      ) : rows.length === 0 ? (
        <EmptyState
          icon={ReceiptText}
          title={
            person || source !== "all"
              ? "No matching requests"
              : "No requests yet"
          }
          description="Requests from apps and trusted-network clients appear here."
          action={
            person || source !== "all" ? (
              <Button
                variant="outline"
                onClick={() => {
                  setPerson("");
                  setSource("all");
                }}
              >
                Clear filters
              </Button>
            ) : (
              <Button asChild variant="outline">
                <a href="/ui/">Connect an app</a>
              </Button>
            )
          }
        />
      ) : (
        <>
          {isError && !isFetchNextPageError && (
            <ErrorState
              message="These requests could not be refreshed. Try again."
              onRetry={() => void refetch()}
            />
          )}
          <div className="divide-border divide-y">
            {rows.map((row) => (
              <button
                key={row.receipt.request_id}
                type="button"
                aria-label={`Request details: ${requestSummary(row.receipt)} by ${row.person_label}`}
                className="hover:bg-muted/40 focus-visible:outline-ring flex w-full min-w-0 items-center gap-3 py-4 text-left focus-visible:outline-2"
                onClick={() => setRecord(row)}
              >
                <div className="bg-muted hidden size-9 shrink-0 items-center justify-center rounded-full sm:flex">
                  <ReceiptText
                    aria-hidden="true"
                    className="text-muted-foreground size-4"
                  />
                </div>
                <div className="min-w-0 flex-1 space-y-1">
                  <p className="text-sm font-medium break-words">
                    {requestSummary(row.receipt)}
                    {row.receipt.parent_request_id && (
                      <span className="text-muted-foreground ml-2 text-xs font-normal">
                        Internal execution
                      </span>
                    )}
                  </p>
                  <p className="text-muted-foreground truncate text-xs">
                    <span title={`${row.person_label} · ${row.app_label}`}>
                      {row.source?.label ??
                        `${row.person_label} · ${row.app_label}`}
                    </span>
                    {" · "}
                    <span
                      title={
                        row.receipt.resolved_model ||
                        row.receipt.requested_model ||
                        "Model not recorded"
                      }
                    >
                      {row.receipt.resolved_model ||
                        row.receipt.requested_model ||
                        "Model not recorded"}
                    </span>
                    {" · "}
                    <RelativeTime ts={row.created_at} />
                  </p>
                </div>
                <div className="shrink-0 space-y-1 text-right text-xs">
                  <p
                    className={
                      row.receipt.status === "failed"
                        ? "text-destructive"
                        : "text-foreground"
                    }
                  >
                    {outcome(row.receipt.status)}
                  </p>
                  <p className="text-muted-foreground">
                    {duration(row.receipt.latency_ms)}
                  </p>
                </div>
                <ArrowRight
                  aria-hidden="true"
                  className="text-muted-foreground size-4 shrink-0"
                />
              </button>
            ))}
          </div>
          <div className="flex flex-col items-center gap-4">
            {isFetchNextPageError && (
              <ErrorState
                message="More requests could not be loaded. Try again."
                onRetry={() => void fetchNextPage()}
              />
            )}
            {hasNextPage ? (
              <Button
                variant="outline"
                disabled={isFetchingNextPage}
                onClick={() => void fetchNextPage()}
              >
                {isFetchingNextPage ? "Loading…" : "Load more"}
              </Button>
            ) : (
              <p className="text-muted-foreground text-xs">
                {person
                  ? "No more matching requests"
                  : "End of request receipts"}
              </p>
            )}
          </div>
        </>
      )}
      <Sheet
        open={record !== null}
        onOpenChange={(open) => {
          if (!open) setRecord(null);
        }}
      >
        <SheetContent className="w-full overflow-y-auto sm:max-w-lg">
          {record && (
            <>
              <SheetHeader className="pr-14">
                <SheetTitle>{requestSummary(record.receipt)}</SheetTitle>
                <SheetDescription>
                  {outcome(record.receipt.status)} · {record.person_label}
                </SheetDescription>
              </SheetHeader>
              <div className="min-w-0 space-y-6 px-4 pb-8">
                <dl className="space-y-4 text-sm">
                  {[
                    ["Person", record.person_label],
                    ["App", record.app_label],
                    ["Source", record.source?.label ?? record.app_label],
                    [
                      "Model",
                      record.receipt.resolved_model ||
                        record.receipt.requested_model ||
                        "Not recorded",
                    ],
                    ["Time taken", duration(record.receipt.latency_ms)],
                  ].map(([label, value]) => (
                    <div key={label}>
                      <dt className="text-muted-foreground">{label}</dt>
                      <dd className="mt-1 break-words">{value}</dd>
                    </div>
                  ))}
                  <div>
                    <dt className="text-muted-foreground">When</dt>
                    <dd className="mt-1">
                      <RelativeTime ts={record.created_at} />
                    </dd>
                  </div>
                </dl>
                {!!record.receipt.warnings?.length && (
                  <div className="space-y-2 text-sm">
                    <p className="font-medium">What to know</p>
                    {record.receipt.warnings.map((warning, i) => (
                      <p key={i} className="text-muted-foreground break-words">
                        {warning}
                      </p>
                    ))}
                  </div>
                )}
                <details>
                  <summary className="cursor-pointer text-sm font-medium">
                    Technical details
                  </summary>
                  <div className="mt-4">
                    <pre
                      tabIndex={0}
                      aria-label="Technical request details"
                      className="bg-muted focus-visible:outline-ring overflow-x-auto rounded-lg p-4 font-mono text-xs leading-relaxed focus-visible:outline-2"
                    >
                      <code>{JSON.stringify(record.receipt, null, 2)}</code>
                    </pre>
                  </div>
                </details>
              </div>
            </>
          )}
        </SheetContent>
      </Sheet>
    </div>
  );
}
