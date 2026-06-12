"use client";

/** Admin → Audit — surface spec (docs/design/WEB_UI_STEERING.md).
 *
 * Reverse-chron list of gateway events. Key behaviours:
 *  - Raw action strings ("app.create") mapped to sentence-case human phrases;
 *    raw type rendered as micro mono detail below the phrase.
 *  - Client-side filter chips: actor + action family (the prefix before the
 *    first dot, e.g. "app", "credential", "operator").
 *  - "Load more" increases the limit param and re-fetches the full window
 *    (per the contract: no cursor/offset at current volumes).
 *  - Row click opens a Sheet with the full record as mono JSON + CopyButton.
 *  - Every timestamp goes through <RelativeTime>; every entity id through
 *    <EntityRef> — raw IDs and epoch ints never appear as primary content. */

import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  api,
  ApiError,
  queryKeys,
  type AuditEntry,
  type User,
} from "@/lib/api";
import {
  Button,
  CopyButton,
  DataTable,
  type DataTableColumn,
  EmptyState,
  EntityRef,
  RelativeTime,
  SectionHeader,
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui";

// ---------------------------------------------------------------------------
// Action phrase map — raw event type → sentence-case human phrase + family
//
// The "family" is used for the action-family filter chip.  It is the portion
// before the first dot, normalised to lower-case (e.g. "operator", "app").

interface ActionMeta {
  /** Sentence-case human phrase shown in the Action cell. */
  phrase: string;
  /** Filter family (short prefix); derived automatically when absent. */
  family?: string;
}

const ACTION_PHRASES: Record<string, ActionMeta> = {
  "app.create":                               { phrase: "Created app",             family: "app" },
  "app.disable":                              { phrase: "Disabled app",             family: "app" },
  "app.enable":                               { phrase: "Re-enabled app",           family: "app" },
  "credential.create":                        { phrase: "Created key",              family: "key" },
  "credential.revoke":                        { phrase: "Revoked key",              family: "key" },
  "profile.create":                           { phrase: "Created profile",          family: "profile" },
  "profile.update":                           { phrase: "Updated profile",          family: "profile" },
  "profile.delete":                           { phrase: "Deleted profile",          family: "profile" },
  "admin_key.create":                         { phrase: "Created admin key",        family: "key" },
  "runtime_profile_mapping.create":           { phrase: "Added access mapping",     family: "access" },
  "runtime_profile_mapping.disable":          { phrase: "Removed access mapping",   family: "access" },
  "operator.user_access_defaults.update":     { phrase: "Updated user defaults",    family: "operator" },
  "operator.service_availability.update":     { phrase: "Updated service status",   family: "operator" },
  "operator.media_job_admission.update":      { phrase: "Updated media admission",  family: "operator" },
  "operator.service_registry.update":         { phrase: "Updated service registry", family: "operator" },
  "operator.service.added":                   { phrase: "Added service",            family: "operator" },
  "operator.service.updated":                 { phrase: "Updated service",          family: "operator" },
  "operator.service.removed":                 { phrase: "Removed service",          family: "operator" },
  "operator.service.toggled":                 { phrase: "Toggled service",          family: "operator" },
  "operator.extension_package.enable":        { phrase: "Enabled extension",        family: "operator" },
  "operator.oidc_provider_config.update":     { phrase: "Updated OIDC config",      family: "operator" },
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

/** All unique action families present in the current result set. */
function deriveActionFamilies(entries: AuditEntry[]): string[] {
  const families = new Set<string>();
  for (const entry of entries) {
    families.add(actionMeta(entry.action).family ?? "other");
  }
  return Array.from(families).sort();
}

// ---------------------------------------------------------------------------
// Pagination — "Load more" re-fetches the full window at an increased limit.

const INITIAL_LIMIT = 50;
const LOAD_MORE_STEP = 50;
const MAX_LIMIT = 200;

// ---------------------------------------------------------------------------
// Helpers

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return "Something went wrong. Please try again.";
}

function userLabel(user: User | undefined): string {
  return user?.display_name ?? user?.email ?? "Unknown user";
}

// ---------------------------------------------------------------------------
// Page

export default function AdminAuditPage() {
  const [limit, setLimit] = useState(INITIAL_LIMIT);
  const [actorFilter, setActorFilter] = useState<string | null>(null);
  const [familyFilter, setFamilyFilter] = useState<string | null>(null);
  const [detailEntry, setDetailEntry] = useState<AuditEntry | null>(null);

  const {
    data: auditEntries,
    isPending: auditPending,
    isError: auditIsError,
    error: auditError,
    refetch: refetchAudit,
    isFetching: auditFetching,
  } = useQuery({
    queryKey: queryKeys.audit(limit),
    queryFn: () => api.audit.list(limit),
  });

  const {
    data: usersData,
    isPending: usersPending,
  } = useQuery({
    queryKey: ["users"] as const,
    queryFn: api.users.list,
  });

  const usersById = useMemo(
    () => new Map((usersData ?? []).map((user) => [user.id, user])),
    [usersData],
  );

  // Unique actors for filter chips (computed from loaded entries).
  const actorOptions = useMemo(() => {
    if (!auditEntries) return [];
    const seen = new Map<string, string>(); // actor_id → display label
    for (const entry of auditEntries) {
      if (!seen.has(entry.actor_id)) {
        seen.set(entry.actor_id, actorDisplayLabel(entry, usersById));
      }
    }
    return Array.from(seen.entries()).map(([id, label]) => ({ id, label }));
  }, [auditEntries, usersById]);

  const actionFamilies = useMemo(
    () => deriveActionFamilies(auditEntries ?? []),
    [auditEntries],
  );

  // Client-side filter: apply both actor and action family chips.
  const visibleEntries = useMemo(() => {
    if (!auditEntries) return [];
    return auditEntries.filter((entry) => {
      if (actorFilter && entry.actor_id !== actorFilter) return false;
      if (
        familyFilter &&
        (actionMeta(entry.action).family ?? "other") !== familyFilter
      )
        return false;
      return true;
    });
  }, [auditEntries, actorFilter, familyFilter]);

  const canLoadMore =
    !auditIsError &&
    !auditPending &&
    (auditEntries?.length ?? 0) >= limit &&
    limit < MAX_LIMIT;

  function loadMore() {
    setLimit((prev) => Math.min(prev + LOAD_MORE_STEP, MAX_LIMIT));
  }

  const loading = auditPending || usersPending;

  const columns: DataTableColumn<AuditEntry>[] = [
    {
      id: "when",
      header: "When",
      cell: (entry) => <RelativeTime ts={entry.created_at} />,
      hideOnMobile: false,
    },
    {
      id: "actor",
      header: "Actor",
      cell: (entry) => <ActorCell entry={entry} usersById={usersById} />,
    },
    {
      id: "action",
      header: "Action",
      cell: (entry) => <ActionCell entry={entry} />,
    },
    {
      id: "target",
      header: "Target",
      cell: (entry) => <TargetCell entry={entry} />,
      hideOnMobile: true,
    },
  ];

  return (
    <main className="mx-auto flex w-full max-w-[1080px] flex-col gap-8 px-4 py-8 md:px-6">
      <SectionHeader
        kicker="activity"
        title="Audit"
        description="A record of who changed what, when. Newest first."
      />

      {auditIsError && !auditEntries ? (
        <ErrorBanner onRetry={() => void refetchAudit()}>
          Couldn&apos;t load audit events. {errorMessage(auditError)}
        </ErrorBanner>
      ) : (
        <>
          {/* Filter chips — client-side only, no round-trip. */}
          {!loading && (actorOptions.length > 0 || actionFamilies.length > 0) && (
            <FilterBar
              actorOptions={actorOptions}
              actorFilter={actorFilter}
              onActorChange={setActorFilter}
              actionFamilies={actionFamilies}
              familyFilter={familyFilter}
              onFamilyChange={setFamilyFilter}
            />
          )}

          <DataTable
            label="Audit events"
            columns={columns}
            rows={visibleEntries}
            rowKey={(entry) => entry.id}
            loading={loading}
            skeletonRows={8}
            empty={
              actorFilter || familyFilter ? (
                <EmptyState
                  action={
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => {
                        setActorFilter(null);
                        setFamilyFilter(null);
                      }}
                    >
                      Clear filters
                    </Button>
                  }
                >
                  No events match the current filters.
                </EmptyState>
              ) : (
                <EmptyState>No audit events yet.</EmptyState>
              )
            }
            rowMenu={(entry) => (
              <Button
                variant="ghost"
                size="sm"
                className="text-vf-small h-7 px-2"
                onClick={() => setDetailEntry(entry)}
                aria-label="View full event record"
              >
                Details
              </Button>
            )}
          />

          {canLoadMore && (
            <div className="flex justify-center pt-2">
              <Button
                variant="secondary"
                size="sm"
                loading={auditFetching}
                onClick={loadMore}
              >
                Load more
              </Button>
            </div>
          )}
          {!canLoadMore && limit >= MAX_LIMIT && (auditEntries?.length ?? 0) > 0 && (
            <p className="text-vf-small text-vf-subtle text-center">
              Showing the most recent {MAX_LIMIT} events.
            </p>
          )}
        </>
      )}

      {/* Row detail sheet — mono JSON of the full record + copy button. */}
      <Sheet
        open={detailEntry !== null}
        onOpenChange={(open) => {
          if (!open) setDetailEntry(null);
        }}
      >
        {detailEntry && (
          <SheetContent>
            <SheetHeader>
              <SheetTitle>
                {actionMeta(detailEntry.action).phrase}
              </SheetTitle>
              <SheetDescription>
                Full event record — <RelativeTime ts={detailEntry.created_at} />
              </SheetDescription>
            </SheetHeader>
            <AuditDetailPanel entry={detailEntry} />
          </SheetContent>
        )}
      </Sheet>
    </main>
  );
}

// ---------------------------------------------------------------------------
// Actor cell — resolves "cli" as "System"; resolves user ids via the users
// cache; falls back gracefully when the user isn't found.

function actorDisplayLabel(
  entry: AuditEntry,
  usersById: Map<string, User>,
): string {
  if (entry.actor_id === "cli") return "System";
  const user = usersById.get(entry.actor_id);
  return userLabel(user);
}

function ActorCell({
  entry,
  usersById,
}: {
  entry: AuditEntry;
  usersById: Map<string, User>;
}) {
  if (entry.actor_id === "cli") {
    return <span className="text-vf-small text-vf-muted">System</span>;
  }
  const user = usersById.get(entry.actor_id);
  const name = userLabel(user);
  return (
    <EntityRef name={name} id={entry.actor_id} idVisibility="hover" />
  );
}

// ---------------------------------------------------------------------------
// Action cell — human phrase + raw type as micro mono detail.

function ActionCell({ entry }: { entry: AuditEntry }) {
  const { phrase } = actionMeta(entry.action);
  return (
    <span className="flex flex-col gap-0.5">
      <span className="text-vf-small text-vf-text">{phrase}</span>
      <span className="text-vf-micro text-vf-subtle font-mono">
        {entry.action}
      </span>
    </span>
  );
}

// ---------------------------------------------------------------------------
// Target cell — EntityRef when we have a target_id; friendly fallback when
// the event has no target (e.g. operator-wide settings updates).

function TargetCell({ entry }: { entry: AuditEntry }) {
  if (!entry.target_id) {
    return <span className="text-vf-subtle">—</span>;
  }
  // Prefer a name from metadata (e.g. {name: "brother-laptop"}); fall back to
  // the kind label, then to the raw id as the display name.
  const metaName =
    entry.metadata && typeof entry.metadata["name"] === "string"
      ? entry.metadata["name"]
      : null;
  const displayName =
    metaName ??
    (entry.target_kind
      ? `${entry.target_kind.replace(/_/g, " ")}`
      : entry.target_id);

  return <EntityRef name={displayName} id={entry.target_id} />;
}

// ---------------------------------------------------------------------------
// Filter bar — actor chip group + action-family chip group.

function FilterBar({
  actorOptions,
  actorFilter,
  onActorChange,
  actionFamilies,
  familyFilter,
  onFamilyChange,
}: {
  actorOptions: { id: string; label: string }[];
  actorFilter: string | null;
  onActorChange: (id: string | null) => void;
  actionFamilies: string[];
  familyFilter: string | null;
  onFamilyChange: (family: string | null) => void;
}) {
  return (
    <div className="flex flex-wrap gap-4">
      {/* Actor filter */}
      {actorOptions.length > 1 && (
        <ChipGroup label="Actor">
          <FilterChip
            active={actorFilter === null}
            onClick={() => onActorChange(null)}
          >
            All
          </FilterChip>
          {actorOptions.map(({ id, label }) => (
            <FilterChip
              key={id}
              active={actorFilter === id}
              onClick={() => onActorChange(actorFilter === id ? null : id)}
            >
              {label}
            </FilterChip>
          ))}
        </ChipGroup>
      )}

      {/* Action family filter */}
      {actionFamilies.length > 1 && (
        <ChipGroup label="Category">
          <FilterChip
            active={familyFilter === null}
            onClick={() => onFamilyChange(null)}
          >
            All
          </FilterChip>
          {actionFamilies.map((family) => (
            <FilterChip
              key={family}
              active={familyFilter === family}
              onClick={() =>
                onFamilyChange(familyFilter === family ? null : family)
              }
            >
              {family}
            </FilterChip>
          ))}
        </ChipGroup>
      )}
    </div>
  );
}

function ChipGroup({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="text-vf-micro text-vf-subtle font-mono tracking-wider uppercase">
        {label}:
      </span>
      {children}
    </div>
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
          ? "rounded-vf-sm bg-vf-accent/15 text-vf-accent border-vf-accent/30 text-vf-small border px-2.5 py-0.5 font-medium transition-colors"
          : "rounded-vf-sm border-vf-border text-vf-muted hover:text-vf-text hover:border-vf-border/80 text-vf-small border bg-transparent px-2.5 py-0.5 transition-colors"
      }
    >
      {children}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Audit detail sheet panel — mono JSON of the full record + a CopyButton.

function AuditDetailPanel({ entry }: { entry: AuditEntry }) {
  const json = JSON.stringify(entry, null, 2);

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
              name={
                (entry.metadata &&
                  typeof entry.metadata["name"] === "string" &&
                  entry.metadata["name"]) ||
                entry.target_id
              }
              id={entry.target_id}
            />
          </DetailRow>
        )}
        {entry.actor_id && (
          <DetailRow label="Actor ID">
            <span className="text-vf-micro font-mono">{entry.actor_id}</span>
          </DetailRow>
        )}
      </dl>

      {/* Full raw record */}
      <div className="flex flex-col gap-1.5">
        <div className="flex items-center justify-between gap-2">
          <span className="text-vf-micro text-vf-subtle font-mono tracking-wider uppercase">
            Full record
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
      <dt className="text-vf-micro text-vf-subtle shrink-0 font-mono tracking-wider uppercase">
        {label}
      </dt>
      <dd className="text-vf-small text-vf-text min-w-0 text-right">
        {children}
      </dd>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Designed error state.

function ErrorBanner({
  children,
  onRetry,
}: {
  children: ReactNode;
  onRetry: () => void;
}) {
  return (
    <div
      role="alert"
      className="rounded-vf-md border-vf-bad/40 bg-vf-bad/10 flex flex-wrap items-center justify-between gap-3 border p-4"
    >
      <p className="text-vf-small text-vf-text">{children}</p>
      <Button size="sm" onClick={onRetry}>
        Retry
      </Button>
    </div>
  );
}
