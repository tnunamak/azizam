"use client";

import { useState } from "react";
import { Requests } from "./requests";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { History, ChevronRight } from "lucide-react";
import { api, type AuditEntry } from "@/lib/api";
import { auditDescription, auditTargetName } from "@/lib/audit-description";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import {
  PageHeader,
  EmptyState,
  ErrorState,
  RelativeTime,
  CodeBlock,
} from "@/components/product";

const categories = [
  { value: "", label: "All changes" },
  { value: "app.", label: "Apps" },
  { value: "credential.", label: "Keys" },
  { value: "profile.", label: "Access plans" },
  { value: "operator.", label: "Gateway settings" },
  { value: "runtime_profile_mapping.", label: "Access mappings" },
];
export default function ActivityPage() {
  const [category, setCategory] = useState("");
  const [actor, setActor] = useState("");
  const [actorInput, setActorInput] = useState("");
  const [entry, setEntry] = useState<AuditEntry | null>(null);
  // Filter choices come from an unfiltered page; labels never infer identity
  // from an opaque actor ID. The filter itself is applied by the server.
  const { data: optionsData } = useQuery({
    queryKey: ["activity", "actors"],
    queryFn: () => api.audit.list({ limit: 100 }),
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
    queryKey: ["activity", "audit", category, actor],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      api.audit.list({
        limit: 50,
        action_prefix: category || undefined,
        actor_id: actor || undefined,
        before: pageParam,
      }),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  });
  const entries = auditData?.pages.flatMap((page) => page.records) ?? [];
  const actors = Array.from(
    new Map(
      (optionsData?.records ?? []).map((row) => [
        row.actor_id,
        row.actor_label,
      ]),
    ),
  );
  return (
    <div className="space-y-8">
      <PageHeader
        title="Activity"
        answer="Who changed what, and how did requests go?"
      />
      <Tabs defaultValue="audit">
        <TabsList>
          <TabsTrigger value="audit">Audit</TabsTrigger>
          <TabsTrigger value="requests">Requests</TabsTrigger>
        </TabsList>
        <TabsContent value="audit" className="mt-6 space-y-6">
          <div className="flex flex-wrap items-end gap-4">
            <div className="space-y-2">
              <label
                className="block text-sm font-medium"
                htmlFor="audit-category"
              >
                Change type
              </label>
              <select
                id="audit-category"
                className="border-input bg-background h-10 max-w-full rounded-lg border px-3 text-sm"
                value={category}
                onChange={(e) => setCategory(e.target.value)}
              >
                {categories.map((item) => (
                  <option key={item.value} value={item.value}>
                    {item.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-2">
              <label
                className="block text-sm font-medium"
                htmlFor="audit-person"
              >
                Person
              </label>
              <select
                id="audit-person"
                className="border-input bg-background h-10 max-w-full rounded-lg border px-3 text-sm"
                value={actor}
                onChange={(e) => {
                  setActor(e.target.value);
                  setActorInput(e.target.value);
                }}
              >
                <option value="">Everyone</option>
                {actors.map(([id, label]) => (
                  <option key={id} value={id}>
                    {label}
                  </option>
                ))}
                {actor && !actors.some(([id]) => id === actor) && (
                  <option value={actor}>Selected person</option>
                )}
              </select>
            </div>
            <Button
              variant="outline"
              onClick={() => void refetchAudit()}
              disabled={auditFetching}
            >
              Refresh
            </Button>
            {(category || actor) && (
              <Button
                variant="ghost"
                onClick={() => {
                  setActor("");
                  setActorInput("");
                  setCategory("");
                }}
              >
                Clear filters
              </Button>
            )}
          </div>
          <details className="text-muted-foreground text-sm">
            <summary className="cursor-pointer">
              Filter by a person not in the recent events
            </summary>
            <form
              className="mt-3 flex max-w-md gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                setActor(actorInput.trim());
              }}
            >
              <Input
                aria-label="Person ID"
                value={actorInput}
                onChange={(e) => setActorInput(e.target.value)}
                placeholder="Person ID"
              />
              <Button type="submit" variant="outline">
                Apply
              </Button>
            </form>
          </details>
          {auditError && entries.length > 0 && !nextPageError && (
            <ErrorState
              message="These events could not be refreshed. Try again."
              onRetry={() => void refetchAudit()}
            />
          )}
          {auditPending ? (
            <div
              aria-label="Loading audit events"
              aria-busy="true"
              className="space-y-5"
            >
              {[0, 1, 2, 3, 4].map((i) => (
                <Skeleton className="h-14 w-full" key={i} />
              ))}
            </div>
          ) : auditError && !entries.length ? (
            <ErrorState
              message="Audit events could not be loaded. Try again."
              onRetry={() => void refetchAudit()}
            />
          ) : !entries.length ? (
            <EmptyState
              icon={History}
              title={
                category || actor
                  ? "No changes match these filters"
                  : "No changes yet"
              }
              description="Changes to apps, keys, access plans and gateway settings appear here."
              action={
                category || actor ? (
                  <Button
                    variant="outline"
                    onClick={() => {
                      setCategory("");
                      setActor("");
                    }}
                  >
                    Clear filters
                  </Button>
                ) : undefined
              }
            />
          ) : (
            <>
              <div className="divide-border divide-y">
                {entries.map((row) => (
                  <button
                    type="button"
                    key={row.id}
                    aria-label={`Details: ${auditDescription(row)} by ${row.actor_label}`}
                    onClick={() => setEntry(row)}
                    className="group hover:bg-muted/40 focus-visible:outline-ring flex w-full items-center gap-4 py-4 text-left transition-colors focus-visible:outline-2"
                  >
                    <div className="bg-muted hidden size-8 shrink-0 items-center justify-center rounded-full sm:flex">
                      <History
                        aria-hidden="true"
                        className="text-muted-foreground size-4"
                      />
                    </div>
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-medium">
                        {auditDescription(row)}
                      </p>
                      <p className="text-muted-foreground mt-1 text-xs break-words">
                        {row.actor_label}
                        {auditTargetName(row)
                          ? ` · ${auditTargetName(row)}`
                          : ""}
                      </p>
                      <p className="text-muted-foreground mt-1 text-xs break-words">
                        {row.action}
                      </p>
                    </div>
                    <div className="text-muted-foreground shrink-0 text-xs">
                      <RelativeTime ts={row.created_at} />
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
                  <ErrorState
                    message="More events could not be loaded. Try again."
                    onRetry={() => void fetchNextPage()}
                  />
                )}
                {hasNextPage ? (
                  <Button
                    variant="outline"
                    disabled={fetchingNextPage}
                    onClick={() => void fetchNextPage()}
                  >
                    {fetchingNextPage ? "Loading…" : "Load more"}
                  </Button>
                ) : (
                  <p className="text-muted-foreground text-xs">
                    {category || actor
                      ? "No more matching events"
                      : "End of audit log"}
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
        <SheetContent className="w-full overflow-y-auto sm:max-w-lg">
          {entry && (
            <>
              <SheetHeader className="pr-14">
                <SheetTitle>{auditDescription(entry)}</SheetTitle>
                <SheetDescription>
                  {entry.actor_label} · <RelativeTime ts={entry.created_at} />
                </SheetDescription>
              </SheetHeader>
              <div className="min-w-0 space-y-6 px-4 pb-8">
                <dl className="space-y-4 text-sm">
                  <div>
                    <dt className="text-muted-foreground">Person</dt>
                    <dd className="mt-1">{entry.actor_label}</dd>
                  </div>
                  {auditTargetName(entry) && (
                    <div>
                      <dt className="text-muted-foreground">Changed</dt>
                      <dd className="mt-1 break-words">
                        {auditTargetName(entry)}
                      </dd>
                    </div>
                  )}
                  <div>
                    <dt className="text-muted-foreground">When</dt>
                    <dd className="mt-1">
                      <RelativeTime ts={entry.created_at} />
                    </dd>
                  </div>
                </dl>
                <details>
                  <summary className="cursor-pointer text-sm font-medium">
                    Technical details
                  </summary>
                  <div className="mt-4">
                    <CodeBlock
                      language="json"
                      code={JSON.stringify(
                        {
                          ...entry,
                          created_at: new Date(
                            entry.created_at * 1000,
                          ).toISOString(),
                        },
                        null,
                        2,
                      )}
                    />
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
