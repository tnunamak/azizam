"use client";

/** Admin → Overview.
 *
 * Two-column working page: left = quickstart + metric strip + apps table;
 * right = recent activity. Bottom row: base URL + health/services summary.
 * No marketing copy; no decorative charts — only real status, auth, and
 * activity series. Empty/loading states are distinct from errors.
 */

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  ActivityFeedRow,
  AppAccessRow,
  Avatar,
  avatarToneFor,
  Badge,
  Button,
  CopyField,
  EmptyOperationalState,
  IncidentBanner,
  MetricStrip,
  ObjectList,
  ObjectRow,
  PanelHeader,
  ProductPanel,
  QuickStartChecklist,
  RelativeTime,
  SectionActionHeader,
  ServicesIcon,
  Skeleton,
  StatusAdvisory,
  SystemHealthBand,
  type ChartPoint,
} from "@/components/ui";
import {
  api,
  queryKeys,
  type AttentionItem,
  type AuditEntry,
  type Credential,
  type Service,
  type StatusPayload,
  type StatusSeriesPoint,
  type User,
} from "@/lib/api";
import {
  appCredentialFacts,
  buildTelemetryCards,
  isActiveAppCredentialFor,
  isActiveKey,
} from "./overview-derivations";
import {
  adminAppHref,
  adminAuditHref,
  adminServiceHref,
  adminTargetHref,
} from "@/lib/admin-links";
import { projectStatusHealth } from "@/lib/status-health-projection";

// ---------------------------------------------------------------------------
// Human phrases for audit action types

const ACTION_LABELS: Record<string, string> = {
  "app.create": "Created app",
  "app.disable": "Disabled app",
  "app.enable": "Re-enabled app",
  "app.self_create": "Created personal app",
  "credential.create": "Created key",
  "credential.revoke": "Revoked key",
  "profile.create": "Created profile",
  "profile.update": "Updated profile",
  "profile.delete": "Deleted profile",
  "admin_key.create": "Created admin key",
  "runtime_profile_mapping.create": "Created access mapping",
  "runtime_profile_mapping.disable": "Disabled access mapping",
  "operator.user_access_defaults.update": "Updated user defaults",
  "operator.service_availability.update": "Updated service availability",
  "operator.media_job_admission.update": "Updated media admission",
  "operator.service_registry.update": "Updated service registry",
  "operator.service_registry.seed": "Seeded service registry",
  "operator.service.added": "Added service",
  "operator.service.updated": "Updated service",
  "operator.service.removed": "Removed service",
  "operator.service.toggled": "Toggled service",
  "operator.extension_package.enable": "Enabled extension",
  "operator.oidc_provider_config.update": "Updated sign-in config",
  "setup.local_owner_create": "Created local owner account",
  "setup.local_owner_login": "Signed in locally",
};

function actionLabel(action: string): string {
  if (action in ACTION_LABELS) return ACTION_LABELS[action]!;
  const parts = action.split(".");
  const last = parts[parts.length - 1] ?? action;
  return last.charAt(0).toUpperCase() + last.slice(1).replace(/_/g, " ");
}

function serviceList(
  registry: Awaited<ReturnType<typeof api.services.registry>> | undefined,
): Service[] {
  return Object.values(registry?.services ?? {}).sort((a, b) =>
    a.name.localeCompare(b.name),
  );
}

function sumSeries(
  seriesByName: Record<string, StatusSeriesPoint[]> | undefined,
): ChartPoint[] {
  const buckets = new Map<number, number>();
  for (const series of Object.values(seriesByName ?? {})) {
    for (const point of series) {
      if (
        !Array.isArray(point) ||
        point.length < 2 ||
        !Number.isFinite(point[0]) ||
        !Number.isFinite(point[1])
      ) {
        continue;
      }
      buckets.set(point[0], (buckets.get(point[0]) ?? 0) + point[1]);
    }
  }
  return [...buckets.entries()]
    .sort(([a], [b]) => a - b)
    .map(([ts, value]) => ({
      x: ts,
      y: value,
      label: new Date(ts * 1000).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      }),
    }));
}

function formatCompact(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return new Intl.NumberFormat(undefined, {
    notation: Math.abs(value) >= 10_000 ? "compact" : "standard",
    maximumFractionDigits: value < 10 ? 1 : 0,
  }).format(value);
}

/** Returns null (render nothing) rather than claim staleness we can't prove:
 * an absent timestamp means the source didn't supply one, not that the
 * observation is known to be stale. */
function statusFreshness(generatedAt: number | undefined): string | null {
  if (!generatedAt || !Number.isFinite(generatedAt)) {
    return null;
  }
  const ageSeconds = Math.max(0, Math.floor(Date.now() / 1000 - generatedAt));
  if (ageSeconds < 60) return "Updated just now";
  if (ageSeconds < 3_600) return `Updated ${Math.floor(ageSeconds / 60)}m ago`;
  if (ageSeconds < 86_400)
    return `Updated ${Math.floor(ageSeconds / 3_600)}h ago`;
  return `Updated ${Math.floor(ageSeconds / 86_400)}d ago`;
}

function userLabel(user: User): string {
  return user.display_name ?? user.email ?? user.name ?? "Unnamed user";
}

function auditActorLabel(entry: AuditEntry): string {
  return entry.actor_label;
}

// ---------------------------------------------------------------------------
// Attention section — bounded, grouped advisories; never an unbounded dump.

/** Overview shows compact advisories, not a data dump: cap the visible rows
 * and group identical-shape items (e.g. "N apps have no active key") so a
 * fixture or instance with many similar problems doesn't bury the page. */
const ATTENTION_VISIBLE_CAP = 5;
const ATTENTION_GROUP_THRESHOLD = 3;

interface AttentionRow {
  key: string;
  title: string;
  detail: string;
  tone: "warn" | "bad" | "info";
  href?: string;
  side?: string;
  count: number;
}

function groupAttentionItems(items: AttentionItem[]): AttentionRow[] {
  const byShape = new Map<string, AttentionItem[]>();
  for (const item of items) {
    const shapeKey = `${item.kind}:${item.detail}`;
    const bucket = byShape.get(shapeKey);
    if (bucket) bucket.push(item);
    else byShape.set(shapeKey, [item]);
  }

  const rows: AttentionRow[] = [];
  for (const [shapeKey, group] of byShape) {
    const first = group[0]!;
    const tone = first.severity === "problem" ? "bad" : "warn";
    if (group.length < ATTENTION_GROUP_THRESHOLD) {
      for (const item of group) {
        const href = adminTargetHref(item.target_kind, item.target_id) ?? undefined;
        rows.push({
          key: `${shapeKey}:${item.target_id ?? item.title}`,
          title: item.title,
          detail: item.detail,
          tone,
          href,
          side: href ? "View →" : undefined,
          count: 1,
        });
      }
      continue;
    }
    // Same kind + detail across many targets: one summarized row rather than
    // N identical-looking rows. There is no single real target for a
    // summarized row, so — unlike the per-item rows above — it carries no
    // href/"View" action rather than guessing a listing URL.
    rows.push({
      key: shapeKey,
      title: `${group.length} ${first.kind}s need attention`,
      detail: first.detail,
      tone,
      count: group.length,
    });
  }
  return rows;
}

function AttentionSection({
  items,
  loading,
  error,
  onRetry,
}: {
  items: AttentionItem[];
  loading: boolean;
  error: boolean;
  onRetry: () => void;
}) {
  const [expanded, setExpanded] = useState(false);

  if (error) {
    return (
      <IncidentBanner
        tone="bad"
        title="Couldn't load attention items"
        detail="The rest of Overview remains available."
        actions={
          <Button variant="ghost" size="sm" onClick={onRetry}>
            Retry
          </Button>
        }
      />
    );
  }
  if (loading) {
    return <Skeleton className="h-20 w-full" />;
  }
  if (items.length === 0) return null;

  const problemCount = items.filter((i) => i.severity === "problem").length;
  const tone = problemCount > 0 ? ("bad" as const) : ("warn" as const);
  const headline = `${items.length} item${items.length === 1 ? "" : "s"} need${items.length === 1 ? "s" : ""} attention`;

  const rows = groupAttentionItems(items);
  const visibleRows = expanded ? rows : rows.slice(0, ATTENTION_VISIBLE_CAP);
  const hiddenCount = rows.length - visibleRows.length;

  return (
    <StatusAdvisory
      tone={tone}
      title={headline}
      items={visibleRows.map((row) => ({
        title: row.title,
        detail: row.detail,
        tone: row.tone,
        href: row.href,
        side: row.side,
      }))}
      footer={
        hiddenCount > 0 ? (
          <Button variant="ghost" size="sm" onClick={() => setExpanded(true)}>
            Show {hiddenCount} more
          </Button>
        ) : expanded && rows.length > ATTENTION_VISIBLE_CAP ? (
          <Button variant="ghost" size="sm" onClick={() => setExpanded(false)}>
            Show fewer
          </Button>
        ) : undefined
      }
    />
  );
}

// ---------------------------------------------------------------------------
// Apps table (compact list for the left column)

function AppsSection({
  apps,
  credentialsData,
  credentialFactsState,
  usersById,
  ownerFactsState,
  statusData,
}: {
  apps: Awaited<ReturnType<typeof api.apps.list>>;
  credentialsData: Credential[] | undefined;
  credentialFactsState: "ready" | "loading" | "unavailable";
  usersById: Map<string, User>;
  ownerFactsState: "ready" | "loading" | "unavailable";
  statusData: StatusPayload | undefined;
}) {
  if (apps.length === 0) {
    return (
      <EmptyOperationalState
        title="No apps yet."
        detail="Create an app to start routing requests."
        action={
          <Button asChild variant="primary" size="sm">
            <Link href="/admin/apps">Create app</Link>
          </Button>
        }
      />
    );
  }

  return (
    <div className="overflow-hidden rounded-md border border-[var(--vf-border)]">
      {/* Column header row */}
      <div className="hidden items-center gap-2.5 border-b border-[var(--vf-border)] bg-[var(--vf-surface-1)] px-3 py-1.5 min-[980px]:flex">
        <span className="w-[34px] shrink-0" />
        <span className="text-vf-micro text-vf-subtle min-w-0 flex-1 font-mono uppercase">
          App name
        </span>
        <span className="text-vf-micro text-vf-subtle w-20 shrink-0 text-right font-mono uppercase">
          Last used
        </span>
        <span className="text-vf-micro text-vf-subtle w-14 shrink-0 text-right font-mono uppercase">
          Status
        </span>
      </div>
      <div className="vf-activity-feed">
        {apps.slice(0, 5).map((app) => {
          const credentialFacts = appCredentialFacts(credentialsData, app.id);
          const t = statusData?.auth_keys?.find((k) => k.app_id === app.id);
          const lastSeenTs = t?.last_seen ? Math.floor(t.last_seen) : null;
          const credTs = credentialFacts?.lastUsedAt ?? null;
          const ts = lastSeenTs ?? credTs;
          const lastUsedLabel = ts
            ? (() => {
                const diffSec = Math.floor(Date.now() / 1000) - ts;
                if (diffSec < 60) return "just now";
                if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
                if (diffSec < 86400)
                  return `${Math.floor(diffSec / 3600)}h ago`;
                return `${Math.floor(diffSec / 86400)}d ago`;
              })()
            : undefined;
          return (
            <AppAccessRow
              key={app.id}
              name={app.name}
              avatar={
                <Avatar
                  initials={app.name.slice(0, 2)}
                  tone={avatarToneFor(app.name)}
                />
              }
              owner={
                app.owner_user_id
                  ? ownerFactsState === "loading"
                    ? "Owner loading"
                    : ownerFactsState === "unavailable"
                      ? "Owner unavailable"
                      : (() => {
                          const owner = usersById.get(app.owner_user_id!);
                          return owner ? userLabel(owner) : "Owner unavailable";
                        })()
                  : undefined
              }
              profile={app.effective_profile_name}
              keyCount={
                credentialFactsState === "loading"
                  ? "Keys loading"
                  : credentialFactsState === "unavailable"
                    ? "Keys unavailable"
                    : credentialFacts?.activeKeyCount
              }
              lastUsedLabel={lastUsedLabel}
              status={app.disabled_at !== null ? "warn" : "ok"}
              statusLabel={app.disabled_at !== null ? "disabled" : "active"}
              href={adminAppHref(app.id)}
            />
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Recent activity feed (right rail)

function RecentActivitySection({
  entries,
  loading,
  error,
  onRetry,
}: {
  entries: AuditEntry[] | undefined;
  loading: boolean;
  error: boolean;
  onRetry: () => void;
}) {
  if (error) {
    return (
      <div className="rounded-md border border-[var(--vf-border)] px-4 py-3">
        <IncidentBanner
          tone="bad"
          title="Couldn't load recent activity"
          detail="Other Overview data remains available."
          actions={
            <Button variant="ghost" size="sm" onClick={onRetry}>
              Retry
            </Button>
          }
        />
      </div>
    );
  }
  if (loading) {
    return (
      <div className="rounded-md border border-[var(--vf-border)]">
        <div className="flex flex-col gap-2 p-3">
          <Skeleton className="h-12 w-full" />
          <Skeleton className="h-12 w-full" />
          <Skeleton className="h-12 w-full" />
        </div>
      </div>
    );
  }
  if (!entries || entries.length === 0) {
    return (
      <div className="rounded-md border border-[var(--vf-border)]">
        <EmptyOperationalState
          title="No activity yet."
          detail="Control-plane changes will appear here."
        />
      </div>
    );
  }
  return (
    <div className="overflow-hidden rounded-md border border-[var(--vf-border)]">
      <div className="vf-activity-feed">
        {entries.map((entry) => {
          const actor = auditActorLabel(entry);
          const targetName =
            entry.metadata && typeof entry.metadata["name"] === "string"
              ? entry.metadata["name"]
              : undefined;
          return (
            <ActivityFeedRow
              key={entry.id}
              href={
                adminTargetHref(entry.target_kind, entry.target_id) ??
                adminAuditHref(entry.id)
              }
              tone="info"
              title={actionLabel(entry.action)}
              detail={targetName ?? undefined}
              actor={actor !== "Unknown user" ? actor : undefined}
              time={<RelativeTime ts={entry.created_at} />}
            />
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page

export default function AdminOverviewPage() {
  const [baseUrl, setBaseUrl] = useState("/v1");
  const {
    data: overview,
    isPending: overviewPending,
    isError: overviewError,
    refetch: refetchOverview,
  } = useQuery({
    queryKey: queryKeys.overview,
    queryFn: api.overview,
  });

  const {
    data: auditEntries,
    isPending: auditPending,
    isError: auditError,
    refetch: refetchAudit,
  } = useQuery({
    queryKey: queryKeys.auditRecent,
    queryFn: () => api.audit.list({ limit: 8 }).then((r) => r.records),
  });

  const {
    data: usersData,
    isError: usersError,
    refetch: refetchUsers,
  } = useQuery({
    queryKey: queryKeys.users,
    queryFn: api.users.list,
  });
  const {
    data: appsData,
    isError: appsError,
    refetch: refetchApps,
  } = useQuery({
    queryKey: queryKeys.apps,
    queryFn: api.apps.list,
  });
  const {
    data: credentialsData,
    isError: credentialsError,
    refetch: refetchCredentials,
  } = useQuery({
    queryKey: queryKeys.credentials,
    queryFn: api.credentials.list,
  });
  const {
    data: registry,
    isError: registryError,
    refetch: refetchRegistry,
  } = useQuery({
    queryKey: queryKeys.serviceRegistry,
    queryFn: api.services.registry,
  });
  const {
    data: statusData,
    isError: statusError,
    isPending: statusPending,
    refetch: refetchStatus,
  } = useQuery({
    queryKey: queryKeys.status,
    queryFn: api.status,
    staleTime: 10_000,
  });

  const usersById = useMemo(
    () => new Map((usersData ?? []).map((user) => [user.id, user])),
    [usersData],
  );
  const apps = useMemo(
    () => [...(appsData ?? [])].sort((a, b) => b.created_at - a.created_at),
    [appsData],
  );
  const firstApp = apps[0] ?? null;
  const services = useMemo(() => serviceList(registry), [registry]);
  const enabledServices = services.filter((service) => service.enabled).length;
  const activeKeys = useMemo(
    () => (credentialsData ?? []).filter(isActiveKey),
    [credentialsData],
  );
  const hasFirstAppCredential = useMemo(
    () =>
      firstApp !== null &&
      (credentialsData ?? []).some((credential) =>
        isActiveAppCredentialFor(credential, firstApp.id),
      ),
    [credentialsData, firstApp],
  );
  const telemetry = useMemo(
    () => ({
      ...buildTelemetryCards(statusData),
      requestSeries: sumSeries(
        statusData?.metrics?.trends?.per_key_request_series_1h,
      ),
    }),
    [statusData],
  );

  // Keep setup visible until each real prerequisite is in place.
  const setupIncomplete =
    appsData === undefined ||
    credentialsData === undefined ||
    apps.length === 0 ||
    !hasFirstAppCredential ||
    enabledServices === 0;

  const attention = overview?.attention ?? [];
  const hasAttention = overviewPending || overviewError || attention.length > 0;
  const ownerFactsState =
    usersData === undefined
      ? usersError
        ? ("unavailable" as const)
        : ("loading" as const)
      : ("ready" as const);
  const credentialFactsState =
    credentialsData === undefined
      ? credentialsError
        ? ("unavailable" as const)
        : ("loading" as const)
      : ("ready" as const);

  useEffect(() => {
    setBaseUrl(`${window.location.origin}/v1`);
  }, []);

  const health = useMemo(
    () =>
      projectStatusHealth({
        data: statusData,
        isPending: statusPending,
        isError: statusError,
      }),
    [statusData, statusPending, statusError],
  );
  const liveServiceById = useMemo(() => {
    const map = new Map<
      string,
      NonNullable<StatusPayload["current_health"]>["services"][number]
    >();
    for (const service of statusData?.current_health?.services ?? []) {
      map.set(service.service_id, service);
    }
    return map;
  }, [statusData]);

  return (
    <div className="flex flex-col gap-5">
      {/* Page header */}
      <SectionActionHeader
        as="h1"
        kicker="operator"
        title="Overview"
        action={
          <Button asChild variant="primary">
            <Link href="/admin/apps">+ Create app</Link>
          </Button>
        }
      />

      <SystemHealthBand
        tone={health.tone}
        title={health.label}
        detail={(() => {
          const freshness = statusFreshness(statusData?.generated_at);
          return freshness ? `${health.detail} ${freshness}.` : health.detail;
        })()}
        metrics={[
          {
            label: "Apps",
            value: appsData === undefined ? "—" : String(apps.length),
            detail: "configured",
          },
          {
            label: "Active keys",
            value:
              credentialsData === undefined ? "—" : String(activeKeys.length),
            detail: "credentials",
          },
          {
            label: "Services",
            value:
              registry === undefined
                ? "—"
                : `${enabledServices}/${services.length}`,
            detail: "enabled",
            tone: "neutral",
          },
        ]}
        action={
          <Button asChild variant="ghost" size="sm">
            <Link href="/admin/status">Diagnose status</Link>
          </Button>
        }
      />

      {statusError && (
        <IncidentBanner
          tone="bad"
          title="Couldn't load live status"
          detail="Known apps and activity remain visible."
          actions={
            <Button
              variant="ghost"
              size="sm"
              onClick={() => void refetchStatus()}
            >
              Retry
            </Button>
          }
        />
      )}

      {/* Attention banners — shown only when items exist or erroring */}
      {hasAttention && (
        <AttentionSection
          items={attention}
          loading={overviewPending}
          error={overviewError}
          onRetry={() => void refetchOverview()}
        />
      )}

      {/* QuickStart stepper — visible until all prerequisites are complete */}
      {setupIncomplete && (
        <QuickStartChecklist
          items={[
            {
              id: "create-app",
              label: "Create an app",
              detail: "Register your first caller identity.",
              complete: apps.length > 0,
              action: (
                <Button asChild variant="ghost" size="sm">
                  <Link href="/admin/apps">Create app</Link>
                </Button>
              ),
            },
            {
              id: "mint-key",
              label: "Generate an API key",
              detail: "Mint a key for the app so callers can authenticate.",
              complete: hasFirstAppCredential,
              action: firstApp ? (
                <Button asChild variant="ghost" size="sm">
                  <Link href={adminAppHref(firstApp.id)}>Create key</Link>
                </Button>
              ) : (
                <Button variant="ghost" size="sm" disabled>
                  Create key
                </Button>
              ),
            },
            {
              id: "configure-service",
              label: "Configure a service",
              detail: "Add or enable a service before routing requests.",
              complete: enabledServices > 0,
              action: (
                <Button asChild variant="ghost" size="sm">
                  <Link href="/admin/services">Configure service</Link>
                </Button>
              ),
            },
            {
              id: "send-test-request",
              label: "Send a test request",
              detail: "Call the base URL with your key to confirm it routes.",
              complete: Boolean(
                telemetry.requestWindowAvailable && (telemetry.requests24h ?? 0) > 0,
              ),
              action: (
                <Button asChild variant="ghost" size="sm">
                  <Link href="#base-url">View base URL</Link>
                </Button>
              ),
            },
          ]}
        />
      )}

      {(telemetry.requestWindowAvailable || telemetry.errorWindowAvailable) && (
        <MetricStrip
          items={[
            ...(telemetry.requestWindowAvailable
              ? [
                  {
                    label: "Requests",
                    value: formatCompact(telemetry.requests24h),
                    detail: "last 24h",
                    tone: "info" as const,
                    series:
                      telemetry.requestSeries.length >= 3
                        ? telemetry.requestSeries
                        : undefined,
                  },
                ]
              : []),
            ...(telemetry.errorWindowAvailable
              ? [
                  {
                    label: "Failed requests",
                    value: formatCompact(telemetry.failedRequests24h),
                    detail: "last 24h",
                    tone:
                      telemetry.failedRequests24h! > 0
                        ? ("warn" as const)
                        : ("neutral" as const),
                  },
                ]
              : []),
          ]}
        />
      )}

      {/* Two-column body: left = apps table, right = activity feed */}
      <div className="grid gap-5 min-[980px]:grid-cols-[minmax(0,1.35fr)_minmax(18rem,0.85fr)]">
        {/* LEFT — apps table */}
        <div className="min-w-0">
          <section>
            <SectionActionHeader
              title="Your apps"
              count={appsData !== undefined ? apps.length : undefined}
              action={
                <Link
                  href="/admin/apps"
                  className="text-vf-small text-vf-muted hover:text-vf-text"
                >
                  View all →
                </Link>
              }
            />
            <div className="mt-3">
              {appsError && appsData === undefined ? (
                <IncidentBanner
                  tone="bad"
                  title="Couldn't load apps"
                  detail="Retry to load configured callers."
                  actions={
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => void refetchApps()}
                    >
                      Retry
                    </Button>
                  }
                />
              ) : appsData === undefined ? (
                <div className="flex flex-col gap-2">
                  <Skeleton className="h-12 w-full" />
                  <Skeleton className="h-12 w-full" />
                  <Skeleton className="h-12 w-full" />
                </div>
              ) : (
                <div className="flex flex-col gap-3">
                  {appsError && (
                    <IncidentBanner
                      tone="bad"
                      title="Apps may be stale"
                      detail="Couldn't refresh the app list."
                      actions={
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => void refetchApps()}
                        >
                          Retry
                        </Button>
                      }
                    />
                  )}
                  {credentialsError && (
                    <IncidentBanner
                      tone="bad"
                      title={
                        credentialsData === undefined
                          ? "Couldn't load app key facts"
                          : "App key facts may be stale"
                      }
                      detail="Key counts and credential last-used times are unavailable until this retry succeeds."
                      actions={
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => void refetchCredentials()}
                        >
                          Retry
                        </Button>
                      }
                    />
                  )}
                  {usersError && (
                    <IncidentBanner
                      tone="bad"
                      title={
                        usersData === undefined
                          ? "Couldn't load app owners"
                          : "App owner details may be stale"
                      }
                      detail="Owner names are unavailable until this retry succeeds."
                      actions={
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => void refetchUsers()}
                        >
                          Retry
                        </Button>
                      }
                    />
                  )}
                  <AppsSection
                    apps={apps}
                    credentialsData={credentialsData}
                    credentialFactsState={credentialFactsState}
                    usersById={usersById}
                    ownerFactsState={ownerFactsState}
                    statusData={statusData}
                  />
                </div>
              )}
            </div>
            {appsData !== undefined && apps.length > 0 && (
              <p className="text-vf-micro text-vf-muted mt-2">
                Showing {Math.min(5, apps.length)} of {apps.length} app
                {apps.length === 1 ? "" : "s"}
              </p>
            )}
          </section>
        </div>

        {/* RIGHT — activity feed */}
        <div className="min-w-0">
          <section>
            <SectionActionHeader
              title="Recent activity"
              action={
                <Link
                  href="/admin/audit"
                  className="text-vf-small text-vf-muted hover:text-vf-text"
                >
                  View all →
                </Link>
              }
            />
            <div className="mt-3">
              <RecentActivitySection
                entries={auditEntries}
                loading={auditPending}
                error={auditError}
                onRetry={() => void refetchAudit()}
              />
            </div>
          </section>
        </div>
      </div>

      {/* Bottom row: Base URL (left) + Health/Services (right) */}
      <div className="grid gap-5 min-[980px]:grid-cols-[minmax(0,1.35fr)_minmax(18rem,0.85fr)]">
        {/* Base URL */}
        <section id="base-url">
          <SectionActionHeader
            title="Base URL"
            description="Use this URL for all API requests."
          />
          <div className="mt-3">
            <ProductPanel tight>
              <CopyField
                value={baseUrl}
                monospace
                description="Works with OpenAI-compatible SDKs and tools."
              />
            </ProductPanel>
          </div>
        </section>

        {/* Services */}
        <section>
          <SectionActionHeader title="Services" />
          <div className="mt-3 flex flex-col gap-3">
            {registryError ? (
              <IncidentBanner
                tone="bad"
                title="Couldn't load services"
                detail="Live health and configured services are separate facts."
                actions={
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => void refetchRegistry()}
                  >
                    Retry
                  </Button>
                }
              />
            ) : registry === undefined ? (
              <div className="flex flex-col gap-2">
                <Skeleton className="h-12 w-full" />
                <Skeleton className="h-12 w-full" />
              </div>
            ) : services.length === 0 ? (
              <EmptyOperationalState
                title="No services configured."
                detail="Add a service before the gateway can route requests."
                action={
                  <Button asChild variant="ghost" size="sm">
                    <Link href="/admin/services">Open services</Link>
                  </Button>
                }
              />
            ) : services.length > 0 ? (
              <ProductPanel tight>
                <PanelHeader
                  title={`${enabledServices} of ${services.length} enabled`}
                  action={
                    <Link
                      href="/admin/services"
                      className="text-vf-small text-vf-accent hover:text-vf-text"
                    >
                      Manage
                    </Link>
                  }
                />
                <ObjectList className="mt-2">
                  {services.slice(0, 4).map((service) => {
                    const live = liveServiceById.get(service.id);
                    const liveStatus = live?.observation ?? "unavailable";
                    const tone =
                      live?.tone === "ok"
                        ? "ok"
                        : live?.tone === "warn"
                          ? "warn"
                          : live?.tone === "bad"
                            ? "bad"
                            : "neutral";
                    const label = liveStatus.replace(/_/g, " ");
                    return (
                      <ObjectRow
                        key={service.id}
                        href={adminServiceHref(service.id)}
                        icon={<ServicesIcon className="size-4" />}
                        title={service.name}
                        meta={
                          live?.last_error ??
                          service.config.base_url ??
                          service.config.host ??
                          "managed"
                        }
                        side={
                          <Badge tone={tone} sm>
                            {label}
                          </Badge>
                        }
                      />
                    );
                  })}
                </ObjectList>
              </ProductPanel>
            ) : null}
          </div>
        </section>
      </div>
    </div>
  );
}
