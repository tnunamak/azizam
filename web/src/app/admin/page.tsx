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
  StatusDotLabel,
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

function isActiveKey(credential: Credential): boolean {
  return credential.revoked_at === null;
}

function serviceList(
  registry: Awaited<ReturnType<typeof api.services.registry>> | undefined,
): Service[] {
  return Object.values(registry?.services ?? {}).sort((a, b) =>
    a.name.localeCompare(b.name),
  );
}

function asChartSeries(series: StatusSeriesPoint[] | undefined): ChartPoint[] {
  return (series ?? [])
    .filter(
      (point) =>
        Array.isArray(point) &&
        point.length >= 2 &&
        Number.isFinite(point[0]) &&
        Number.isFinite(point[1]),
    )
    .map(([ts, value]) => ({
      x: ts,
      y: value,
      label: new Date(ts * 1000).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      }),
    }));
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

function latestValue(series: ChartPoint[]): number | null {
  const last = series.at(-1);
  return last && Number.isFinite(last.y) ? last.y : null;
}

function formatCompact(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return new Intl.NumberFormat(undefined, {
    notation: Math.abs(value) >= 10_000 ? "compact" : "standard",
    maximumFractionDigits: value < 10 ? 1 : 0,
  }).format(value);
}

function buildTelemetryCards(status: StatusPayload | undefined) {
  const requestSeries = sumSeries(
    status?.metrics?.trends?.per_key_request_series_1h,
  );
  const requests24h = (status?.auth_keys ?? []).reduce((sum, key) => {
    const value = key.requests_24h;
    return (
      sum + (typeof value === "number" && Number.isFinite(value) ? value : 0)
    );
  }, 0);

  const firstGpu = status?.metrics?.gpus?.[0];
  const vramSeries = asChartSeries(firstGpu?.vram_pct_24h_series);
  const tempSeries = asChartSeries(firstGpu?.temp_24h_series);
  const xidSeries = asChartSeries(status?.metrics?.trends?.xid_109_hourly_24h);
  const xid24h = status?.metrics?.trends?.xid_109_24h_count;

  return {
    requestSeries,
    requests24h,
    vramSeries,
    vramValue: latestValue(vramSeries) ?? firstGpu?.vram_pct_max_24h ?? null,
    tempSeries,
    tempValue: latestValue(tempSeries) ?? firstGpu?.temp_max_24h_c ?? null,
    xidSeries,
    xid24h:
      typeof xid24h === "number" && Number.isFinite(xid24h) ? xid24h : null,
  };
}

function userLabel(user: User | undefined): string {
  return user?.display_name ?? user?.email ?? "Unknown user";
}

function auditActorLabel(
  entry: AuditEntry,
  usersById: Map<string, User>,
): string {
  if (entry.actor_id === "cli" || entry.actor_kind === "cli") return "System";
  const user = usersById.get(entry.actor_id);
  if (user) return userLabel(user);
  if (
    entry.actor_kind === "service_account" ||
    entry.actor_id.startsWith("svc_")
  ) {
    return "Admin key";
  }
  if (entry.actor_kind === "admin") return "Admin";
  return "Unknown user";
}

// ---------------------------------------------------------------------------
// Attention section — one IncidentBanner per item, or an all-clear card

function AttentionSection({
  items,
  loading,
  error,
}: {
  items: AttentionItem[];
  loading: boolean;
  error: boolean;
}) {
  if (error) {
    return (
      <IncidentBanner
        tone="bad"
        title="Couldn't load attention items"
        detail="Check the gateway log for errors."
      />
    );
  }
  if (loading) {
    return null;
  }
  if (items.length === 0) return null;

  const problemCount = items.filter((i) => i.severity === "problem").length;
  const tone = problemCount > 0 ? ("bad" as const) : ("warn" as const);
  const headline = `${items.length} item${items.length === 1 ? "" : "s"} need${items.length === 1 ? "s" : ""} attention`;

  return (
    <StatusAdvisory
      tone={tone}
      title={headline}
      items={items.map((item) => {
        const href =
          adminTargetHref(item.target_kind, item.target_id) ?? undefined;
        return {
          title: item.title,
          detail: item.detail,
          tone: item.severity === "problem" ? "bad" : "warn",
          href,
          side: href ? "View →" : undefined,
        };
      })}
    />
  );
}

// ---------------------------------------------------------------------------
// Apps table (compact list for the left column)

function AppsSection({
  apps,
  credentialsData,
  usersById,
  statusData,
}: {
  apps: Awaited<ReturnType<typeof api.apps.list>>;
  credentialsData: Credential[] | undefined;
  usersById: Map<string, User>;
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
          const appActiveKeys = (credentialsData ?? []).filter(
            (c) =>
              c.subject_kind === "app" &&
              c.app_id === app.id &&
              c.revoked_at === null,
          ).length;
          const t = statusData?.auth_keys?.find((k) => k.app_id === app.id);
          const lastSeenTs = t?.last_seen ? Math.floor(t.last_seen) : null;
          const credTs = (credentialsData ?? [])
            .filter(
              (c) =>
                c.subject_kind === "app" &&
                c.app_id === app.id &&
                c.last_used_at !== null,
            )
            .reduce<number | null>((best, c) => {
              const v = c.last_used_at!;
              return best === null || v > best ? v : best;
            }, null);
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
                  ? userLabel(usersById.get(app.owner_user_id))
                  : undefined
              }
              profile={app.effective_profile_name}
              keyCount={appActiveKeys}
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
  usersById,
  loading,
  error,
}: {
  entries: AuditEntry[] | undefined;
  usersById: Map<string, User>;
  loading: boolean;
  error: boolean;
}) {
  if (error) {
    return (
      <div className="rounded-md border border-[var(--vf-border)] px-4 py-3">
        <IncidentBanner
          tone="bad"
          title="Couldn't load recent activity"
          detail="Check the gateway log for errors."
        />
      </div>
    );
  }
  if (loading) {
    return (
      <div className="rounded-md border border-[var(--vf-border)]">
        <EmptyOperationalState
          title="Loading activity"
          detail="Recent gateway events will appear here."
          tone="info"
        />
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
          const actor = auditActorLabel(entry, usersById);
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
  } = useQuery({
    queryKey: queryKeys.overview,
    queryFn: api.overview,
  });

  const {
    data: auditEntries,
    isPending: auditPending,
    isError: auditError,
  } = useQuery({
    queryKey: queryKeys.auditRecent,
    queryFn: () => api.audit.list({ limit: 8 }).then((r) => r.records),
  });

  const { data: usersData, isPending: usersPending } = useQuery({
    queryKey: queryKeys.users,
    queryFn: api.users.list,
  });
  const { data: appsData } = useQuery({
    queryKey: queryKeys.apps,
    queryFn: api.apps.list,
  });
  const { data: credentialsData } = useQuery({
    queryKey: queryKeys.credentials,
    queryFn: api.credentials.list,
  });
  const { data: registry } = useQuery({
    queryKey: queryKeys.serviceRegistry,
    queryFn: api.services.registry,
  });
  const {
    data: statusData,
    isError: statusError,
    isPending: statusPending,
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
  const telemetry = useMemo(
    () => buildTelemetryCards(statusData),
    [statusData],
  );

  // Show quickstart until all four conditions hold.
  const setupIncomplete =
    appsData === undefined ||
    credentialsData === undefined ||
    apps.length === 0 ||
    activeKeys.length === 0 ||
    enabledServices === 0 ||
    telemetry.requests24h === 0;

  const attention = overview?.attention ?? [];
  const hasAttention = overviewError || attention.length > 0;

  useEffect(() => {
    setBaseUrl(`${window.location.origin}/v1`);
  }, []);

  const health = projectStatusHealth({
    data: statusData,
    isPending: statusPending,
    isError: statusError,
  });
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
        kicker="operator"
        title="Overview"
        action={
          <Button asChild variant="primary">
            <Link href="/admin/apps">+ Create app</Link>
          </Button>
        }
      />

      {/* Attention banners — shown only when items exist or erroring */}
      {hasAttention && (
        <AttentionSection
          items={attention}
          loading={overviewPending}
          error={overviewError}
        />
      )}

      {/* QuickStart stepper — visible until all 4 steps complete */}
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
              complete: activeKeys.length > 0,
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
              id: "enable-service",
              label: "Copy base URL",
              detail: "Use this URL as your OpenAI base URL.",
              complete: enabledServices > 0,
              action: (
                <Button asChild variant="ghost" size="sm">
                  <Link href="#base-url">Show URL</Link>
                </Button>
              ),
            },
            {
              id: "send-request",
              label: "Send a test request",
              detail: "Confirm the gateway routes requests end to end.",
              complete: telemetry.requests24h > 0,
              action: (
                <Button asChild variant="ghost" size="sm">
                  <Link href="/admin/services">Docs</Link>
                </Button>
              ),
            },
          ]}
        />
      )}

      {/* KPI strip — always visible */}
      <MetricStrip
        items={[
          {
            label: "Apps",
            value: appsData !== undefined ? String(apps.length) : "—",
            detail: "registered",
          },
          {
            label: "Active keys",
            value:
              credentialsData !== undefined ? String(activeKeys.length) : "—",
            detail: "credentials",
          },
          {
            label: "Requests",
            value: formatCompact(
              telemetry.requests24h > 0 ? telemetry.requests24h : null,
            ),
            detail: "24h",
            tone: "info" as const,
            series:
              telemetry.requestSeries.length >= 3
                ? telemetry.requestSeries
                : undefined,
          },
          {
            label: "Services on",
            value:
              registry !== undefined
                ? `${enabledServices}/${services.length || 0}`
                : "—",
            detail: "enabled",
            tone:
              services.length > 0 && enabledServices < services.length
                ? ("warn" as const)
                : services.length > 0
                  ? ("ok" as const)
                  : ("neutral" as const),
          },
        ]}
      />

      {/* Two-column body: left = apps table, right = activity feed */}
      <div className="vf-overview-body">
        {/* LEFT — apps table */}
        <div className="vf-overview-left">
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
              {appsData === undefined ? (
                <div className="flex flex-col gap-2">
                  <Skeleton className="h-12 w-full" />
                  <Skeleton className="h-12 w-full" />
                  <Skeleton className="h-12 w-full" />
                </div>
              ) : (
                <AppsSection
                  apps={apps}
                  credentialsData={credentialsData}
                  usersById={usersById}
                  statusData={statusData}
                />
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
        <div className="vf-overview-right">
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
                usersById={usersById}
                loading={auditPending || usersPending}
                error={auditError}
              />
            </div>
          </section>
        </div>
      </div>

      {/* Bottom row: Base URL (left) + Health/Services (right) */}
      <div className="vf-overview-bottom">
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

        {/* API Health + Services */}
        <section>
          <SectionActionHeader title="API Health" />
          <div className="mt-3 flex flex-col gap-3">
            <div className="vf-health-summary">
              <StatusDotLabel
                tone={health.tone}
                label={health.label}
                pulse={health.tone === "ok"}
              />
              <p className="text-vf-micro text-vf-muted">{health.detail}</p>
            </div>

            {services.length > 0 && (
              <ProductPanel tight>
                <PanelHeader
                  title="Services"
                  description={`${enabledServices} of ${services.length} enabled`}
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
            )}
          </div>
        </section>
      </div>
    </div>
  );
}
