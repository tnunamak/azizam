"use client";

import Link from "next/link";
import { useEffect, useId, useRef, useState } from "react";
import { Requests, ResultsStatus } from "./requests";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { History, ChevronRight } from "lucide-react";
import { api, type AuditEntry } from "@/lib/api";
import { auditDescription } from "@/lib/audit-description";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { PageHeader } from "@/components/product/page-header";
import { EmptyState } from "@/components/product/empty-state";
import { Callout, DelayedSpinner, StatusLine } from "@/components/product/status-ui";
import { formatTimestamp } from "@/lib/format";

const auditRowLayout = "flex w-full items-center gap-4 py-4 text-left";

const categories = [
  { value: "", label: "All changes" },
  { value: "app.", label: "Apps" },
  { value: "credential.", label: "Keys" },
  { value: "profile.", label: "Access plans" },
  { value: "operator.", label: "Shared settings" },
  { value: "runtime_profile_mapping.", label: "Access assignments" },
];

function auditSummary(entry: AuditEntry) {
  if (entry.action === "media.library.everyone")
    return "Viewed everyone’s media library";
  if (entry.action === "usage.summary.everyone")
    return "Viewed everyone’s usage";
  if (entry.action === "operator.instance.update")
    return "Updated shared settings";
  if (entry.action === "operator.default_access_plan.update")
    return "Updated the default access plan";
  return auditDescription(entry);
}
function changedName(entry: AuditEntry) {
  if (typeof entry.metadata?.name === "string") return entry.metadata.name;
  const labels: Record<string, string> = {
    credential: "Key",
    profile: "Access plan",
    user: "Person",
    operator_setting: "Shared settings",
    service: "Engine",
    runtime_profile_mapping: "Access assignment",
    app: "App",
  };
  return labels[entry.target_kind ?? ""] ?? "Item";
}

function personLabel(person: {
  display_name?: string | null;
  name?: string | null;
  email?: string | null;
}) {
  return person.display_name || person.name || person.email || "Person";
}

function activityActorName(
  label: string,
  actorKind: AuditEntry["actor_kind"] = null,
) {
  if (actorKind === "service_account") return "Automation";
  if (actorKind === "user" || actorKind === "admin") {
    return label.includes("@") ? "Person" : label;
  }
  return "System";
}

export default function ActivityPage() {
  const rowId = useId();
  const filterRef = useRef<HTMLButtonElement>(null);
  const openerRef = useRef<HTMLButtonElement | null>(null);
  const pageButtonRef = useRef<HTMLButtonElement | null>(null);
  const endRef = useRef<HTMLParagraphElement>(null);
  const [category, setCategory] = useState("");
  const [showAccessAudit, setShowAccessAudit] = useState(false);
  const [showAutomated, setShowAutomated] = useState(false);
  const [actor, setActor] = useState("");
  function clearFilters() {
    setCategory("");
    setActor("");
    filterRef.current?.focus();
  }
  const {
    data: people,
    isPending: peoplePending,
    isError: peopleError,
    refetch: refetchPeople,
  } = useQuery({
    queryKey: ["users"],
    queryFn: api.users.list,
  });
  const [entry, setEntry] = useState<AuditEntry | null>(null);
  // People supply names beyond the audit window; recent events also supply
  // labels for system actors. IDs are only used as server filter values.
  const { data: optionsData } = useQuery({
    queryKey: ["activity", "actors", showAccessAudit, showAutomated],
    queryFn: () =>
      api.audit.list({
        limit: 100,
        exclude_access_audit: !showAccessAudit,
        show_automated: showAutomated,
      }),
  });
  const {
    data: auditData,
    refetch: refetchAudit,
    isFetching: auditFetching,
    isError: auditError,
    isFetchNextPageError: nextPageError,
    isPending: auditPending,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage: fetchingNextPage,
  } = useInfiniteQuery({
    queryKey: ["activity", "audit", category, actor, showAccessAudit, showAutomated],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      api.audit.list({
        limit: 50,
        action_prefix: category || undefined,
        actor_id: actor || undefined,
        exclude_access_audit: !showAccessAudit,
        show_automated: showAutomated,
        before: pageParam,
      }),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  });
  const entries = auditData?.pages.flatMap((page) => page.records) ?? [];
  // One name per person: the people list wins, so the selector, the rows and
  // the sheet never disagree about who made a change.
  const personNames = new Map(
    (people ?? []).map((person) => [person.id, personLabel(person)] as const),
  );
  const actorName = (row: AuditEntry) =>
    personNames.get(row.actor_id) ??
    activityActorName(row.actor_label, row.actor_kind);
  const actors = Array.from(
    new Map([
      ...(people ?? []).map((person) => [
        person.id,
        personLabel(person),
      ] as const),
      ...(optionsData?.records ?? [])
        .filter((row) => !people?.some((person) => person.id === row.actor_id))
        .map((row) => [
          row.actor_id,
          activityActorName(row.actor_label, row.actor_kind),
        ] as const),
    ]),
  );
  const automatedCount =
    auditData?.pages[0]?.automated_count ?? optionsData?.automated_count ?? 0;
  useEffect(() => {
    if (fetchingNextPage || !pageButtonRef.current) return;
    const button = pageButtonRef.current;
    if (!hasNextPage && (document.activeElement === button ||
      (!button.isConnected && document.activeElement === document.body))) {
      endRef.current?.focus();
    }
    pageButtonRef.current = null;
  }, [hasNextPage, fetchingNextPage]);
  function loadMore(button: HTMLButtonElement) {
    if (fetchingNextPage) return;
    pageButtonRef.current = button;
    void fetchNextPage();
  }
  const status = auditPending ? "Loading changes" : fetchingNextPage ? "Loading more changes"
    : auditFetching ? "Refreshing changes" : auditError ? ""
    : `${entries.length} ${entries.length === 1 ? "change" : "changes"} loaded. ${hasNextPage ? "More changes are available." : category || actor ? "No more matching changes." : "End of changes."}`;
  return (
    <div className="space-y-8 motion-reduce:[&_*]:animate-none motion-reduce:[&_*]:transition-none">
      <PageHeader title="Activity" subtitle="Changes and request outcomes" />
      <Tabs defaultValue="requests">
        <TabsList>
          <TabsTrigger value="requests">Requests</TabsTrigger>
          <TabsTrigger value="audit">Changes</TabsTrigger>
        </TabsList>
        <TabsContent value="audit" className="mt-6 space-y-6">
          <ResultsStatus label="Changes updates" message={status} />
          <div className="flex flex-wrap items-end gap-4">
            <div className="space-y-2">
              <label
                className="block text-sm font-medium"
                htmlFor="audit-category"
              >
                Change type
              </label>
              <Select
                value={category || "all"}
                onValueChange={(value) =>
                  setCategory(value === "all" ? "" : value)
                }
              >
                <SelectTrigger
                  id="audit-category"
                  ref={filterRef}
                  className="min-h-6 min-w-6 w-[180px] max-w-full"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {categories.map((item) => (
                    <SelectItem
                      key={item.value || "all"}
                      value={item.value || "all"}
                    >
                      {item.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <label
                className="block text-sm font-medium"
                htmlFor="audit-person"
              >
                Person
              </label>
              <Select
                value={actor || "everyone"}
                onValueChange={(value) => {
                  const next = value === "everyone" ? "" : value;
                  setActor(next);
                }}
              >
                <SelectTrigger
                  id="audit-person"
                  className="min-h-6 min-w-6 w-[180px] max-w-full"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="everyone">Everyone</SelectItem>
                  {actors.map(([id, label]) => (
                    <SelectItem key={id} value={id}>
                      {label}
                    </SelectItem>
                  ))}
                  {actor && !actors.some(([id]) => id === actor) && (
                    <SelectItem value={actor}>Selected person</SelectItem>
                  )}
                </SelectContent>
              </Select>
            </div>
            <Button
              variant="outline"
              onClick={() => { if (!auditFetching) void refetchAudit(); }}
              aria-disabled={auditFetching}
            >
              Refresh
            </Button>
            {(category || actor) && (
              <Button
                variant="ghost"
                onClick={clearFilters}
              >
                Clear filters
              </Button>
            )}
          </div>
          {peoplePending && (
            <StatusLine tone="unknown" text="Loading people" compact />
          )}
          {peopleError && (
            <Callout
              as="div"
              severity="critical"
              title="Couldn't load people"
              headerAction={
                <Button variant="outline" onClick={() => void refetchPeople()}>
                  Try again
                </Button>
              }
            >
              <p>
                People could not be loaded. Try again.
                {optionsData?.records.length
                  ? " Names from recent changes are still shown."
                  : ""}
              </p>
            </Callout>
          )}
          <div className="flex flex-wrap items-center gap-3">
            {automatedCount > 0 && (
              <div
                role="group"
                aria-label="Automated activity filter"
                className="text-muted-foreground flex flex-wrap items-center gap-3 text-sm"
              >
                <span>
                  {showAutomated
                    ? `Showing ${automatedCount} automated ${automatedCount === 1 ? "change" : "changes"}`
                    : `${automatedCount} automated ${automatedCount === 1 ? "change" : "changes"} hidden`}
                </span>
                <Button
                  variant="outline"
                  aria-pressed={showAutomated}
                  onClick={() => setShowAutomated((value) => !value)}
                >
                  {showAutomated
                    ? "Hide automated changes"
                    : "Show automated changes"}
                </Button>
              </div>
            )}
            <Button
              variant="outline"
              aria-pressed={showAccessAudit}
              onClick={() => setShowAccessAudit((value) => !value)}
            >
              {showAccessAudit ? "Hide access history" : "Show access history"}
            </Button>
          </div>
          {auditError && entries.length > 0 && !nextPageError && (
            <Callout
              as="div"
              severity="critical"
              title="Couldn't refresh changes"
              headerAction={
                <Button variant="outline" onClick={() => void refetchAudit()}>
                  Try again
                </Button>
              }
            >
              <p>
                The changes list could not be refreshed. We could
                not identify the cause. The changes already loaded are still
                shown.
              </p>
            </Callout>
          )}
          {auditPending ? (
            <div aria-label="Loading changes" aria-busy="true" className="relative">
              <span className="absolute right-0 top-4 z-10"><DelayedSpinner /></span>
              <div aria-hidden="true" className="divide-border divide-y">
                {[0, 1, 2, 3, 4].map((i) => (
                  <div className={auditRowLayout} key={i}>
                    <Skeleton className="hidden size-8 shrink-0 rounded-full sm:flex motion-reduce:animate-none" />
                    <div className="min-w-0 flex-1">
                      <Skeleton className="h-5 w-2/3 motion-reduce:animate-none" />
                      <Skeleton className="mt-1 h-4 w-1/2 motion-reduce:animate-none" />
                    </div>
                    <Skeleton className="h-4 w-16 shrink-0 motion-reduce:animate-none" />
                    <span className="size-4 shrink-0" />
                  </div>
                ))}
              </div>
            </div>
          ) : auditError && !entries.length ? (
            <Callout
              as="div"
              severity="critical"
              title="Couldn't load changes"
              headerAction={
                <Button variant="outline" onClick={() => void refetchAudit()}>
                  Try again
                </Button>
              }
            >
              <p>
                The changes list could not be loaded. We could not
                identify the cause. Try again.
              </p>
            </Callout>
          ) : !entries.length ? (
            <EmptyState
              icon={History}
              title={
                category || actor
                  ? "No matching changes"
                  : automatedCount > 0 && !showAutomated
                    ? "No visible changes"
                    : "No changes yet"
              }
              description={
                category || actor
                  ? "Changes to apps, keys and access plans appear here; none match these filters."
                  : automatedCount > 0 && !showAutomated
                    ? "Changes to apps, keys and access plans appear here; automated changes are currently hidden."
                    : "Changes to apps, keys and access plans appear here."
              }
              action={
                category || actor ? (
                  <Button
                    size="sm"
                    onClick={clearFilters}
                  >
                    Clear filters and show all changes
                  </Button>
                ) : automatedCount > 0 && !showAutomated ? (
                  <Button size="sm" onClick={() => setShowAutomated(true)}>
                    Show automated changes
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
              <div className="divide-border divide-y">
                {entries.map((row, index) => (
                  <button
                    type="button"
                    key={row.id}
                    aria-label={`Details: ${auditSummary(row)} by ${actorName(row)}`}
                    aria-describedby={`${rowId}-${index}-context ${rowId}-${index}-time`}
                    onClick={(event) => {
                      openerRef.current = event.currentTarget;
                      setEntry(row);
                    }}
                    className={`${auditRowLayout} group hover:bg-muted/40 focus-visible:outline-ring transition-colors focus-visible:outline-2 focus-visible:-outline-offset-2 motion-reduce:transition-none`}
                  >
                    <div className="bg-muted hidden size-8 shrink-0 items-center justify-center rounded-full sm:flex">
                      <History
                        aria-hidden="true"
                        className="text-muted-foreground size-4"
                      />
                    </div>
                    <div id={`${rowId}-${index}-context`} className="min-w-0 flex-1">
                      <p className="text-sm font-medium">{auditSummary(row)}</p>
                      <p className="text-muted-foreground mt-1 text-xs break-words">
                        {actorName(row)}
                        {changedName(row) ? ` · ${changedName(row)}` : ""}
                      </p>
                    </div>
                    <div id={`${rowId}-${index}-time`} className="text-muted-foreground shrink-0 text-xs">
                      <time {...formatTimestamp(row.created_at)} />
                    </div>
                    <ChevronRight
                      aria-hidden="true"
                      className="text-muted-foreground size-4 shrink-0"
                    />
                  </button>
                ))}
              </div>
              <div className="flex flex-col items-center gap-4">
                {nextPageError && (
                  <Callout
                    as="div"
                    severity="critical"
                    title="Couldn't load more changes"
                    headerAction={
                      <Button variant="outline" aria-disabled={fetchingNextPage} onClick={(event) => loadMore(event.currentTarget)}>
                        Try again
                      </Button>
                    }
                  >
                    <p>
                      More changes could not be loaded. We
                      could not identify the cause. The changes already loaded
                      are still shown.
                    </p>
                  </Callout>
                )}
                {hasNextPage ? (
                  <Button
                    variant="outline"
                    aria-disabled={fetchingNextPage}
                    onClick={(event) => loadMore(event.currentTarget)}
                  >
                    {fetchingNextPage ? "Loading…" : "Load more"}
                  </Button>
                ) : (
                  <p ref={endRef} tabIndex={-1} className="text-muted-foreground text-xs focus-visible:outline-ring focus-visible:outline-2">
                    {category || actor
                      ? "No more matching changes"
                      : "End of changes"}
                  </p>
                )}
              </div>
            </>
          )}
        </TabsContent>
        <TabsContent value="requests" className="mt-6">
          <Requests />
        </TabsContent>
      </Tabs>
      <Sheet
        open={entry !== null}
        onOpenChange={(open) => {
          if (!open) setEntry(null);
        }}
      >
        <SheetContent
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            (openerRef.current?.isConnected ? openerRef.current : filterRef.current)?.focus();
          }}
          className="w-full overflow-y-auto sm:max-w-lg motion-reduce:animate-none motion-reduce:transition-none motion-reduce:[&_*]:animate-none motion-reduce:[&_*]:transition-none [&_button]:min-h-6 [&_button]:min-w-6"
        >
          {entry && (
            <>
              <SheetHeader className="pr-14">
                <SheetTitle>{auditSummary(entry)}</SheetTitle>
                <SheetDescription className="sr-only">
                  Details of this change
                </SheetDescription>
              </SheetHeader>
              <div className="min-w-0 space-y-6 px-4 pb-8">
                <dl className="space-y-4 text-sm">
                  <div>
                    <dt className="text-muted-foreground">Changed by</dt>
                    <dd className="mt-1">{actorName(entry)}</dd>
                  </div>
                  {changedName(entry) && (
                    <div>
                      <dt className="text-muted-foreground">Changed</dt>
                      <dd className="mt-1 break-words">{changedName(entry)}</dd>
                    </div>
                  )}
                  <div>
                    <dt className="text-muted-foreground">When</dt>
                    <dd className="mt-1">
                      <time {...formatTimestamp(entry.created_at)} />
                    </dd>
                  </div>
                </dl>
                <details>
                  <summary className="min-h-6 min-w-6 cursor-pointer content-center text-sm font-medium focus-visible:outline-ring focus-visible:outline-2">
                    Technical details
                  </summary>
                  <div className="mt-4">
                    <pre
                      tabIndex={0}
                      aria-label="Technical change details"
                      className="bg-muted focus-visible:outline-ring overflow-x-auto rounded-lg p-4 font-mono text-xs leading-relaxed focus-visible:outline-2"
                    >
                      <code>
                        {JSON.stringify(
                          {
                            ...entry,
                            created_at: new Date(
                              entry.created_at * 1000,
                            ).toISOString(),
                          },
                          null,
                          2,
                        )}
                      </code>
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
