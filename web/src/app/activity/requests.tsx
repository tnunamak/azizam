"use client";

import { useEffect, useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { ArrowRight, ReceiptText } from "lucide-react";
import { Button } from "@/components/ui/button";
import { CopyButton } from "@/components/ui/copy-button";
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
import { EmptyState } from "@/components/product/empty-state";
import { Table, TableBody, TableRow, TableCell } from "@/components/ui/table";
import { Card } from "@/components/ui/card";
import { Callout, StateBadge, StatusLine } from "@/components/product/status-ui";
import { RelativeTime } from "@/components/product";
import { errorCopy } from "@/lib/error-copy";
import { receiptSelectionKeys } from "@/lib/receipt-selection";
import { useModelName } from "@/lib/use-model-name";

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
  if (/\bspeech\b/.test(intent)) return "Speech request";
  if (/\bimage\b/.test(intent)) return "Images request";
  if (/\bdecision\b|\bdecide\b/.test(intent)) return "Decisions request";
  if (/\bembedding\b|\bembed\b/.test(intent)) return "Embeddings request";
  return intent.charAt(0).toUpperCase() + intent.slice(1);
}
function outcome(status?: string) {
  return (
    (
      {
        success: "Completed",
        succeeded: "Completed",
        ok: "Completed",
        failed: "Failed",
        error: "Failed",
        backend_failed: "Failed",
        rejected: "Rejected",
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
function outcomeTone(status?: string) {
  if (
    [
      "success",
      "succeeded",
      "ok",
      "accepted",
      "running",
      "in_progress",
    ].includes(status ?? "")
  )
    return "text-foreground";
  if (
    ["failed", "error", "backend_failed", "rejected", "timed_out"].includes(
      status ?? "",
    )
  )
    return "text-destructive";
  return "text-muted-foreground";
}
function outcomeNeedsAttention(status?: string) {
  return [
    "failed",
    "error",
    "backend_failed",
    "rejected",
    "timed_out",
  ].includes(status ?? "");
}
function outcomeSummary(receipt: Receipt) {
  const label = outcome(receipt.status);
  if (label === "Completed") return "The gateway completed this request.";
  if (label === "Rejected") return "The gateway rejected this request.";
  if (label === "Failed") return "The request failed.";
  if (label === "Timed out")
    return "The request timed out before a result was recorded.";
  if (label === "In progress")
    return "The request was still running when this receipt was recorded.";
  if (label === "Accepted")
    return "The gateway accepted this request for processing.";
  if (label === "Cancelled")
    return "The request was cancelled before completion.";
  if (label === "Disconnected")
    return "The connection ended before completion was recorded.";
  return "The receipt does not include a final outcome.";
}
function outcomeCause(receipt: Receipt) {
  const statusLabel = outcome(receipt.status);
  if (
    !["Failed", "Rejected", "Timed out"].includes(
      statusLabel,
    )
  )
    return null;
  const metadata = object(receipt.metadata);
  if (statusLabel === "Failed") {
    const stage = receiptString(receipt.stage ?? metadata.stage);
    const code = receiptString(receipt.error_code ?? metadata.error_code);
    return [stage ? `Stage: ${stage}` : null, code ? `Error code: ${code}` : null]
      .filter(Boolean).join(" · ") || null;
  }
  const status = number(metadata.status_code ?? receipt.status_code);
  if (status && status >= 400) return errorCopy({ status }).message;
  const warning = receipt.warnings?.find((item) => item.trim());
  return warning ?? null;
}
function nextAction(receipt: Receipt) {
  const status = receipt.status ?? "";
  if (outcomeNeedsAttention(status))
    return "Open Technical details for the recorded status and warnings.";
  if (["stale_incomplete", "incomplete", ""].includes(status))
    return "Check for a newer receipt before treating this request as complete.";
  return null;
}
function object(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : {};
}
function number(value: unknown): number | null {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}
function receiptString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}
function declaredFallback(receipt: Receipt) {
  const metadata = object(receipt.metadata);
  return receiptString(receipt.fallback_model ?? metadata.fallback_model);
}
function duration(ms?: number | null) {
  if (typeof ms !== "number" || !Number.isFinite(ms)) return "";
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export function Requests() {
  const nameModel = useModelName();
  const [person, setPerson] = useState("");
  const [source, setSource] = useState("all");
  const [selectedReceiptKey, setSelectedReceiptKey] = useState<string | null>(null);
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
  const keys = receiptSelectionKeys(
    rows.map((row) => row.receipt),
    rows.map((row) => ({
      person_label: row.person_label,
      app_label: row.app_label,
      created_at: row.created_at,
      source: row.source,
    })),
  );
  const people = data?.pages[0]?.people ?? [];
  const record = rows.find((_, index) => keys[index] === selectedReceiptKey) ?? null;
  useEffect(() => {
    if (selectedReceiptKey !== null && record === null) setSelectedReceiptKey(null);
  }, [selectedReceiptKey, record]);
  return (
    <div className="space-y-6">
      <p className="text-muted-foreground text-meta">
        Receipts show outcome, route and timing without request bodies.
      </p>
      <div className="flex flex-wrap items-end gap-4">
        <div className="min-w-0 space-y-2">
          <label htmlFor="request-person" className="sr-only">
            Person
          </label>
          <Select
            value={person || "everyone"}
            onValueChange={(value) =>
              setPerson(value === "everyone" ? "" : value)
            }
          >
            <SelectTrigger id="request-person" size="sm" className="w-44 max-w-full">
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
          <label htmlFor="request-source" className="sr-only">
            Source
          </label>
          <Select value={source} onValueChange={setSource}>
            <SelectTrigger id="request-source" size="sm" className="w-52 max-w-full">
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
          <StatusLine tone="unknown" text="Loading requests" compact />
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-20 w-full" />
          ))}
        </div>
      ) : isError && rows.length === 0 ? (
        <Callout
          as="div"
          severity="critical"
          title="Couldn't load requests"
          headerAction={
            <Button variant="outline" onClick={() => void refetch()}>
              Try again
            </Button>
          }
        />
      ) : rows.length === 0 ? (
        <EmptyState
          icon={ReceiptText}
          title={
            person || source !== "all"
              ? "No matching requests"
              : "No request receipts yet"
          }
          description="Requests from apps and devices on your trusted network appear here."
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
            <Callout
              as="div"
              severity="critical"
              title="Couldn't refresh requests"
              headerAction={
                <Button variant="outline" onClick={() => void refetch()}>
                  Try again
                </Button>
              }
            >
              <p>The requests already loaded are still shown.</p>
            </Callout>
          )}
          <Card>
            <Table dense aria-label="Request receipts" className="table-fixed">
              <TableBody>
                {rows.map((row, index) => (
                  <TableRow key={keys[index]}>
                    <TableCell className="p-0">
                      <button
                        type="button"
                        aria-label={`Request details: ${requestSummary(row.receipt)} by ${row.person_label}`}
                        className="focus-visible:outline-ring flex min-h-(--row-1-dense) w-full min-w-0 items-center gap-3 px-3 text-left focus-visible:outline-2 max-sm:min-h-(--row-phone-2)"
                        onClick={() => setSelectedReceiptKey(keys[index])}
                      >
                        <div className="flex min-w-0 flex-1 flex-col sm:flex-row sm:items-center sm:gap-3">
                          <p className="min-w-0 truncate text-body font-medium sm:shrink-0">
                            {requestSummary(row.receipt)}
                            {row.receipt.parent_request_id && (
                              <span className="text-muted-foreground ml-2 text-meta font-normal">
                                Internal execution
                              </span>
                            )}
                          </p>
                          <p className="text-muted-foreground truncate text-meta">
                            <span title={`${row.person_label} · ${row.app_label}`}>
                              {row.source?.label ?? `${row.person_label} · ${row.app_label}`}
                            </span>
                            {row.receipt.resolved_model || row.receipt.requested_model ? (
                              <>
                                {" · "}
                                {nameModel(
                                  row.receipt.resolved_model ||
                                    row.receipt.requested_model ||
                                    "",
                                )}
                              </>
                            ) : null}
                            {row.created_at ? <> · <RelativeTime ts={row.created_at} /></> : null}
                          </p>
                        </div>
                        <div className="flex shrink-0 flex-col items-end sm:flex-row sm:items-center sm:gap-3">
                          {["failed", "error", "backend_failed"].includes(row.receipt.status ?? "") ? (
                            <StateBadge state="down" word={outcome(row.receipt.status)} />
                          ) : ["running", "in_progress"].includes(row.receipt.status ?? "") ? (
                            <StateBadge state="busy" word={outcome(row.receipt.status)} />
                          ) : row.receipt.status === "rejected" ? (
                            <StateBadge state="degraded" word={outcome(row.receipt.status)} />
                          ) : (
                            <span className="text-foreground">{outcome(row.receipt.status)}</span>
                          )}
                          <p className="text-muted-foreground figures">
                            {duration(row.receipt.latency_ms)}
                          </p>
                        </div>
                        <ArrowRight
                          aria-hidden="true"
                          className="text-muted-foreground size-4 shrink-0"
                        />
                      </button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Card>
          <div className="flex flex-col items-center gap-4">
            {isFetchNextPageError && (
              <Callout
                as="div"
                severity="critical"
                title="Couldn't load more requests"
                headerAction={
                  <Button variant="outline" onClick={() => void fetchNextPage()}>
                    Try again
                  </Button>
                }
              >
                <p>The requests already loaded are still shown.</p>
              </Callout>
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
              <p className="text-muted-foreground text-meta">
                {person || source !== "all"
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
          if (!open) setSelectedReceiptKey(null);
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
                <div className="rounded-panel border p-4 text-meta">
                  <p
                    className={`font-medium ${outcomeTone(record.receipt.status)}`}
                  >
                    {outcome(record.receipt.status)}
                  </p>
                  <p className="text-muted-foreground mt-2">
                    {outcomeSummary(record.receipt)}
                  </p>
                  {outcomeCause(record.receipt) ? (
                    <p className="text-muted-foreground mt-2">
                      {outcomeCause(record.receipt)}
                    </p>
                  ) : null}
                  {nextAction(record.receipt) ? (
                    <p className="text-muted-foreground mt-2">
                      {nextAction(record.receipt)}
                    </p>
                  ) : null}
                </div>
                <dl className="space-y-4 text-meta">
                  {[
                    ["Request ID", record.receipt.request_id],
                    ["Person", record.person_label],
                    ["App", record.app_label],
                    ["Source", record.source?.label ?? record.app_label],
                    [
                      "Requested model",
                      record.receipt.requested_model
                        ? nameModel(record.receipt.requested_model)
                        : "",
                    ],
                    [
                      "Served model",
                      record.receipt.resolved_model
                        ? nameModel(record.receipt.resolved_model)
                        : "",
                    ],
                    ...(declaredFallback(record.receipt)
                      ? ([
                          [
                            "Declared fallback",
                            declaredFallback(record.receipt) as string,
                          ],
                        ] as const)
                      : []),
                    ["Time taken", duration(record.receipt.latency_ms)],
                  ].filter(([, value]) => value != null && value !== "").map(([label, value]) => (
                    <div key={label}>
                      <dt className="text-muted-foreground">{label}</dt>
                      <dd className="mt-1 flex min-w-0 items-center gap-2 break-words">
                        <span className="min-w-0 break-all">{value}</span>
                        {label === "Request ID" ? (
                          <CopyButton
                            value={String(value)}
                            aria-label="Copy request ID"
                          />
                        ) : null}
                      </dd>
                    </div>
                  ))}
                  <div>
                    <dt className="text-muted-foreground">When</dt>
                    <dd className="mt-1">
                      <RelativeTime ts={record.created_at} />
                    </dd>
                  </div>
                </dl>
                {outcomeNeedsAttention(record.receipt.status) &&
                  !!record.receipt.warnings?.length && (
                    <div className="space-y-2 text-meta">
                      <p className="font-medium">What to know</p>
                      {record.receipt.warnings.map((warning, i) => (
                        <p
                          key={i}
                          className="text-muted-foreground break-words"
                        >
                          {warning}
                        </p>
                      ))}
                    </div>
                  )}
                <details>
                  <summary className="cursor-pointer text-meta font-medium">
                    Technical details
                  </summary>
                  <div className="mt-4">
                    <pre
                      tabIndex={0}
                      aria-label="Technical request details"
                      className="bg-muted focus-visible:outline-ring overflow-x-auto rounded-panel p-4 font-mono text-meta leading-relaxed focus-visible:outline-2"
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
