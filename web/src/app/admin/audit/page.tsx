"use client";

/** Admin → Audit — surface spec (docs/design/WEB_UI_STEERING.md).
 *
 * Reverse-chron list of gateway events. Key behaviours:
 *  - Filter chips (actor, action family) drive the SERVER query — they pass
 *    actor_id / action_prefix to GET /audit, not a client-side slice.
 *  - Changing a filter resets pagination and refetches from the server.
 *  - "Load more" follows next_cursor from the response (append pages).
 *  - HONEST END STATE: when next_cursor is null, render "End of audit log"
 *    (no filter) or "No more matching events" (under a filter).
 *  - Row click opens a Sheet with the full record as mono JSON + CopyButton.
 *  - Every timestamp goes through <RelativeTime>; every entity id through
 *    <EntityRef> — raw IDs and epoch ints never appear as primary content.
 *
 * Action-family chips: derived from the currently loaded set. Because the
 * full log is server-paginated we cannot enumerate all families cheaply, so
 * chips show the families seen in the loaded pages. A note makes this clear
 * when a family filter is active. */

import {
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";

import {
  api,
  ApiError,
  queryKeys,
  type AuditEntry,
  type User,
} from "@/lib/api";
import {
  ActivityFeedRow,
  Button,
  CompactTouchTargets,
  CopyButton,
  EmptyOperationalState,
  EntityRef,
  FilterToolbar,
  IncidentBanner,
  RelativeTime,
  SectionActionHeader,
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  Skeleton,
} from "@/components/ui";
import { adminTargetHref } from "@/lib/admin-links";

// ---------------------------------------------------------------------------
// Action phrase map — raw event type → sentence-case human phrase + family
//
// The "family" is used for the action-family filter chip and maps to the
// action_prefix server param (e.g. family "app" → action_prefix "app.").

interface ActionMeta {
  /** Sentence-case human phrase shown in the Action cell. */
  phrase: string;
  /** Filter family (short prefix); derived automatically when absent. */
  family?: string;
}

const ACTION_PHRASES: Record<string, ActionMeta> = {
  "app.create": { phrase: "Created app", family: "app" },
  "app.disable": { phrase: "Disabled app", family: "app" },
  "app.enable": { phrase: "Re-enabled app", family: "app" },
  "credential.create": { phrase: "Created key", family: "credential" },
  "credential.revoke": { phrase: "Revoked key", family: "credential" },
  "profile.create": { phrase: "Created profile", family: "profile" },
  "profile.update": { phrase: "Updated profile", family: "profile" },
  "profile.delete": { phrase: "Deleted profile", family: "profile" },
  "admin_key.create": { phrase: "Created admin key", family: "admin_key" },
  "runtime_profile_mapping.create": {
    phrase: "Added access mapping",
    family: "access",
  },
  "runtime_profile_mapping.disable": {
    phrase: "Removed access mapping",
    family: "access",
  },
  "operator.user_access_defaults.update": {
    phrase: "Updated user defaults",
    family: "operator",
  },
  "operator.service_availability.update": {
    phrase: "Updated service status",
    family: "operator",
  },
  "operator.media_job_admission.update": {
    phrase: "Updated media admission",
    family: "operator",
  },
  "operator.service_registry.update": {
    phrase: "Updated service registry",
    family: "operator",
  },
  "operator.service.added": { phrase: "Added service", family: "operator" },
  "operator.service.updated": { phrase: "Updated service", family: "operator" },
  "operator.service.removed": { phrase: "Removed service", family: "operator" },
  "operator.service.toggled": { phrase: "Toggled service", family: "operator" },
  "operator.extension_package.enable": {
    phrase: "Enabled extension",
    family: "operator",
  },
  "operator.oidc_provider_config.update": {
    phrase: "Updated OIDC config",
    family: "operator",
  },
};

function actionMeta(action: string): ActionMeta {
  const known = ACTION_PHRASES[action];
  if (known) return known;
  // Fallback: humanise the raw string and derive the family from its prefix.
  const parts = action.split(".");
  const family = parts[0] ?? "other";
  const verb = parts[parts.length - 1] ?? action;
  const phrase =
    verb.charAt(0).toUpperCase() + verb.slice(1).replace(/_/g, " ");
  return { phrase, family };
}

/** Server predicates for the visible action families. */
const FAMILY_ACTION_PREFIXES: Record<string, string> = {
  access: "runtime_profile_mapping.",
  admin_key: "admin_key.",
  app: "app.",
  credential: "credential.",
  operator: "operator.",
  profile: "profile.",
};

/** Map a displayed family to its exact server-side action predicate. */
function familyToPrefix(family: string): string {
  return FAMILY_ACTION_PREFIXES[family] ?? `${family}.`;
}

// ---------------------------------------------------------------------------
// Page-level pagination state
//
// We accumulate pages manually (not useInfiniteQuery) because the filter
// set can change mid-session and we need a simple reset path.

const PAGE_SIZE = 50;

// ---------------------------------------------------------------------------
// Helpers

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return "Something went wrong. Please try again.";
}

function userLabel(user: User | undefined): string {
  return user?.display_name ?? user?.email ?? "Unknown user";
}

function actorDisplayLabel(
  entry: AuditEntry,
  usersById: Map<string, User>,
): string {
  if (entry.actor_id === "cli" || entry.actor_kind === "cli") return "System";
  const user = usersById.get(entry.actor_id);
  if (user) return userLabel(user);
  if (entry.actor_kind === "service_account") return "Service principal";
  if (entry.actor_kind === "admin") return "Admin";
  return "Unattributed actor";
}

function auditDetails(entry: AuditEntry): Record<string, unknown> {
  // Audit-detail redaction is a server trust-boundary contract. Until that
  // typed projection lands, do not pretend client-side key matching is safe.
  return {
    action: actionMeta(entry.action).phrase,
    actor:
      entry.actor_kind === "cli" || entry.actor_id === "cli"
        ? "System"
        : (entry.actor_kind ?? "Unattributed actor"),
    target: entry.target_kind ?? undefined,
  };
}

function RouteEventTargetSync({
  onChange,
}: {
  onChange: (state: {
    event: string | null;
    actor: string | null;
    category: string | null;
  }) => void;
}) {
  const searchParams = useSearchParams();
  useEffect(() => {
    onChange({
      event: searchParams.get("event"),
      actor: searchParams.get("actor"),
      category: searchParams.get("category"),
    });
  }, [onChange, searchParams]);
  return null;
}

// ---------------------------------------------------------------------------
// Page

function AdminAuditContents() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const auditSearch = searchParams.toString();
  const adminPathname = pathname.replace(/^\/ui(?=\/admin\/)/, "");
  const auditReturnPath = `${adminPathname}${auditSearch ? `?${auditSearch}` : ""}`;
  // Active server-side filters
  const [actorFilter, setActorFilter] = useState<string | null>(null);
  const [familyFilter, setFamilyFilter] = useState<string | null>(null);
  const activeFiltersRef = useRef({
    actor: null as string | null,
    category: null as string | null,
  });

  // Accumulated pages: list of cursors consumed (tracked for future
  // refetch-all support; not read directly by render logic).
  // next_cursor returned by the last fetched page (null = exhausted)
  const [nextCursor, setNextCursor] = useState<string | null | undefined>(
    undefined,
  );
  // Accumulated rows across all pages
  const [allEntries, setAllEntries] = useState<AuditEntry[]>([]);
  // The pending load belongs to the generation that started it. A filter
  // transition immediately makes an older pending request invisible.
  const [loadingMoreGeneration, setLoadingMoreGeneration] = useState<
    number | null
  >(null);
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null);

  // Options are discovered from unfiltered and filtered responses, but never
  // discarded when a narrow result page replaces the visible rows.
  const actorOptionsRef = useRef(new Map<string, string>());
  const familyOptionsRef = useRef(new Set<string>());
  const [, setOptionVersion] = useState(0);
  const generationRef = useRef(0);
  const loadingMore = loadingMoreGeneration === generationRef.current;

  const [detailEntry, setDetailEntry] = useState<AuditEntry | null>(null);
  const [routeEventTarget, setRouteEventTarget] = useState<string | null>(null);
  const consumedEventTargetRef = useRef<string | null>(null);

  // Build the query params for the FIRST page
  const firstPageParams = {
    limit: PAGE_SIZE,
    actor_id: actorFilter ?? undefined,
    action_prefix: familyFilter ? familyToPrefix(familyFilter) : undefined,
  };
  const {
    data: firstPageData,
    isPending: firstPagePending,
    isError: firstPageIsError,
    error: firstPageError,
    refetch: refetchFirst,
    isFetching: firstPageFetching,
  } = useQuery({
    queryKey: queryKeys.audit({
      actorId: actorFilter,
      actionPrefix: familyFilter,
    }),
    queryFn: async () => {
      const generation = generationRef.current;
      const page = await api.audit.list(firstPageParams);
      return { generation, page };
    },
    // Don't cache stale data when filters change — always treat as fresh
    staleTime: 0,
  });

  useEffect(() => {
    if (!routeEventTarget) {
      consumedEventTargetRef.current = null;
      return;
    }
    if (consumedEventTargetRef.current === routeEventTarget) return;
    const target = allEntries.find((entry) => entry.id === routeEventTarget);
    if (!target) return;
    setDetailEntry(target);
    consumedEventTargetRef.current = routeEventTarget;
  }, [allEntries, routeEventTarget]);

  const routeEventInLoadedPage = useMemo(() => {
    if (!routeEventTarget) return null;
    return allEntries.find((entry) => entry.id === routeEventTarget) ?? null;
  }, [allEntries, routeEventTarget]);

  const {
    data: routeEvent,
    isError: routeEventIsError,
    isFetching: routeEventFetching,
  } = useQuery({
    queryKey: queryKeys.auditEvent(routeEventTarget),
    queryFn: () => api.audit.get(routeEventTarget ?? ""),
    enabled: routeEventTarget !== null && routeEventInLoadedPage === null,
  });

  useEffect(() => {
    if (!routeEventTarget || !routeEvent) return;
    if (detailEntry?.id !== routeEvent.id) {
      setDetailEntry(routeEvent);
    }
    consumedEventTargetRef.current = routeEventTarget;
  }, [detailEntry?.id, routeEvent, routeEventTarget]);

  const { data: usersData, isPending: usersPending } = useQuery({
    queryKey: ["users"] as const,
    queryFn: api.users.list,
  });

  const usersById = useMemo(
    () => new Map((usersData ?? []).map((user) => [user.id, user])),
    [usersData],
  );

  useEffect(() => {
    if (!firstPageData || firstPageData.generation !== generationRef.current)
      return;
    const result = firstPageData.page;
    setAllEntries(result.records);
    setNextCursor(result.next_cursor);
    setLoadMoreError(null);
    for (const entry of result.records) {
      actorOptionsRef.current.set(
        entry.actor_id,
        actorDisplayLabel(entry, usersById),
      );
      familyOptionsRef.current.add(actionMeta(entry.action).family ?? "other");
    }
    setOptionVersion((version) => version + 1);
  }, [firstPageData, usersById]);

  // Keep the currently selected values present even for empty filtered pages.
  const actorOptions = (() => {
    if (actorFilter && !actorOptionsRef.current.has(actorFilter)) {
      return [...actorOptionsRef.current, [actorFilter, "Selected actor"]].map(
        ([id, label]) => ({ id, label }),
      );
    }
    return Array.from(actorOptionsRef.current, ([id, label]) => ({
      id,
      label,
    }));
  })();

  const actionFamilies = Array.from(
    new Set([
      ...familyOptionsRef.current,
      ...(familyFilter ? [familyFilter] : []),
    ]),
  ).sort();

  const replaceRouteState = useCallback(
    (next: {
      event?: string | null;
      actor?: string | null;
      category?: string | null;
    }) => {
      const params = new URLSearchParams(window.location.search);
      for (const [key, value] of Object.entries(next)) {
        if (value) params.set(key, value);
        else params.delete(key);
      }
      router.replace(`${pathname}${params.size ? `?${params}` : ""}`);
    },
    [pathname, router],
  );

  const resetPagination = useCallback(() => {
    generationRef.current += 1;
    setAllEntries([]);
    setNextCursor(undefined);
    setLoadingMoreGeneration(null);
    setLoadMoreError(null);
  }, []);

  // Update both filter atoms in one route transition so event and return
  // context survive while actor/category never race each other in the URL.
  const applyFilters = useCallback(
    (next: { actor: string | null; category: string | null }) => {
      resetPagination();
      activeFiltersRef.current = next;
      setActorFilter(next.actor);
      setFamilyFilter(next.category);
      replaceRouteState(next);
    },
    [replaceRouteState, resetPagination],
  );

  const handleActorChange = useCallback(
    (actor: string | null) => {
      applyFilters({ actor, category: familyFilter });
    },
    [applyFilters, familyFilter],
  );

  const handleFamilyChange = useCallback(
    (category: string | null) => {
      applyFilters({ actor: actorFilter, category });
    },
    [actorFilter, applyFilters],
  );

  const handleRouteStateChange = useCallback(
    ({
      event,
      actor,
      category,
    }: {
      event: string | null;
      actor: string | null;
      category: string | null;
    }) => {
      setRouteEventTarget(event);
      const active = activeFiltersRef.current;
      if (actor === active.actor && category === active.category) return;
      resetPagination();
      activeFiltersRef.current = { actor, category };
      setActorFilter(actor);
      setFamilyFilter(category);
    },
    [resetPagination],
  );

  // Load the next page
  async function loadMore() {
    if (!nextCursor || loadingMore) return;
    const generation = generationRef.current;
    const cursor = nextCursor;
    setLoadingMoreGeneration(generation);
    setLoadMoreError(null);
    try {
      const result = await api.audit.list({
        limit: PAGE_SIZE,
        actor_id: actorFilter ?? undefined,
        action_prefix: familyFilter ? familyToPrefix(familyFilter) : undefined,
        before: cursor,
      });
      if (generation !== generationRef.current) return;
      setAllEntries((prev) => [...prev, ...result.records]);
      setNextCursor(result.next_cursor);
    } catch (error) {
      if (generation === generationRef.current)
        setLoadMoreError(errorMessage(error));
    } finally {
      if (generation === generationRef.current) setLoadingMoreGeneration(null);
    }
  }

  const loading = firstPagePending || usersPending;
  const hasFilter = Boolean(actorFilter ?? familyFilter);
  // Exhausted = last page returned null cursor (and we've seen at least one page)
  const exhausted = nextCursor === null && !firstPagePending;
  const missingRouteEvent =
    routeEventTarget !== null &&
    routeEventInLoadedPage === null &&
    routeEventIsError &&
    !routeEventFetching;

  return (
    <CompactTouchTargets>
      <div className="flex w-full flex-col gap-8">
        <RouteEventTargetSync onChange={handleRouteStateChange} />
        <SectionActionHeader
          as="h1"
          kicker="activity"
          title="Audit"
          description="A record of who changed what, when. Newest first."
        />

        {firstPageIsError && allEntries.length === 0 ? (
          <IncidentBanner
            tone="bad"
            title="Couldn't load audit events."
            detail={errorMessage(firstPageError)}
            actions={
              <Button size="sm" onClick={() => void refetchFirst()}>
                Retry
              </Button>
            }
          />
        ) : (
          <>
            {firstPageIsError && allEntries.length > 0 && (
              <IncidentBanner
                tone="bad"
                title="Audit events may be stale."
                detail={errorMessage(firstPageError)}
                actions={
                  <Button size="sm" onClick={() => void refetchFirst()}>
                    Retry
                  </Button>
                }
              />
            )}
            {missingRouteEvent && (
              <IncidentBanner
                tone="warn"
                title="No audit event matches this link"
                detail={`The audit event "${routeEventTarget}" is not in the current audit log.`}
              />
            )}

            {/* Filter chips — drive the server query (not client-side slice). */}
            {!loading &&
              (actorOptions.length > 0 ||
                actionFamilies.length > 0 ||
                hasFilter) && (
                <FilterToolbar
                  filters={
                    <>
                      {(actorOptions.length > 1 || actorFilter !== null) && (
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className="text-vf-micro text-vf-subtle font-mono uppercase">
                            Actor:
                          </span>
                          <FilterChip
                            active={actorFilter === null}
                            onClick={() => handleActorChange(null)}
                          >
                            All
                          </FilterChip>
                          {actorOptions.map(({ id, label }) => (
                            <FilterChip
                              key={id}
                              active={actorFilter === id}
                              onClick={() =>
                                handleActorChange(
                                  actorFilter === id ? null : id,
                                )
                              }
                            >
                              {label}
                            </FilterChip>
                          ))}
                        </div>
                      )}
                      {(actionFamilies.length > 1 || familyFilter !== null) && (
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className="text-vf-micro text-vf-subtle font-mono uppercase">
                            Category:
                          </span>
                          <FilterChip
                            active={familyFilter === null}
                            onClick={() => handleFamilyChange(null)}
                          >
                            All
                          </FilterChip>
                          {actionFamilies.map((family) => (
                            <FilterChip
                              key={family}
                              active={familyFilter === family}
                              onClick={() =>
                                handleFamilyChange(
                                  familyFilter === family ? null : family,
                                )
                              }
                            >
                              {family}
                            </FilterChip>
                          ))}
                        </div>
                      )}
                    </>
                  }
                />
              )}

            {loading || (firstPageFetching && allEntries.length === 0) ? (
              <div className="vf-activity-feed" aria-busy="true">
                {Array.from({ length: 8 }).map((_, i) => (
                  <div key={i} className="vf-activity-row">
                    <Skeleton className="mt-1 h-2 w-2 shrink-0 rounded-full" />
                    <div className="flex flex-1 flex-col gap-1">
                      <Skeleton className="h-4 w-48" />
                      <Skeleton className="h-3 w-32" />
                    </div>
                    <Skeleton className="h-3 w-16 shrink-0" />
                  </div>
                ))}
              </div>
            ) : allEntries.length === 0 ? (
              hasFilter ? (
                <EmptyOperationalState
                  title="No events match the current filters."
                  action={
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => {
                        applyFilters({ actor: null, category: null });
                      }}
                    >
                      Clear filters
                    </Button>
                  }
                />
              ) : (
                <EmptyOperationalState title="No audit events yet." />
              )
            ) : (
              <div className="vf-activity-feed">
                {allEntries.map((entry) => {
                  const { phrase } = actionMeta(entry.action);
                  const actor = actorDisplayLabel(entry, usersById);
                  const target = entry.target_id
                    ? entry.metadata &&
                      typeof entry.metadata["name"] === "string"
                      ? entry.metadata["name"]
                      : entry.target_kind
                        ? entry.target_kind.replace(/_/g, " ")
                        : entry.target_id
                    : undefined;
                  return (
                    <ActivityFeedRow
                      key={entry.id}
                      title={phrase}
                      detail={
                        target ? `${target} · ${entry.action}` : entry.action
                      }
                      actor={actor}
                      time={<RelativeTime ts={entry.created_at} />}
                      stat={
                        <Button
                          variant="ghost"
                          size="sm"
                          className="text-vf-small px-3"
                          onClick={() => {
                            setDetailEntry(entry);
                            replaceRouteState({ event: entry.id });
                          }}
                          aria-label="View full event record"
                        >
                          Details
                        </Button>
                      }
                    />
                  );
                })}
              </div>
            )}

            {/* Load more / End of log — honest end state per the honesty law */}
            {!loading && allEntries.length > 0 && (
              <div className="flex flex-col items-center gap-2 pt-2">
                {loadMoreError && (
                  <IncidentBanner
                    tone="bad"
                    title="Couldn't load more audit events."
                    detail={loadMoreError}
                  />
                )}
                {exhausted ? (
                  <p className="text-vf-small text-vf-subtle">
                    {hasFilter ? "No more matching events" : "End of audit log"}
                  </p>
                ) : nextCursor !== undefined ? (
                  <Button
                    variant="secondary"
                    size="sm"
                    loading={loadingMore}
                    onClick={() => void loadMore()}
                  >
                    Load more
                  </Button>
                ) : null}
              </div>
            )}
          </>
        )}

        {/* Row detail sheet — mono JSON of the full record + copy button. */}
        <Sheet
          open={detailEntry !== null}
          onOpenChange={(open) => {
            if (!open) {
              setDetailEntry(null);
              replaceRouteState({ event: null });
            }
          }}
        >
          {detailEntry && (
            <SheetContent>
              <SheetHeader>
                <SheetTitle>{actionMeta(detailEntry.action).phrase}</SheetTitle>
                <SheetDescription>
                  Safe event details —{" "}
                  <RelativeTime ts={detailEntry.created_at} />
                </SheetDescription>
              </SheetHeader>
              <AuditDetailPanel
                entry={detailEntry}
                returnPath={auditReturnPath}
              />
            </SheetContent>
          )}
        </Sheet>
      </div>
    </CompactTouchTargets>
  );
}

export default function AdminAuditPage() {
  return (
    <Suspense fallback={null}>
      <AdminAuditContents />
    </Suspense>
  );
}

function FilterChip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={
        active
          ? "rounded-vf-sm bg-vf-accent/15 text-vf-accent border-vf-accent/30 text-vf-small min-h-11 min-w-11 border px-3 font-medium transition-colors min-[980px]:min-h-0 min-[980px]:min-w-0"
          : "rounded-vf-sm border-vf-border text-vf-muted hover:text-vf-text hover:border-vf-border/80 text-vf-small min-h-11 min-w-11 border bg-transparent px-3 transition-colors min-[980px]:min-h-0 min-[980px]:min-w-0"
      }
    >
      {children}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Audit detail sheet panel — mono JSON of the full record + a CopyButton.

function AuditDetailPanel({
  entry,
  returnPath,
}: {
  entry: AuditEntry;
  returnPath: string;
}) {
  const details = auditDetails(entry);
  const json = JSON.stringify(details, null, 2);
  const targetHref = adminTargetHref(
    entry.target_kind,
    entry.target_id,
    returnPath,
  );
  const targetName =
    (entry.metadata &&
      typeof entry.metadata["name"] === "string" &&
      entry.metadata["name"]) ||
    entry.target_kind?.replace(/_/g, " ") ||
    "target";

  return (
    <div className="flex flex-col gap-4 pt-2">
      {/* Quick-glance summary */}
      <dl className="flex flex-col gap-2">
        <DetailRow label="When">
          <RelativeTime ts={entry.created_at} />
        </DetailRow>
        <DetailRow label="Event">
          <span className="font-mono">{entry.action}</span>
        </DetailRow>
        {entry.target_id && (
          <DetailRow label="Target">
            <EntityRef
              name={targetName}
              id={entry.target_id}
              href={targetHref ?? undefined}
              idVisibility="hover"
            />
          </DetailRow>
        )}
        <DetailRow label="Actor">
          <span>
            {entry.actor_kind === "cli" || entry.actor_id === "cli"
              ? "System"
              : entry.actor_kind === "service_account"
                ? "Service principal"
                : (entry.actor_kind ?? "Unattributed actor")}
          </span>
        </DetailRow>
      </dl>

      {/* Full raw record */}
      <div className="flex flex-col gap-1.5">
        <div className="flex items-center justify-between gap-2">
          <span className="text-vf-micro text-vf-subtle font-mono uppercase">
            Safe event details
          </span>
          <CopyButton value={json} aria-label="Copy event JSON" />
        </div>
        <pre className="rounded-vf-md border-vf-border bg-vf-surface-2 text-vf-micro text-vf-text max-h-[60vh] overflow-y-auto border p-3 font-mono break-all whitespace-pre-wrap">
          {json}
        </pre>
      </div>
    </div>
  );
}

function DetailRow({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="text-vf-micro text-vf-subtle shrink-0 font-mono uppercase">
        {label}
      </dt>
      <dd className="text-vf-small text-vf-text min-w-0 text-right">
        {children}
      </dd>
    </div>
  );
}
