"use client";

/** Admin → Overview (docs/design/WEB_UI_STEERING.md — "Overview" surface spec).
 *
 * Single job: "Does anything need me?" — attention list, quiet stat strip,
 * 3 most recent audit rows. NOTHING else; resist dashboard-itis. */

import Link from "next/link";
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  Badge,
  Button,
  EmptyState,
  EntityRef,
  RelativeTime,
  SectionHeader,
  Skeleton,
} from "@/components/ui";
import {
  api,
  queryKeys,
  type AttentionItem,
  type AuditEntry,
  type User,
} from "@/lib/api";

// ---------------------------------------------------------------------------
// Section → route mapping (attention items carry a "section" slug)

const SECTION_ROUTES: Record<string, string> = {
  "apps-keys": "/admin/apps",
  services: "/admin/services",
  profiles: "/admin/profiles",
  users: "/admin/users",
  audit: "/admin/audit",
  settings: "/admin/settings",
};

function sectionHref(section: string): string {
  return SECTION_ROUTES[section] ?? "/admin";
}

// Sentence-case human phrases for audit action types
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
  "setup.local_owner_login": "Signed in (local)",
};

/** Human-readable fallback: capitalise the last dot-segment and replace underscores. */
function actionLabel(action: string): string {
  if (action in ACTION_LABELS) return ACTION_LABELS[action]!;
  const parts = action.split(".");
  const last = parts[parts.length - 1] ?? action;
  return last.charAt(0).toUpperCase() + last.slice(1).replace(/_/g, " ");
}

// ---------------------------------------------------------------------------
// Attention row

function AttentionRow({ item }: { item: AttentionItem }) {
  const tone = item.severity === "problem" ? "bad" : "warn";

  return (
    <div className="flex flex-wrap items-start justify-between gap-3 py-3">
      <div className="flex min-w-0 items-start gap-2.5">
        <Badge tone={tone} sm className="mt-0.5 shrink-0">
          {item.severity === "problem" ? "problem" : "attention"}
        </Badge>
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="text-vf-body text-vf-text">{item.title}</span>
          <span className="text-vf-small text-vf-muted">{item.detail}</span>
        </div>
      </div>
      <Button variant="secondary" size="sm" className="shrink-0" asChild>
        <Link href={sectionHref(item.section)}>
          Go to {item.section.replace("-", " & ")}
        </Link>
      </Button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Stat strip

interface StatStripProps {
  servicesUp: number;
  servicesTotal: number;
  apps: number;
  activeKeys: number;
}

function StatStrip({
  servicesUp,
  servicesTotal,
  apps,
  activeKeys,
}: StatStripProps) {
  return (
    <div className="flex flex-wrap gap-6">
      <StatItem
        label="services"
        value={`${servicesUp} / ${servicesTotal} enabled`}
      />
      <StatItem label="apps" value={String(apps)} />
      <StatItem label="active keys" value={String(activeKeys)} />
    </div>
  );
}

function StatItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-vf-micro text-vf-subtle font-mono tracking-wider uppercase">
        {label}
      </span>
      <span className="text-vf-body text-vf-text font-medium">{value}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Recent audit row

function AuditRow({
  entry,
  usersById,
}: {
  entry: AuditEntry;
  usersById: Map<string, User>;
}) {
  const label = actionLabel(entry.action);

  // Resolve actor: "cli" → "System"; user id → display_name ?? email ?? id
  let actorName: string;
  if (entry.actor_id === "cli") {
    actorName = "System";
  } else {
    const user = usersById.get(entry.actor_id);
    actorName = user?.display_name ?? user?.email ?? entry.actor_id;
  }

  // Resolve target display name only from metadata (e.g. {name: "home-desktop"}).
  // Do NOT fall back to target_kind — that produces dangling raw strings like "user".
  // The row reads: RelativeTime · actor · human action phrase (+ named target when known).
  const targetName =
    entry.metadata && typeof entry.metadata["name"] === "string"
      ? entry.metadata["name"]
      : null;

  return (
    <div className="flex flex-wrap items-center gap-3 py-2.5">
      <RelativeTime
        ts={entry.created_at}
        className="text-vf-small text-vf-muted w-20 shrink-0"
      />
      <EntityRef
        name={actorName}
        id={entry.actor_id}
        idVisibility="hover"
        className="min-w-0 shrink"
      />
      <span className="text-vf-small text-vf-text flex-1">{label}</span>
      {entry.target_id && targetName && (
        <EntityRef
          name={targetName}
          id={entry.target_id}
          idVisibility="hover"
          className="text-vf-small text-vf-muted hidden min-w-0 md:flex"
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Skeleton rows

function AttentionSkeleton() {
  return (
    <div className="flex flex-col divide-y divide-vf-border/60">
      {[1, 2].map((i) => (
        <div key={i} className="flex items-start gap-3 py-3">
          <Skeleton className="mt-0.5 h-5 w-16 shrink-0" />
          <div className="flex flex-1 flex-col gap-1.5">
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="h-3.5 w-1/2" />
          </div>
        </div>
      ))}
    </div>
  );
}

function AuditSkeleton() {
  return (
    <div className="flex flex-col divide-y divide-vf-border/60">
      {[1, 2, 3].map((i) => (
        <div key={i} className="flex items-center gap-3 py-2.5">
          <Skeleton className="h-4 w-16 shrink-0" />
          <Skeleton className="h-4 w-24" />
          <Skeleton className="h-4 flex-1" />
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page

export default function AdminOverviewPage() {
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
    queryFn: () => api.audit.list(3),
  });

  // Fetch users to resolve actor names in the audit teaser rows.
  const { data: usersData } = useQuery({
    queryKey: ["users"] as const,
    queryFn: api.users.list,
  });

  const usersById = useMemo(
    () => new Map((usersData ?? []).map((u) => [u.id, u])),
    [usersData],
  );

  const counts = overview?.counts;
  const attention = overview?.attention ?? [];

  return (
    <div className="flex flex-col gap-10">
      <SectionHeader kicker="admin" title="Overview" />

      {/* Attention list */}
      <section className="flex flex-col gap-3">
        <h3 className="text-vf-heading text-vf-text">Attention</h3>

        {overviewError ? (
          <p className="text-vf-small text-vf-bad">
            Couldn&apos;t load attention items.
          </p>
        ) : overviewPending ? (
          <AttentionSkeleton />
        ) : attention.length === 0 ? (
          <EmptyState className="py-6">All quiet.</EmptyState>
        ) : (
          <div className="divide-vf-border/60 flex flex-col divide-y">
            {attention.map((item, i) => (
              <AttentionRow key={i} item={item} />
            ))}
          </div>
        )}
      </section>

      {/* Stat strip */}
      <section className="flex flex-col gap-3">
        {overviewPending ? (
          <div className="flex gap-6">
            <Skeleton className="h-8 w-24" />
            <Skeleton className="h-8 w-16" />
            <Skeleton className="h-8 w-20" />
          </div>
        ) : counts ? (
          <StatStrip
            servicesUp={counts.services_enabled ?? 0}
            servicesTotal={counts.services_total ?? 0}
            apps={counts.apps}
            activeKeys={counts.active_credentials}
          />
        ) : null}
      </section>

      {/* Recent audit */}
      <section className="flex flex-col gap-3">
        <div className="flex items-center justify-between gap-3">
          <h3 className="text-vf-heading text-vf-text">Recent activity</h3>
          <Link
            href="/admin/audit"
            className="text-vf-small text-vf-muted hover:text-vf-text transition-colors"
          >
            View all
          </Link>
        </div>

        {auditError ? (
          <p className="text-vf-small text-vf-bad">
            Couldn&apos;t load recent activity.
          </p>
        ) : auditPending ? (
          <AuditSkeleton />
        ) : !auditEntries || auditEntries.length === 0 ? (
          <EmptyState className="py-6">No activity yet.</EmptyState>
        ) : (
          <div className="divide-vf-border/60 flex flex-col divide-y">
            {auditEntries.slice(0, 3).map((entry) => (
              <AuditRow key={entry.id} entry={entry} usersById={usersById} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
