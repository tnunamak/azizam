"use client";

import Link from "next/link";
import { type ReactNode, useEffect, useId, useRef, useState } from "react";
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
import { Callout, DelayedSpinner, StateBadge } from "@/components/product/status-ui";
import { formatTimestamp } from "@/lib/format";
import { requestCapability } from "@/lib/capability-state";
import { receiptSelectionKeys } from "@/lib/receipt-selection";
import { useModelName } from "@/lib/use-model-name";
import { technicalModelDetails } from "@/lib/model-name";
import { useViewer } from "@/components/shell/session";

const requestRowLayout = "flex min-h-(--row-2) w-full min-w-0 items-center gap-3 px-4 py-2 text-left max-sm:min-h-(--row-phone-2)";

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

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const isPickList = (value: unknown): value is { id: string; label: string }[] =>
  Array.isArray(value) && value.every((item) =>
    isObject(item) && typeof item.id === "string" && typeof item.label === "string",
  );

// Rows are dereferenced after the query succeeds, so a malformed page must fail
// the query, where the retry callout can reach it.
function isRequestPage(payload: unknown): payload is RequestPage {
  return (
    isObject(payload) &&
    Array.isArray(payload.records) &&
    payload.records.every((row) => isObject(row) && isObject(row.receipt)) &&
    isPickList(payload.people) &&
    (payload.sources === undefined || isPickList(payload.sources)) &&
    (payload.next_cursor === null || typeof payload.next_cursor === "string")
  );
}

// The title of a request in the words people use, never the stored intent.
const requestNames: Record<string, string> = {
  chat: "Chat",
  image: "Image",
  video: "Video",
  speech: "Voice",
  transcribe: "Transcription",
  music: "Music",
  sfx: "Sound effect",
  audio: "Audio",
  decide: "Decision",
  embed: "Embedding",
  rerank: "Rerank",
};
function requestSummary(receipt: Receipt) {
  const capability = requestCapability(
    receipt.intent ?? (receipt as { capability?: string }).capability,
    receipt.path,
  );
  return (capability && requestNames[capability]) || "AI request";
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
/** A refused or timed-out request keeps its recorded HTTP reason, in plain words. */
function outcomeCause(receipt: Receipt) {
  if (!["Rejected", "Timed out"].includes(outcome(receipt.status))) return null;
  const metadata =
    typeof receipt.metadata === "object" && receipt.metadata !== null
      ? (receipt.metadata as Record<string, unknown>)
      : {};
  const status = Number(metadata.status_code ?? receipt.status_code);
  if (status === 401)
    return "The sign-in or app-key check refused this request. Check the app's access key for this person, then try again.";
  if (status === 403)
    return "This request was rejected because it did not have permission. Check this person's access plan and the app's access.";
  if (status === 429) return "A request limit refused this request. The receipt does not identify which limit. Wait before trying again; if it happens again, ask the owner to check the request limits.";
  if (status >= 500 && status < 600)
    return "An error prevented this request from completing. No refusal rule was recorded. Try again; if it happens again, open Technical details and share the request number with the owner.";
  return outcome(receipt.status) === "Rejected"
    ? "The receipt does not identify the rule that refused this request. Open Technical details and share the request number with the owner before trying again."
    : null;
}
function outcomeSummary(receipt: Receipt) {
  const label = outcome(receipt.status);
  if (label === "Completed") return "";
  if (label === "Rejected") {
    const metadata = isObject(receipt.metadata) ? receipt.metadata : {};
    return Number(metadata.status_code ?? receipt.status_code) === 403
      ? "Access check: the receipt does not identify the specific rule."
      : "This request was not allowed.";
  }
  if (label === "Failed") return "We couldn't tell where this stopped. Try again.";
  // Only media lands in Library; a chat or embedding result cannot be found there.
  const media = ["image", "video", "speech", "sfx", "music", "audio"].includes(
    requestCapability(receipt.intent ?? (receipt as { capability?: string }).capability, receipt.path) ?? "",
  );
  const next = media ? "Check Library for a result before trying again." : "Try again.";
  if (label === "Timed out")
    return `The request timed out before we recorded a result. ${next}`;
  if (label === "In progress")
    return "The request was still running when this receipt was recorded.";
  if (label === "Accepted")
    return "This request was accepted. Its result was not recorded here.";
  if (label === "Cancelled")
    return "The request was cancelled before completion.";
  if (label === "Disconnected")
    return `The connection ended before we recorded a result. ${next}`;
  return "We couldn't tell where this stopped. Check for a newer receipt, then try again.";
}
function duration(ms?: number | null) {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms < 0) return "Duration not recorded";
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export function Requests() {
  const rowId = useId();
  const filterRef = useRef<HTMLButtonElement>(null);
  const openerRef = useRef<HTMLButtonElement | null>(null);
  const pageButtonRef = useRef<HTMLButtonElement | null>(null);
  const endRef = useRef<HTMLParagraphElement>(null);
  const nameModel = useModelName();
  const [person, setPerson] = useState("");
  const [source, setSource] = useState("all");
  const [selectedReceiptKey, setSelectedReceiptKey] = useState<string | null>(null);
  const {
    data,
    isPending,
    isFetching,
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
      const payload: unknown = await response.json();
      if (!isRequestPage(payload)) {
        throw new Error("Requests response is not in the expected shape.");
      }
      return payload;
    },
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  });
  useEffect(() => {
    const refresh = () => void refetch();
    window.addEventListener("focus", refresh);
    return () => window.removeEventListener("focus", refresh);
  }, [refetch]);
  const rows = (data?.pages.flatMap((page) => page.records) ?? [])
    .sort((a, b) => b.created_at - a.created_at);
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
  useEffect(() => {
    if (isFetchingNextPage || !pageButtonRef.current) return;
    const button = pageButtonRef.current;
    if (!hasNextPage && (document.activeElement === button ||
      (!button.isConnected && document.activeElement === document.body))) {
      endRef.current?.focus();
    }
    pageButtonRef.current = null;
  }, [hasNextPage, isFetchingNextPage]);
  function clearFilters() {
    setPerson("");
    setSource("all");
    filterRef.current?.focus();
  }
  function loadMore(button: HTMLButtonElement) {
    if (isFetchingNextPage) return;
    pageButtonRef.current = button;
    void fetchNextPage();
  }
  const status = isPending ? "Loading requests" : isFetchingNextPage ? "Loading more requests"
    : isFetching ? "Refreshing requests" : isError ? ""
    : `${rows.length} ${rows.length === 1 ? "request" : "requests"} loaded. ${hasNextPage ? "More requests are available." : person || source !== "all" ? "No more matching requests." : "End of request receipts."}`;
  return (
    <div className="space-y-6 motion-reduce:[&_*]:animate-none motion-reduce:[&_*]:transition-none">
      <ResultsStatus message={status} label="Request updates" />
      <p className="text-muted-foreground text-meta">
        Receipts show the outcome, model and time. They do not include what was asked.
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
            <SelectTrigger ref={filterRef} id="request-person" size="sm" className="min-h-6 min-w-6 w-44 max-w-full">
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
            <SelectTrigger id="request-source" size="sm" className="min-h-6 min-w-6 w-52 max-w-full">
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
            onClick={clearFilters}
          >
            Clear filters
          </Button>
        )}
      </div>
      {isPending ? (
        <div aria-label="Loading requests" aria-busy="true" className="relative">
          <span className="absolute right-4 top-3 z-10"><DelayedSpinner /></span>
          <Card aria-hidden="true" className="gap-0 overflow-hidden p-0! max-sm:p-0!">
            <Table dense className="table-fixed">
              <TableBody>
                {[0, 1, 2, 3].map((i) => (
                  <TableRow key={i}>
                    <TableCell className="px-0! py-0!">
                      <div className={requestRowLayout}>
                        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                          <Skeleton className="h-5 w-2/3 motion-reduce:animate-none" />
                          <Skeleton className="h-4 w-1/2 motion-reduce:animate-none" />
                        </div>
                        <div className="flex min-w-0 max-w-1/2 shrink-0 flex-col items-end gap-0.5">
                          <Skeleton className="h-5 w-16 motion-reduce:animate-none" />
                          <Skeleton className="h-4 w-24 motion-reduce:animate-none" />
                        </div>
                        <span className="size-4 shrink-0" />
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Card>
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
        >
          <p>
            The request list could not be loaded. We could not
            identify the cause. Try again.
          </p>
        </Callout>
      ) : rows.length === 0 ? (
        <EmptyState
          icon={ReceiptText}
          title={
            person || source !== "all"
              ? "No matching requests"
              : "No request receipts yet"
          }
          description={
            person || source !== "all"
              ? "Receipts show request outcomes from your apps and devices; none match these filters."
              : "Requests from apps and devices on your trusted network appear here."
          }
          action={
            person || source !== "all" ? (
              <Button
                size="sm"
                onClick={clearFilters}
              >
                Clear filters and show all requests
              </Button>
            ) : (
              <Button asChild size="sm">
                <Link prefetch={false} href="/">Go to Home</Link>
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
              <p>
                The request list could not be refreshed. We could
                not identify the cause. The requests already loaded are still
                shown.
              </p>
            </Callout>
          )}
          <Card className="gap-0 overflow-hidden p-0! max-sm:p-0!">
            <Table dense aria-label="Request receipts" className="table-fixed">
              <thead className="sr-only"><tr><th scope="col">Request receipt</th></tr></thead>
              <TableBody>
                {rows.map((row, index) => (
                  <TableRow key={keys[index]}>
                    <TableCell className="px-0! py-0!">
                      <button
                        type="button"
                        aria-label={`Request details: ${requestSummary(row.receipt)} by ${row.person_label}`}
                        aria-describedby={`${rowId}-${index}-context ${rowId}-${index}-outcome`}
                        className={`${requestRowLayout} focus-visible:outline-ring hover:[&_p:first-child]:underline focus-visible:outline-2 focus-visible:-outline-offset-2`}
                        onClick={(event) => {
                          openerRef.current = event.currentTarget;
                          setSelectedReceiptKey(keys[index]);
                        }}
                      >
                        <div id={`${rowId}-${index}-context`} className="flex min-w-0 flex-1 flex-col gap-0.5">
                          <p className="min-w-0 truncate text-body font-medium">
                            {requestSummary(row.receipt)}
                            {row.receipt.parent_request_id && (
                              <span className="text-muted-foreground block text-meta font-normal">
                                Part of another request
                              </span>
                            )}
                          </p>
                          <p className="text-muted-foreground truncate text-meta">
                            <span title={`${row.person_label} · ${row.app_label}`}>
                              {row.person_label} · {row.app_label}
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
                          </p>
                        </div>
                        <div id={`${rowId}-${index}-outcome`} className="flex min-w-0 max-w-1/2 shrink-0 flex-col items-end gap-0.5">
                          {["failed", "error", "backend_failed"].includes(row.receipt.status ?? "") ? (
                            <StateBadge state="down" word={outcome(row.receipt.status)} />
                          ) : ["accepted", "running", "in_progress"].includes(row.receipt.status ?? "") ? (
                            <StateBadge state="busy" word={outcome(row.receipt.status)} />
                          ) : ["rejected", "timed_out", "disconnected"].includes(row.receipt.status ?? "") ? (
                            <StateBadge state="degraded" word={outcome(row.receipt.status)} />
                          ) : ["success", "succeeded", "ok"].includes(row.receipt.status ?? "") ? (
                            <span className="text-foreground">{outcome(row.receipt.status)}</span>
                          ) : (
                            <StateBadge state="unknown" word={outcome(row.receipt.status)} />
                          )}
                          <p className="text-muted-foreground figures max-w-full truncate text-meta">
                            <time {...formatTimestamp(row.created_at || null)} />
                            {" · "}
                            {duration(row.receipt.latency_ms) || "Duration not recorded"}
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
                  <Button variant="outline" aria-disabled={isFetchingNextPage} onClick={(event) => loadMore(event.currentTarget)}>
                    Try again
                  </Button>
                }
              >
                <p>
                The request list could not be refreshed. We could
                not identify the cause. The requests already loaded are still
                shown.
              </p>
              </Callout>
            )}
            {hasNextPage ? (
              <Button
                variant="outline"
                aria-disabled={isFetchingNextPage}
                onClick={(event) => loadMore(event.currentTarget)}
              >
                {isFetchingNextPage ? "Loading…" : "Load more"}
              </Button>
            ) : (
              <p ref={endRef} tabIndex={-1} className="text-muted-foreground text-meta focus-visible:outline-ring focus-visible:outline-2">
                {person || source !== "all"
                  ? "No more matching requests"
                  : "End of request receipts"}
              </p>
            )}
          </div>
        </>
      )}
      <RequestReceiptSheet record={record} returnFocus={() => {
        (openerRef.current?.isConnected ? openerRef.current : filterRef.current)?.focus();
      }} onOpenChange={(open) => {
        if (!open) setSelectedReceiptKey(null);
      }} />
    </div>
  );
}

/** Keep the same polite region mounted while a query changes state. */
export function ResultsStatus({ message, label }: { message: string; label?: string }) {
  return <p role="status" aria-label={label} aria-live="polite" aria-atomic="true"
    className="sr-only">{message}</p>;
}

/** Shared receipt presentation for Activity and Usage. Raw routing stays in details. */
export function RequestReceiptSheet({ record, onOpenChange, title, description, cost, returnFocus }: {
  record: (Omit<RequestRecord, "created_at"> & { created_at: number | string | null }) | null;
  onOpenChange: (open: boolean) => void;
  title?: string;
  description?: string;
  cost?: ReactNode;
  returnFocus?: () => void;
}) {
  const nameModel = useModelName();
  const owner = ["owner", "superadmin"].includes(useViewer().role);
  const requested = record?.receipt.requested_model;
  const served = record?.receipt.resolved_model;
  // A name that points at a model (`fast` -> `default-fast`) is the normal case. The
  // banner is for a model that took over: the gateway recorded a fallback.
  const different = Boolean(requested && served && requested !== served);
  const fallbackReasons = record?.receipt.fallback_reasons;
  const fellBack = different && (Boolean(record?.receipt.fallback_model) || (Array.isArray(fallbackReasons) && fallbackReasons.length > 0));
  return (
      <Sheet
        open={record !== null}
        onOpenChange={onOpenChange}
      >
        <SheetContent
          onCloseAutoFocus={returnFocus ? (event) => {
            event.preventDefault();
            returnFocus();
          } : undefined}
          className="w-full overflow-y-auto sm:max-w-lg motion-reduce:animate-none motion-reduce:transition-none motion-reduce:[&_*]:animate-none motion-reduce:[&_*]:transition-none [&_button]:min-h-6 [&_button]:min-w-6"
        >
          {record && (
            <>
              <SheetHeader className="pr-14">
                <SheetTitle>{title ?? requestSummary(record.receipt)}</SheetTitle>
                <SheetDescription>
                  {description ?? "Request receipt"}
                </SheetDescription>
              </SheetHeader>
              <div className="min-w-0 space-y-6 px-4 pb-8">
                <section className="space-y-3">
                  <h3 className="text-meta font-medium">Request</h3>
                  <dl className="space-y-4 text-meta">
                    {[["Person", record.person_label], ["App", record.app_label]].map(([label, value]) => (
                      <div key={label}>
                        <dt className="text-muted-foreground">{label}</dt>
                        <dd className="mt-1 break-words">{value || "Not recorded"}</dd>
                      </div>
                    ))}
                  </dl>
                </section>
                <section className="space-y-3">
                  <h3 className="text-meta font-medium">Model and where it ran</h3>
                {fellBack && <p className="text-meta">A different model was used.</p>}
                <dl className="space-y-4 text-meta">
                  {[
                    ...(fellBack ? [
                      ["Requested model", nameModel(requested!)],
                      ["Model used", nameModel(served!)],
                    ] : different ? [
                      ["Name used", nameModel(requested!)],
                      ["Model", nameModel(served!)],
                    ] : [["Model", served || requested ? nameModel((served || requested)!) : "Model not recorded"]]),
                  ].filter(([, value]) => value != null && value !== "").map(([label, value]) => (
                    <div key={label}>
                      <dt className="text-muted-foreground">{label}</dt>
                      <dd className="mt-1 flex min-w-0 items-center gap-2 break-words">
                        <span className="min-w-0 break-all">{value}</span>
                      </dd>
                    </div>
                  ))}
                  <div>
                    <dt className="text-muted-foreground">Where it ran</dt>
                    <dd className="mt-1">Open Technical details to see how this request was handled, if recorded.</dd>
                  </div>
                </dl>
                </section>
                <section className="space-y-3">
                  <h3 className="text-meta font-medium">Time and cost</h3>
                  <dl className="space-y-4 text-meta">
                  <div>
                    <dt className="text-muted-foreground">Time taken</dt>
                    <dd className="mt-1">{duration(record.receipt.latency_ms)}</dd>
                  </div>
                  <div><dt className="text-muted-foreground">Spend</dt><dd className="mt-1">{cost ?? "Cost not recorded"}</dd></div>
                  <div>
                    <dt className="text-muted-foreground">When</dt>
                    <dd className="mt-1">
                      <time {...formatTimestamp(record.created_at || null)} />
                    </dd>
                  </div>
                </dl>
                </section>
                <div className="rounded-panel border p-4 text-meta">
                  <h3 className="mb-3 font-medium">Outcome</h3>
                  <p
                    className={`font-medium ${outcomeTone(record.receipt.status)}`}
                  >
                    {outcome(record.receipt.status)}
                  </p>
                  {outcomeSummary(record.receipt) && (
                    <p className="text-muted-foreground mt-2">
                      {outcomeSummary(record.receipt)}
                    </p>
                  )}
                  {outcomeCause(record.receipt) ? (
                    <p className="text-muted-foreground mt-2">
                      {outcomeCause(record.receipt)}
                    </p>
                  ) : null}
                </div>
                <details>
                  <summary className="min-h-6 min-w-6 cursor-pointer content-center text-meta font-medium focus-visible:outline-ring focus-visible:outline-2">
                    Technical details
                  </summary>
                  <div className="mt-4 space-y-4">
                    {record.receipt.parent_request_id && <p>Part of another request</p>}
                    {record.receipt.request_id && <p className="flex items-center gap-2 break-all">Request ID: {record.receipt.request_id}<CopyButton value={record.receipt.request_id} aria-label="Copy request ID" /></p>}
                    <pre
                      tabIndex={0}
                      aria-label="Technical request details"
                      className="bg-muted focus-visible:outline-ring overflow-x-auto rounded-panel p-4 font-mono text-meta leading-relaxed focus-visible:outline-2"
                    >
                      <code>{JSON.stringify(technicalModelDetails(record.receipt, owner), null, 2)}</code>
                    </pre>
                  </div>
                </details>
              </div>
            </>
          )}
        </SheetContent>
      </Sheet>
  );
}
