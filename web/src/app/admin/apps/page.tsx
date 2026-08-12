"use client";

/** Admin → Apps & Keys — surface spec #1 (docs/design/WEB_UI_STEERING.md).
 *
 * List-first IA: the apps table opens the section; creation lives behind the
 * one accent Button. Owner names and key counts are client-side joins over
 * the users/credentials caches (web-api-contract.md). The presentation law
 * applies throughout: RelativeTime for every timestamp, EntityRef for every
 * id, SecretMask for key prefixes, ConfirmDialog (typed-name) for the
 * destructive actions, one OverflowMenu per row. The one-time minted key is
 * shown exactly once in the reveal dialog and never logged. */

import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  api,
  ApiError,
  queryKeys,
  type App,
  type AppKind,
  type Credential,
  type MintedCredential,
  type Profile,
  type StatusPayload,
  type User,
} from "@/lib/api";
import {
  AppAccessRow,
  AppAccessRowHeader,
  Avatar,
  avatarToneFor,
  Badge,
  Button,
  ConfirmDialog,
  CopyButton,
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogForm,
  DialogHeader,
  DialogTitle,
  EmptyOperationalState,
  Field,
  FilterToolbar,
  Input,
  KeyRevealBlock,
  IncidentBanner,
  OverflowMenu,
  OverflowMenuItem,
  OverflowMenuSeparator,
  RadioCard,
  RadioCards,
  RelativeTime,
  RightContextPanel,
  type RightContextPanelSection,
  SecretMask,
  SectionActionHeader,
  SummaryStrip,
  Select,
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  Skeleton,
  StatusDotLabel,
  toast,
} from "@/components/ui";
import { safeAuditReturnPath } from "@/lib/admin-links";

// ---------------------------------------------------------------------------
// Telemetry helpers

interface AppTelemetry {
  requests_24h: number | null;
  errors_4xx_24h: number | null;
  errors_5xx_24h: number | null;
  latency_ms_p50: number | null;
  last_seen: number | null;
}

type AppStatusFilter = "active" | "disabled" | "all";
type JoinState = "pending" | "available" | "unavailable";

function buildAppTelemetryMap(
  status: StatusPayload | undefined,
): Map<string, AppTelemetry> {
  const map = new Map<string, AppTelemetry>();
  for (const entry of status?.auth_keys ?? []) {
    if (!entry.app_id) continue;
    map.set(entry.app_id, {
      requests_24h: entry.requests_24h ?? null,
      errors_4xx_24h: entry.errors_4xx_24h ?? null,
      errors_5xx_24h: entry.errors_5xx_24h ?? null,
      latency_ms_p50: entry.latency_ms_p50 ?? null,
      last_seen: entry.last_seen ?? null,
    });
  }
  return map;
}

/** True when an app has any real metric-column telemetry — not just
 * placeholder nulls. Gates the Requests/Errors/P50 row columns and the
 * Summary telemetry cards (a wall of em-dashes is decorative noise, not honest
 * data). Deliberately excludes last_seen: it only feeds the separate "Last
 * used" field, never the metric columns. */
function hasMetricTelemetry(t: AppTelemetry | undefined): boolean {
  if (!t) return false;
  return (
    t.requests_24h !== null ||
    t.errors_4xx_24h !== null ||
    t.errors_5xx_24h !== null ||
    t.latency_ms_p50 !== null
  );
}

function fmtRequests(n: number | null): string {
  if (n === null) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function fmtLatency(ms: number | null): string {
  if (ms === null) return "—";
  if (ms >= 1_000) return `${(ms / 1_000).toFixed(1)}s`;
  return `${Math.round(ms)}ms`;
}

function fmtErrorRate(t: AppTelemetry | undefined): string {
  if (!t) return "—";
  const total = t.requests_24h;
  const errs = (t.errors_4xx_24h ?? 0) + (t.errors_5xx_24h ?? 0);
  if (total === null || total === 0) return errs > 0 ? "—" : "—";
  return `${((errs / total) * 100).toFixed(1)}%`;
}

// ---------------------------------------------------------------------------
// Shared helpers

const FALLBACK_PROFILE_LABEL = "Gateway default";

function nowSeconds(): number {
  return Math.floor(Date.now() / 1000);
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return "Something went wrong. Please try again.";
}

function userLabel(user: User | undefined): string {
  return user?.display_name ?? user?.email ?? "Owner unavailable";
}

function appOwnerLabel(app: App, usersById: Map<string, User>): string {
  if (app.owner_service_account_id) return "Service principal";
  if (app.owner_user_id) return userLabel(usersById.get(app.owner_user_id));
  return "No owner";
}

function normalizedRouteTarget(value: string | undefined): string {
  return (value ?? "")
    .trim()
    .toLowerCase()
    .replace(/[\s_]+/g, "-");
}

function appMatchesRouteTarget(app: App, target: string): boolean {
  const wanted = normalizedRouteTarget(target);
  return [app.id, app.name].some(
    (candidate) => normalizedRouteTarget(candidate) === wanted,
  );
}

function RouteAppTargetSync({
  onChange,
}: {
  onChange: (state: {
    target: string | null;
    returnPath: string | null;
  }) => void;
}) {
  const searchParams = useSearchParams();
  useEffect(() => {
    onChange({
      target: searchParams.get("app"),
      returnPath: safeAuditReturnPath(searchParams.get("return")),
    });
  }, [onChange, searchParams]);
  return null;
}

function isActiveKey(credential: Credential): boolean {
  return credential.revoked_at === null;
}

/** True at/above the product's 980px desktop breakpoint, the same breakpoint
 * SheetContent and RightContextPanel switch on. Checked at click/keydown time
 * (not cached in state) so an early interaction can't read a stale default
 * before a mount effect resolves the real viewport. Row selection needs this
 * to decide whether opening the mobile detail Sheet is meaningful — below
 * 980px, SheetContent renders the visible bottom sheet; at/above 980px it's
 * `min-[980px]:hidden`, so "opening" it would only show Radix's overlay with
 * no visible content. */
function isDesktopViewport(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia("(min-width: 980px)").matches
  );
}

/** Operator-facing profile name. */
function profileLabel(app: App): string {
  return app.effective_profile_source === "fallback"
    ? FALLBACK_PROFILE_LABEL
    : app.effective_profile_name;
}

function FormErrorSummary({ message }: { message: string }) {
  const ref = useRef<HTMLParagraphElement>(null);
  useEffect(() => ref.current?.focus(), [message]);
  return (
    <p
      ref={ref}
      role="alert"
      tabIndex={-1}
      className="text-vf-small text-vf-bad"
    >
      {message}
    </p>
  );
}

/** Active keys first, newest first within each group. */
function sortKeys(keys: Credential[]): Credential[] {
  return [...keys].sort((a, b) => {
    if (isActiveKey(a) !== isActiveKey(b)) return isActiveKey(a) ? -1 : 1;
    return b.created_at - a.created_at;
  });
}

interface RevealedKey {
  credential: MintedCredential;
  /** e.g. "for brother-laptop" / "for CI deploys" — shown in the dialog. */
  context: string;
  kind: "app" | "admin";
}

// ---------------------------------------------------------------------------
// The surface (QueryClientProvider + Toaster are in the admin layout)

export default function AdminAppsPage() {
  const queryClient = useQueryClient();
  const [routeAppTarget, setRouteAppTarget] = useState<string | null>(null);
  const [auditReturnPath, setAuditReturnPath] = useState<string | null>(null);

  const {
    data: appsData,
    isPending: appsPending,
    isError: appsIsError,
    error: appsError,
    refetch: refetchApps,
  } = useQuery({ queryKey: queryKeys.apps, queryFn: api.apps.list });
  const {
    data: usersData,
    isError: usersIsError,
    refetch: refetchUsers,
  } = useQuery({ queryKey: queryKeys.users, queryFn: api.users.list });
  const {
    data: credentialsData,
    isPending: credentialsPending,
    isError: credentialsIsError,
    error: credentialsError,
    refetch: refetchCredentials,
  } = useQuery({
    queryKey: queryKeys.credentials,
    queryFn: api.credentials.list,
  });
  const {
    data: profilesData,
    isError: profilesIsError,
    refetch: refetchProfiles,
  } = useQuery({
    queryKey: queryKeys.profiles,
    queryFn: api.profiles.list,
  });
  const { data: sessionData } = useQuery({
    queryKey: queryKeys.session,
    queryFn: api.session,
  });
  const { data: statusData } = useQuery({
    queryKey: queryKeys.status,
    queryFn: api.status,
    staleTime: 30_000,
  });

  const apps = useMemo(
    () => [...(appsData ?? [])].sort((a, b) => b.created_at - a.created_at),
    [appsData],
  );
  const usersById = useMemo(
    () => new Map((usersData ?? []).map((user) => [user.id, user])),
    [usersData],
  );
  const appKeysByApp = useMemo(() => {
    const grouped = new Map<string, Credential[]>();
    for (const credential of credentialsData ?? []) {
      if (credential.subject_kind !== "app" || !credential.app_id) continue;
      const list = grouped.get(credential.app_id) ?? [];
      list.push(credential);
      grouped.set(credential.app_id, list);
    }
    return grouped;
  }, [credentialsData]);
  const credentialsJoinState: JoinState = credentialsData
    ? "available"
    : credentialsIsError
      ? "unavailable"
      : "pending";
  const profilesJoinState: JoinState = profilesData
    ? "available"
    : profilesIsError
      ? "unavailable"
      : "pending";
  const profilesStale = profilesIsError && Boolean(profilesData);
  const adminKeys = useMemo(
    () =>
      sortKeys(
        (credentialsData ?? []).filter(
          (credential) => credential.subject_kind !== "app",
        ),
      ),
    [credentialsData],
  );
  const activeAdminKeys = useMemo(
    () => adminKeys.filter(isActiveKey),
    [adminKeys],
  );
  const revokedAdminKeys = useMemo(
    () => adminKeys.filter((credential) => !isActiveKey(credential)),
    [adminKeys],
  );

  // Telemetry by app id from /admin/api/status auth_keys snapshot. Rows without
  // a resolved app_id are intentionally not attached to an app row.
  const appTelemetryByAppId = useMemo(
    () => buildAppTelemetryMap(statusData),
    [statusData],
  );

  // Dialog / panel state. Targets hold the full row so dialogs can show
  // names without re-deriving.
  const [createOpen, setCreateOpen] = useState(false);
  const [adminKeyOpen, setAdminKeyOpen] = useState(false);
  const [mintTarget, setMintTarget] = useState<App | null>(null);
  const [profileTarget, setProfileTarget] = useState<App | null>(null);
  const [disableTarget, setDisableTarget] = useState<App | null>(null);
  const [revokeTarget, setRevokeTarget] = useState<Credential | null>(null);
  const [keysAppId, setKeysAppId] = useState<string | null>(null);
  const [revealed, setRevealed] = useState<RevealedKey | null>(null);
  const [selectedAppId, setSelectedAppId] = useState<string | null>(null);
  const [mobileDetailOpen, setMobileDetailOpen] = useState(false); // used by mobile Sheet trigger on row tap
  const [appSearch, setAppSearch] = useState("");
  const [appStatusFilter, setAppStatusFilter] =
    useState<AppStatusFilter>("active");
  const [showRevokedAdminKeys, setShowRevokedAdminKeys] = useState(false);
  const consumedAppTargetRef = useRef<string | null>(null);

  const keysApp = keysAppId
    ? (apps.find((app) => app.id === keysAppId) ?? null)
    : null;

  // Optimistic disable: flip disabled_at in the cache immediately, roll back
  // on error. The real re-enable endpoint powers the Undo action below.
  const disableApp = useMutation({
    mutationFn: (app: App) => api.apps.disable(app.id),
    onMutate: async (app) => {
      await queryClient.cancelQueries({ queryKey: queryKeys.apps });
      const previous = queryClient.getQueryData<App[]>(queryKeys.apps);
      queryClient.setQueryData<App[]>(queryKeys.apps, (rows) =>
        rows?.map((row) =>
          row.id === app.id ? { ...row, disabled_at: nowSeconds() } : row,
        ),
      );
      return { previous };
    },
    onError: (error, app, context) => {
      if (context?.previous) {
        queryClient.setQueryData(queryKeys.apps, context.previous);
      }
      toast.error(`Couldn't disable ${app.name}`, {
        description: errorMessage(error),
      });
    },
    onSuccess: (serverApp) => {
      queryClient.setQueryData<App[]>(queryKeys.apps, (rows) =>
        rows?.map((row) => (row.id === serverApp.id ? serverApp : row)),
      );
      toast.success(`${serverApp.name} disabled`, {
        description: "Its keys stop working immediately.",
        action: {
          label: "Undo",
          onClick: () => enableApp.mutate(serverApp),
        },
      });
    },
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.apps }),
  });

  // Re-enable: the undo for optimistic disable.
  const enableApp = useMutation({
    mutationFn: (app: App) => api.apps.enable(app.id),
    onMutate: async (app) => {
      await queryClient.cancelQueries({ queryKey: queryKeys.apps });
      const previous = queryClient.getQueryData<App[]>(queryKeys.apps);
      queryClient.setQueryData<App[]>(queryKeys.apps, (rows) =>
        rows?.map((row) =>
          row.id === app.id ? { ...row, disabled_at: null } : row,
        ),
      );
      return { previous };
    },
    onError: (error, app, context) => {
      if (context?.previous) {
        queryClient.setQueryData(queryKeys.apps, context.previous);
      }
      toast.error(`Couldn't re-enable ${app.name}`, {
        description: errorMessage(error),
      });
    },
    onSuccess: (serverApp) => {
      queryClient.setQueryData<App[]>(queryKeys.apps, (rows) =>
        rows?.map((row) => (row.id === serverApp.id ? serverApp : row)),
      );
      toast.success(`${serverApp.name} re-enabled`);
    },
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.apps }),
  });

  // Optimistic revoke, same shape, on the credentials cache.
  const revokeKey = useMutation({
    mutationFn: (credential: Credential) =>
      api.credentials.revoke(credential.id),
    onMutate: async (credential) => {
      await queryClient.cancelQueries({ queryKey: queryKeys.credentials });
      const previous = queryClient.getQueryData<Credential[]>(
        queryKeys.credentials,
      );
      queryClient.setQueryData<Credential[]>(queryKeys.credentials, (rows) =>
        rows?.map((row) =>
          row.id === credential.id ? { ...row, revoked_at: nowSeconds() } : row,
        ),
      );
      return { previous };
    },
    onError: (error, credential, context) => {
      if (context?.previous) {
        queryClient.setQueryData(queryKeys.credentials, context.previous);
      }
      toast.error(`Couldn't revoke ${credential.display_name}`, {
        description: errorMessage(error),
      });
    },
    onSuccess: (serverCredential) => {
      queryClient.setQueryData<Credential[]>(queryKeys.credentials, (rows) =>
        rows?.map((row) =>
          row.id === serverCredential.id ? serverCredential : row,
        ),
      );
      toast.success(`${serverCredential.display_name} revoked`, {
        description: "Revoked keys can't be restored.",
      });
    },
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.credentials }),
  });

  const selectedApp = selectedAppId
    ? (apps.find((a) => a.id === selectedAppId) ?? null)
    : null;

  const filteredApps = useMemo(() => {
    const statusFiltered = apps.filter((app) => {
      if (appStatusFilter === "active") return app.disabled_at === null;
      if (appStatusFilter === "disabled") return app.disabled_at !== null;
      return true;
    });
    if (!appSearch.trim()) return statusFiltered;
    const q = appSearch.toLowerCase();
    return statusFiltered.filter(
      (a) =>
        a.name.toLowerCase().includes(q) ||
        userLabel(usersById.get(a.owner_user_id ?? ""))
          .toLowerCase()
          .includes(q),
    );
  }, [apps, appSearch, appStatusFilter, usersById]);

  const routeSelectedApp = useMemo(() => {
    if (!routeAppTarget) return null;
    return (
      apps.find((app) => appMatchesRouteTarget(app, routeAppTarget)) ?? null
    );
  }, [apps, routeAppTarget]);

  // Only render the row metric columns when at least one visible app has real
  // telemetry; otherwise the columns are a wall of em-dashes (data-honesty).
  const anyVisibleTelemetry = useMemo(
    () =>
      filteredApps.some((app) =>
        hasMetricTelemetry(appTelemetryByAppId.get(app.id)),
      ),
    [filteredApps, appTelemetryByAppId],
  );

  useEffect(() => {
    if (!routeAppTarget) {
      consumedAppTargetRef.current = null;
      return;
    }
    if (consumedAppTargetRef.current === routeAppTarget || !routeSelectedApp) {
      return;
    }
    if (routeSelectedApp.disabled_at !== null && appStatusFilter === "active") {
      setAppStatusFilter("all");
    }
    if (selectedAppId !== routeSelectedApp.id) {
      setSelectedAppId(routeSelectedApp.id);
    }
    consumedAppTargetRef.current = routeAppTarget;
  }, [appStatusFilter, routeAppTarget, routeSelectedApp, selectedAppId]);

  useEffect(() => {
    if (routeAppTarget) {
      if (!routeSelectedApp && selectedAppId !== null) {
        setSelectedAppId(null);
      }
      return;
    }
    if (filteredApps.length === 0) {
      if (selectedAppId !== null) setSelectedAppId(null);
      return;
    }
    if (
      !selectedAppId ||
      !filteredApps.some((app) => app.id === selectedAppId)
    ) {
      setSelectedAppId(filteredApps[0]!.id);
    }
  }, [filteredApps, routeAppTarget, routeSelectedApp, selectedAppId]);

  // Build a map from profile id → Profile for the panel scope lookup.
  const profilesById = useMemo(
    () => new Map((profilesData ?? []).map((p) => [p.id, p])),
    [profilesData],
  );
  const missingRouteApp =
    routeAppTarget !== null &&
    routeSelectedApp === null &&
    !appsPending &&
    apps.length > 0;

  const appPanelSections = useMemo((): RightContextPanelSection[] => {
    if (!selectedApp) return [];
    const appKeys =
      credentialsJoinState === "available"
        ? sortKeys(appKeysByApp.get(selectedApp.id) ?? [])
        : [];
    const activeKeys = appKeys.filter(isActiveKey);
    const activeKeyCount = activeKeys.length;
    const lastUsed = appKeys.reduce<number | null>((best, k) => {
      if (k.last_used_at === null) return best;
      return best === null || k.last_used_at > best ? k.last_used_at : best;
    }, null);
    const telemetry = appTelemetryByAppId.get(selectedApp.id);
    const sections: RightContextPanelSection[] = [];

    // Derive scope tags from the app's effective profile.
    const effectiveProfile = selectedApp.effective_profile_id
      ? profilesById.get(selectedApp.effective_profile_id)
      : undefined;
    const scopeTags: string[] = [];
    if (effectiveProfile) {
      // Top-level protocols (e.g. "openai", "chat")
      for (const p of effectiveProfile.protocols ?? []) scopeTags.push(p);
      // allow.intents nested inside profile.profile
      const allow = (
        effectiveProfile.profile as { allow?: { intents?: string[] } }
      )?.allow;
      for (const intent of allow?.intents ?? []) scopeTags.push(intent);
    }

    // About: type / profile / owner / status in a compact KV grid
    sections.push({
      title: "About",
      content: (
        <div className="flex flex-col gap-1.5">
          <div className="flex items-center justify-between gap-2">
            <span className="text-vf-micro text-vf-subtle font-mono uppercase">
              Type
            </span>
            <Badge sm>
              {selectedApp.kind === "personal" ? "Personal" : "Shared"}
            </Badge>
          </div>
          <div className="flex items-center justify-between gap-2">
            <span className="text-vf-micro text-vf-subtle font-mono uppercase">
              Profile
            </span>
            <span className="text-vf-small text-vf-text max-w-[140px] truncate">
              {profileLabel(selectedApp)}
            </span>
          </div>
          {(selectedApp.owner_user_id ||
            selectedApp.owner_service_account_id) && (
            <div className="flex items-center justify-between gap-2">
              <span className="text-vf-micro text-vf-subtle font-mono uppercase">
                Owner
              </span>
              <span className="text-vf-small text-vf-text max-w-[140px] truncate">
                {appOwnerLabel(selectedApp, usersById)}
              </span>
            </div>
          )}
          <div className="flex items-center justify-between gap-2">
            <span className="text-vf-micro text-vf-subtle font-mono uppercase">
              Status
            </span>
            <StatusDotLabel
              tone={selectedApp.disabled_at !== null ? "warn" : "ok"}
              label={selectedApp.disabled_at !== null ? "disabled" : "active"}
            />
          </div>
          <div className="flex items-center justify-between gap-2">
            <span className="text-vf-micro text-vf-subtle font-mono uppercase">
              Last used
            </span>
            <span className="text-vf-small text-vf-text">
              {telemetry?.last_seen || credentialsJoinState === "available" ? (
                <RelativeTime
                  ts={
                    telemetry?.last_seen
                      ? Math.floor(telemetry.last_seen)
                      : lastUsed
                  }
                  fallback="Never"
                />
              ) : credentialsJoinState === "pending" ? (
                "Loading…"
              ) : (
                "Unavailable"
              )}
            </span>
          </div>
        </div>
      ),
    });

    // Scope: capability tags from the effective profile (only if data available).
    if (scopeTags.length > 0) {
      sections.push({
        title: "Scope",
        content: (
          <div className="flex flex-wrap gap-1.5">
            {scopeTags.map((tag) => (
              <Badge key={tag} sm>
                {tag}
              </Badge>
            ))}
          </div>
        ),
      });
    } else if (
      effectiveProfile === undefined &&
      selectedApp.effective_profile_id &&
      // The fallback profile isn't in GET /profiles; don't show placeholder for it.
      selectedApp.effective_profile_source !== "fallback"
    ) {
      sections.push({
        title: "Scope",
        content: (
          <div className="flex items-center gap-2">
            <span className="text-vf-micro text-vf-muted">
              {profilesJoinState === "unavailable"
                ? "Profile capabilities unavailable."
                : "Loading profile capabilities…"}
            </span>
            {profilesJoinState === "unavailable" && (
              <Button
                type="button"
                size="sm"
                onClick={() => void refetchProfiles()}
              >
                Retry
              </Button>
            )}
          </div>
        ),
      });
    }

    // Summary stat grid — Active keys always; telemetry cards only when the
    // selected app has real telemetry (no decorative em-dash cards otherwise).
    const selectedHasTelemetry = hasMetricTelemetry(telemetry);
    sections.push({
      title: "Summary",
      content: (
        <div className="grid grid-cols-2 gap-2">
          <div className="border-vf-border bg-vf-surface-2/60 flex flex-col gap-0.5 rounded border p-2.5">
            <span className="text-vf-micro text-vf-subtle font-mono uppercase">
              Active keys
            </span>
            <span className="text-vf-title text-vf-text font-mono font-bold tabular-nums">
              {credentialsJoinState === "available"
                ? activeKeyCount
                : credentialsJoinState === "pending"
                  ? "…"
                  : "Unavailable"}
            </span>
          </div>
          {selectedHasTelemetry && (
            <>
              <div className="border-vf-border bg-vf-surface-2/60 flex flex-col gap-0.5 rounded border p-2.5">
                <span className="text-vf-micro text-vf-subtle font-mono uppercase">
                  Requests 24h
                </span>
                <span className="text-vf-title text-vf-text font-mono font-bold tabular-nums">
                  {fmtRequests(telemetry?.requests_24h ?? null)}
                </span>
              </div>
              <div className="border-vf-border bg-vf-surface-2/60 flex flex-col gap-0.5 rounded border p-2.5">
                <span className="text-vf-micro text-vf-subtle font-mono uppercase">
                  Latency p50
                </span>
                <span className="text-vf-title text-vf-text font-mono font-bold tabular-nums">
                  {fmtLatency(telemetry?.latency_ms_p50 ?? null)}
                </span>
              </div>
              <div className="border-vf-border bg-vf-surface-2/60 flex flex-col gap-0.5 rounded border p-2.5">
                <span className="text-vf-micro text-vf-subtle font-mono uppercase">
                  Error rate
                </span>
                <span
                  className={`text-vf-title font-mono font-bold tabular-nums ${
                    fmtErrorRate(telemetry) !== "—" &&
                    fmtErrorRate(telemetry) !== "0.0%"
                      ? "text-vf-warn"
                      : "text-vf-text"
                  }`}
                >
                  {fmtErrorRate(telemetry)}
                </span>
              </div>
            </>
          )}
        </div>
      ),
    });

    // Linked API keys with inline revoke
    sections.push({
      title: `Linked API keys${
        credentialsJoinState === "available" && activeKeyCount > 0
          ? ` · ${activeKeyCount} active`
          : ""
      }`,
      content:
        credentialsJoinState === "pending" ? (
          <span className="text-vf-micro text-vf-muted">Loading keys…</span>
        ) : credentialsJoinState === "unavailable" ? (
          <span className="text-vf-micro text-vf-muted">Keys unavailable.</span>
        ) : appKeys.length === 0 ? (
          <span className="text-vf-micro text-vf-muted">No keys yet.</span>
        ) : (
          <ul className="flex flex-col">
            {appKeys.slice(0, 6).map((k) => (
              <li
                key={k.id}
                className="border-vf-border/60 flex items-center justify-between gap-2 border-t py-2 first:border-t-0"
              >
                <div className="flex min-w-0 flex-col gap-0.5">
                  <span className="text-vf-small text-vf-text truncate">
                    {k.display_name}
                  </span>
                  <SecretMask prefix={k.prefix} />
                </div>
                <div className="flex shrink-0 items-center gap-1.5">
                  <Badge sm tone={isActiveKey(k) ? "ok" : undefined}>
                    {isActiveKey(k) ? "active" : "revoked"}
                  </Badge>
                  {isActiveKey(k) && (
                    <button
                      type="button"
                      className="vf-button vf-button-ghost vf-button-sm text-vf-bad hover:text-vf-bad px-2"
                      onClick={() => setRevokeTarget(k)}
                      aria-label={`Revoke ${k.display_name}`}
                    >
                      Revoke
                    </button>
                  )}
                </div>
              </li>
            ))}
            {appKeys.length > 6 && (
              <li className="pt-2">
                <button
                  type="button"
                  className="text-vf-micro text-vf-accent hover:underline"
                  onClick={() => {
                    setKeysAppId(selectedApp.id);
                    setSelectedAppId(null);
                  }}
                >
                  View all {appKeys.length} keys →
                </button>
              </li>
            )}
          </ul>
        ),
    });

    // Actions
    sections.push({
      title: "Actions",
      content: (
        <div className="flex flex-wrap gap-2">
          {selectedApp.disabled_at === null && (
            <Button
              size="sm"
              variant="secondary"
              onClick={() => setMintTarget(selectedApp)}
            >
              Mint key…
            </Button>
          )}
          <Button
            size="sm"
            variant="secondary"
            onClick={() => {
              setKeysAppId(selectedApp.id);
              setSelectedAppId(null);
            }}
          >
            View all keys
          </Button>
          <Button
            size="sm"
            variant="secondary"
            onClick={() => setProfileTarget(selectedApp)}
          >
            Change profile…
          </Button>
          {selectedApp.disabled_at === null && (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setDisableTarget(selectedApp)}
            >
              Disable…
            </Button>
          )}
        </div>
      ),
    });

    return sections;
  }, [
    selectedApp,
    appKeysByApp,
    usersById,
    appTelemetryByAppId,
    profilesById,
    credentialsJoinState,
    profilesJoinState,
    refetchProfiles,
  ]);

  const loading = appsPending;
  const appsFailed = appsIsError && !appsData;
  const joinsFailed = !appsFailed && (usersIsError || credentialsIsError);

  function retryAll() {
    void refetchApps();
    void refetchUsers();
    void refetchCredentials();
    void refetchProfiles();
  }

  return (
    <div className="flex w-full flex-col gap-8">
      <Suspense fallback={null}>
        <RouteAppTargetSync
          onChange={({ target, returnPath }) => {
            setRouteAppTarget(target);
            setAuditReturnPath(returnPath);
          }}
        />
      </Suspense>
      <section className="flex flex-col gap-4">
        <SectionActionHeader
          kicker="access"
          title="Apps & keys"
          description="Apps connect callers to the gateway; every key belongs to an app."
          count={appsData ? apps.length : undefined}
          action={
            <>
              {auditReturnPath && (
                <Button asChild variant="secondary">
                  <Link href={auditReturnPath}>Back to audit</Link>
                </Button>
              )}
              <Button variant="primary" onClick={() => setCreateOpen(true)}>
                Create app
              </Button>
            </>
          }
        />

        <SummaryStrip
          items={[
            {
              label: "Active",
              value: appsData
                ? apps.filter((a) => a.disabled_at === null).length
                : appsIsError
                  ? "Unavailable"
                  : "…",
              badge: appsData ? "live" : appsIsError ? "degraded" : "loading",
            },
            {
              label: "Disabled",
              value: appsData
                ? apps.filter((a) => a.disabled_at !== null).length
                : appsIsError
                  ? "Unavailable"
                  : "…",
              badge: appsData ? "live" : appsIsError ? "degraded" : "loading",
            },
            {
              label: "Active keys",
              value:
                credentialsJoinState === "available"
                  ? (credentialsData ?? []).filter(
                      (c) => c.subject_kind === "app" && c.revoked_at === null,
                    ).length
                  : credentialsJoinState === "unavailable"
                    ? "Unavailable"
                    : "…",
            },
            {
              label: "Admin keys",
              value:
                credentialsJoinState === "available"
                  ? activeAdminKeys.length
                  : credentialsJoinState === "unavailable"
                    ? "Unavailable"
                    : "…",
            },
          ]}
        />

        {joinsFailed && (
          <ErrorBanner
            onRetry={retryAll}
            title="Some details couldn't load"
            detail="Owner names or key counts may be incomplete."
          />
        )}

        {appsFailed ? (
          <ErrorBanner
            onRetry={retryAll}
            title="Couldn't load apps"
            detail={errorMessage(appsError)}
          />
        ) : (
          <div className="flex gap-4">
            <div className="flex min-w-0 flex-1 flex-col gap-3">
              <FilterToolbar
                search={
                  <input
                    type="search"
                    className="vf-field-input w-full"
                    placeholder="Filter apps…"
                    value={appSearch}
                    onChange={(e) => setAppSearch(e.target.value)}
                    aria-label="Filter apps"
                  />
                }
                filters={
                  <>
                    {(
                      [
                        [
                          "active",
                          `Active ${apps.filter((app) => app.disabled_at === null).length}`,
                        ],
                        [
                          "disabled",
                          `Disabled ${apps.filter((app) => app.disabled_at !== null).length}`,
                        ],
                        ["all", `All ${apps.length}`],
                      ] as const
                    ).map(([value, label]) => (
                      <button
                        key={value}
                        type="button"
                        className="vf-filter-chip"
                        aria-pressed={appStatusFilter === value}
                        onClick={() => setAppStatusFilter(value)}
                      >
                        {label}
                      </button>
                    ))}
                  </>
                }
              />
              {missingRouteApp && (
                <IncidentBanner
                  tone="warn"
                  title="No app matches this link"
                  detail={`The app target "${routeAppTarget}" is not in the current app list.`}
                />
              )}
              {loading ? (
                <div className="flex flex-col gap-2" aria-busy="true">
                  <Skeleton className="h-14 w-full" />
                  <Skeleton className="h-14 w-full" />
                  <Skeleton className="h-14 w-full" />
                </div>
              ) : filteredApps.length === 0 ? (
                apps.length === 0 ? (
                  <EmptyOperationalState
                    title="No apps yet"
                    detail="Create an app to hand out its first key."
                    action={
                      <Button onClick={() => setCreateOpen(true)}>
                        Create app
                      </Button>
                    }
                  />
                ) : (
                  <EmptyOperationalState
                    title="No apps match the filter"
                    detail={
                      appStatusFilter === "active"
                        ? "No active apps match the current search."
                        : appStatusFilter === "disabled"
                          ? "No disabled apps match the current search."
                          : undefined
                    }
                    action={
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          setAppSearch("");
                          setAppStatusFilter("active");
                        }}
                      >
                        Clear filter
                      </Button>
                    }
                  />
                )
              ) : (
                <div className="vf-panel overflow-hidden p-0">
                  <AppAccessRowHeader
                    metricLabels={
                      anyVisibleTelemetry
                        ? ["Requests", "Errors", "P50"]
                        : undefined
                    }
                  />
                  {filteredApps.map((app) => {
                    const activeKeyCount =
                      credentialsJoinState === "available"
                        ? (appKeysByApp.get(app.id) ?? []).filter(isActiveKey)
                            .length
                        : undefined;
                    const isSelected = selectedAppId === app.id;
                    // Below 980px, selecting a row also opens the mobile
                    // detail Sheet (SheetContent there is the only visible
                    // detail view). At/above 980px, SheetContent is
                    // `min-[980px]:hidden` — the RightContextPanel already
                    // shows the selection, so opening the Sheet would only
                    // render Radix's overlay with nothing visible inside it.
                    const selectApp = () => {
                      setSelectedAppId(app.id);
                      if (!isDesktopViewport()) setMobileDetailOpen(true);
                    };
                    return (
                      <div
                        key={app.id}
                        className={`cursor-pointer transition-colors ${isSelected ? "bg-vf-row-hover ring-vf-accent/30 ring-1 ring-inset" : ""}`}
                      >
                        <div className="relative flex min-w-0 items-stretch">
                          <button
                            type="button"
                            onClick={selectApp}
                            className="rounded-vf-md focus-visible:outline-vf-accent absolute inset-0 z-10 focus-visible:outline-2 focus-visible:outline-offset-[-2px]"
                            aria-label={`Select ${app.name}`}
                          />
                          <div className="pointer-events-none min-w-0 flex-1">
                            <AppAccessRow
                              name={app.name}
                              avatar={
                                <Avatar
                                  initials={app.name.slice(0, 2)}
                                  tone={avatarToneFor(app.name)}
                                />
                              }
                              owner={
                                app.owner_user_id
                                  ? appOwnerLabel(app, usersById)
                                  : app.owner_service_account_id
                                    ? appOwnerLabel(app, usersById)
                                    : undefined
                              }
                              profile={profileLabel(app)}
                              keyCount={activeKeyCount}
                              metrics={
                                !anyVisibleTelemetry
                                  ? undefined
                                  : (() => {
                                      const t = appTelemetryByAppId.get(app.id);
                                      const req = fmtRequests(
                                        t?.requests_24h ?? null,
                                      );
                                      const lat = fmtLatency(
                                        t?.latency_ms_p50 ?? null,
                                      );
                                      const errRate = fmtErrorRate(t);
                                      return (
                                        <>
                                          <span className="flex flex-col items-end">
                                            <span className="text-vf-small text-vf-text font-mono tabular-nums">
                                              {req}
                                            </span>
                                            <span className="text-vf-micro text-vf-subtle font-mono uppercase">
                                              reqs 24h
                                            </span>
                                          </span>
                                          <span className="flex flex-col items-end">
                                            <span
                                              className={`text-vf-small font-mono tabular-nums ${errRate !== "—" && errRate !== "0.0%" ? "text-vf-warn" : "text-vf-text"}`}
                                            >
                                              {errRate}
                                            </span>
                                            <span className="text-vf-micro text-vf-subtle font-mono uppercase">
                                              errors
                                            </span>
                                          </span>
                                          <span className="flex flex-col items-end">
                                            <span className="text-vf-small text-vf-text font-mono tabular-nums">
                                              {lat}
                                            </span>
                                            <span className="text-vf-micro text-vf-subtle font-mono uppercase">
                                              p50
                                            </span>
                                          </span>
                                        </>
                                      );
                                    })()
                              }
                              lastUsedLabel={(() => {
                                // Prefer status snapshot last_seen (float epoch seconds);
                                // fall back to credential last_used_at (int epoch seconds).
                                const t = appTelemetryByAppId.get(app.id);
                                const lastSeenTs = t?.last_seen
                                  ? Math.floor(t.last_seen)
                                  : null;
                                if (credentialsJoinState !== "available") {
                                  return credentialsJoinState === "pending"
                                    ? "Loading…"
                                    : "Unavailable";
                                }
                                const keys = appKeysByApp.get(app.id) ?? [];
                                const credTs = keys.reduce<number | null>(
                                  (best, k) => {
                                    if (k.last_used_at === null) return best;
                                    return best === null ||
                                      k.last_used_at > best
                                      ? k.last_used_at
                                      : best;
                                  },
                                  null,
                                );
                                const ts = lastSeenTs ?? credTs;
                                if (!ts) return "Never";
                                const diffSec =
                                  Math.floor(Date.now() / 1000) - ts;
                                if (diffSec < 60) return "just now";
                                if (diffSec < 3600)
                                  return `${Math.floor(diffSec / 60)}m ago`;
                                if (diffSec < 86400)
                                  return `${Math.floor(diffSec / 3600)}h ago`;
                                return `${Math.floor(diffSec / 86400)}d ago`;
                              })()}
                              status={app.disabled_at !== null ? "warn" : "ok"}
                              statusLabel={
                                app.disabled_at !== null ? "disabled" : "active"
                              }
                              actions={
                                <span className="vf-access-row-kind-badge">
                                  <Badge
                                    sm
                                    tone={
                                      app.kind === "personal"
                                        ? "info"
                                        : undefined
                                    }
                                  >
                                    {app.kind === "personal"
                                      ? "Personal"
                                      : "Shared"}
                                  </Badge>
                                </span>
                              }
                            />
                          </div>
                          <span className="z-20 flex shrink-0 items-center px-2">
                            <OverflowMenu label={`Actions for ${app.name}`}>
                              <OverflowMenuItem
                                disabled={app.disabled_at !== null}
                                onSelect={() => setMintTarget(app)}
                              >
                                Mint key…
                              </OverflowMenuItem>
                              <OverflowMenuItem
                                onSelect={() => setKeysAppId(app.id)}
                              >
                                View keys
                              </OverflowMenuItem>
                              <OverflowMenuItem
                                onSelect={() => setProfileTarget(app)}
                              >
                                Change profile…
                              </OverflowMenuItem>
                              <OverflowMenuSeparator />
                              <OverflowMenuItem
                                danger
                                disabled={app.disabled_at !== null}
                                onSelect={() => setDisableTarget(app)}
                              >
                                Disable app…
                              </OverflowMenuItem>
                            </OverflowMenu>
                          </span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            {selectedApp && (
              <RightContextPanel
                title={selectedApp.name}
                subtitle={profileLabel(selectedApp)}
                status={
                  <Avatar
                    initials={selectedApp.name.slice(0, 2)}
                    tone={avatarToneFor(selectedApp.name)}
                    size="sm"
                  />
                }
                actions={
                  <Button
                    size="sm"
                    variant="primary"
                    disabled={selectedApp.disabled_at !== null}
                    onClick={() => setMintTarget(selectedApp)}
                  >
                    Mint key
                  </Button>
                }
                sections={appPanelSections}
                className="hidden w-72 shrink-0 min-[980px]:flex min-[980px]:flex-col"
              />
            )}
          </div>
        )}
      </section>

      {/* App detail sheet — mobile/tablet only (lg+ shows inline panel) */}
      <Sheet
        open={mobileDetailOpen && selectedApp !== null}
        onOpenChange={(open) => {
          if (!open) setMobileDetailOpen(false);
        }}
      >
        {selectedApp && (
          <SheetContent className="overflow-y-auto min-[980px]:hidden">
            <SheetHeader>
              <div className="flex items-center gap-3">
                <Avatar
                  initials={selectedApp.name.slice(0, 2)}
                  tone={avatarToneFor(selectedApp.name)}
                />
                <div className="min-w-0 flex-1">
                  <SheetTitle className="truncate">
                    {selectedApp.name}
                  </SheetTitle>
                  <SheetDescription className="truncate">
                    {profileLabel(selectedApp)}
                  </SheetDescription>
                </div>
              </div>
            </SheetHeader>
            <div className="mt-4 flex flex-col gap-0">
              {appPanelSections.map((section, i) => (
                <div
                  key={i}
                  className="border-vf-border/60 flex flex-col gap-2 border-t py-3 first:border-t-0"
                >
                  {section.title && (
                    <p className="text-vf-micro text-vf-subtle font-mono uppercase">
                      {section.title}
                    </p>
                  )}
                  {section.content}
                </div>
              ))}
            </div>
            <div className="border-vf-border/60 mt-4 border-t pt-4">
              <Button
                size="sm"
                variant="primary"
                disabled={selectedApp.disabled_at !== null}
                onClick={() => {
                  setMobileDetailOpen(false);
                  setMintTarget(selectedApp);
                }}
              >
                Mint key
              </Button>
            </div>
          </SheetContent>
        )}
      </Sheet>

      {/* Admin automation keys — compact subsection, not a full table. */}
      <section className="flex flex-col gap-4">
        <SectionActionHeader
          kicker="automation"
          title="Admin keys"
          description={
            revokedAdminKeys.length > 0
              ? `${activeAdminKeys.length} active, ${revokedAdminKeys.length} revoked hidden.`
              : "For scripts and CI that call the admin API. Not tied to an app."
          }
          action={
            <div className="flex items-center gap-2">
              {revokedAdminKeys.length > 0 && (
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => setShowRevokedAdminKeys((value) => !value)}
                >
                  {showRevokedAdminKeys ? "Hide revoked" : "Show revoked"}
                </Button>
              )}
              <Button size="sm" onClick={() => setAdminKeyOpen(true)}>
                Create admin key
              </Button>
            </div>
          }
        />

        {credentialsIsError && !credentialsData ? (
          <ErrorBanner
            onRetry={retryAll}
            title="Couldn't load admin keys"
            detail={errorMessage(credentialsError)}
          />
        ) : credentialsPending ? (
          <div className="vf-feed-panel flex flex-col gap-0" aria-busy="true">
            <Skeleton className="h-14 w-full rounded-none border-b border-[var(--vf-border)]" />
            <Skeleton className="h-14 w-full rounded-none" />
          </div>
        ) : adminKeys.length === 0 ? (
          <EmptyOperationalState
            title="No admin automation keys"
            detail="Create one to let scripts and CI call the admin API."
            action={
              <Button size="sm" onClick={() => setAdminKeyOpen(true)}>
                Create admin key
              </Button>
            }
          />
        ) : !showRevokedAdminKeys && activeAdminKeys.length === 0 ? (
          <EmptyOperationalState
            title="No active admin keys"
            detail="Revoked admin keys are hidden by default."
            action={
              <Button
                size="sm"
                variant="secondary"
                onClick={() => setShowRevokedAdminKeys(true)}
              >
                Show revoked
              </Button>
            }
          />
        ) : (
          <div className="vf-feed-panel overflow-hidden">
            <ul>
              {(showRevokedAdminKeys ? adminKeys : activeAdminKeys).map(
                (credential) => (
                  <li
                    key={credential.id}
                    className="flex items-center gap-3 border-b border-[var(--vf-border)] px-4 py-3 last:border-b-0"
                  >
                    {/* Key identity */}
                    <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                      <span className="text-vf-small text-vf-text truncate font-medium">
                        {credential.display_name}
                      </span>
                      <div className="flex flex-wrap items-center gap-2">
                        <SecretMask prefix={credential.prefix} />
                        {credential.scopes.length > 0 && (
                          <span className="text-vf-micro text-vf-muted font-mono">
                            {credential.scopes.join(", ")}
                          </span>
                        )}
                      </div>
                    </div>
                    {/* Last used */}
                    <span className="text-vf-micro text-vf-muted hidden shrink-0 tabular-nums sm:block">
                      <RelativeTime
                        ts={credential.last_used_at}
                        fallback="Never"
                      />
                    </span>
                    {/* Status badge */}
                    <KeyStatusBadge credential={credential} />
                    {/* Actions */}
                    <OverflowMenu
                      label={`Actions for ${credential.display_name}`}
                    >
                      <OverflowMenuItem
                        danger
                        disabled={!isActiveKey(credential)}
                        onSelect={() => setRevokeTarget(credential)}
                      >
                        Revoke key…
                      </OverflowMenuItem>
                    </OverflowMenu>
                  </li>
                ),
              )}
            </ul>
          </div>
        )}
      </section>

      {/* App keys detail panel */}
      <Sheet
        open={keysApp !== null}
        onOpenChange={(open) => {
          if (!open) setKeysAppId(null);
        }}
      >
        {keysApp && (
          <SheetContent>
            <SheetHeader>
              <SheetTitle>{keysApp.name}</SheetTitle>
              <SheetDescription>
                Keys this app uses to call the gateway.
              </SheetDescription>
            </SheetHeader>
            <AppKeysPanel
              app={keysApp}
              keys={
                credentialsJoinState === "available"
                  ? sortKeys(appKeysByApp.get(keysApp.id) ?? [])
                  : []
              }
              joinState={credentialsJoinState}
              onMint={() => setMintTarget(keysApp)}
              onRevoke={setRevokeTarget}
              onRetry={() => void refetchCredentials()}
            />
          </SheetContent>
        )}
      </Sheet>

      {/* Dialogs */}
      <CreateAppDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        users={usersData ?? []}
        profiles={profilesData ?? []}
        profilesJoinState={profilesJoinState}
        profilesStale={profilesStale}
        onRetryProfiles={() => void refetchProfiles()}
      />
      <ChangeProfileDialog
        app={profileTarget}
        onOpenChange={(open) => {
          if (!open) setProfileTarget(null);
        }}
        profiles={profilesData ?? []}
        profilesJoinState={profilesJoinState}
        profilesStale={profilesStale}
        onRetryProfiles={() => void refetchProfiles()}
      />
      <MintKeyDialog
        app={mintTarget}
        onOpenChange={(open) => {
          if (!open) setMintTarget(null);
        }}
        onMinted={(credential, app) => {
          setMintTarget(null);
          setRevealed({ credential, context: `for ${app.name}`, kind: "app" });
        }}
      />
      <CreateAdminKeyDialog
        open={adminKeyOpen}
        onOpenChange={setAdminKeyOpen}
        canMintOwner={sessionData?.actor.roles.includes("owner") ?? false}
        onMinted={(credential) => {
          setAdminKeyOpen(false);
          setRevealed({
            credential,
            context: `for ${credential.display_name}`,
            kind: "admin",
          });
        }}
      />
      <RevealKeyDialog revealed={revealed} onClose={() => setRevealed(null)} />

      {/* Destructive confirms (typed-name) */}
      <ConfirmDialog
        open={disableTarget !== null}
        onOpenChange={(open) => {
          if (!open) setDisableTarget(null);
        }}
        title="Disable app?"
        description={
          disableTarget
            ? `${disableTarget.name} and all of its keys stop working immediately. You can re-enable the app from the success message if needed.`
            : undefined
        }
        confirmLabel="Disable app"
        typedName={disableTarget?.name}
        onConfirm={() => {
          // Optimistic: the cache flips immediately; errors roll back + toast.
          if (disableTarget)
            return disableApp.mutateAsync(disableTarget).then(() => undefined);
          return Promise.resolve();
        }}
      />
      <ConfirmDialog
        open={revokeTarget !== null}
        onOpenChange={(open) => {
          if (!open) setRevokeTarget(null);
        }}
        title="Revoke key?"
        description={
          revokeTarget
            ? `Anything still using ${revokeTarget.display_name} is cut off immediately. Revoked keys can't be restored.`
            : undefined
        }
        confirmLabel="Revoke key"
        typedName={revokeTarget?.display_name}
        onConfirm={() => {
          if (revokeTarget)
            return revokeKey.mutateAsync(revokeTarget).then(() => undefined);
          return Promise.resolve();
        }}
      />
    </div>
  );
}

function KeyStatusBadge({ credential }: { credential: Credential }) {
  return isActiveKey(credential) ? (
    <Badge sm tone="ok">
      active
    </Badge>
  ) : (
    <Badge sm>revoked</Badge>
  );
}

// ---------------------------------------------------------------------------
// Designed error state (spec #4: error banner with retry)

function ErrorBanner({
  title,
  detail,
  onRetry,
}: {
  title?: string;
  detail?: string;
  onRetry: () => void;
}) {
  return (
    <IncidentBanner
      tone="bad"
      title={title ?? "Something went wrong"}
      detail={detail}
      actions={
        <Button size="sm" onClick={onRetry}>
          Retry
        </Button>
      }
    />
  );
}

// ---------------------------------------------------------------------------
// App keys panel (inside the detail Sheet)

function AppKeysPanel({
  app,
  keys,
  joinState,
  onMint,
  onRevoke,
  onRetry,
}: {
  app: App;
  keys: Credential[];
  joinState: JoinState;
  onMint: () => void;
  onRevoke: (credential: Credential) => void;
  onRetry: () => void;
}) {
  const [showRevoked, setShowRevoked] = useState(false);

  if (joinState === "pending") {
    return (
      <div className="flex flex-col gap-3" aria-busy="true">
        <Skeleton className="h-12 w-full" />
        <Skeleton className="h-12 w-full" />
      </div>
    );
  }

  if (joinState === "unavailable") {
    return (
      <ErrorBanner
        title="Couldn't load app keys"
        detail="Refresh to try again."
        onRetry={onRetry}
      />
    );
  }

  if (keys.length === 0) {
    return (
      <EmptyOperationalState
        title="No keys yet"
        detail={
          app.disabled_at === null
            ? "Mint a key to let this app call the gateway."
            : undefined
        }
        action={
          app.disabled_at === null ? (
            <Button size="sm" onClick={onMint}>
              Mint key
            </Button>
          ) : undefined
        }
      />
    );
  }

  const revokedKeys = keys.filter((k) => !isActiveKey(k));
  const visibleKeys = showRevoked ? keys : keys.filter(isActiveKey);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-2">
        {app.disabled_at === null ? (
          <Button size="sm" onClick={onMint}>
            Mint key
          </Button>
        ) : (
          <span />
        )}
        {revokedKeys.length > 0 && (
          <Button
            size="sm"
            variant="secondary"
            onClick={() => setShowRevoked((value) => !value)}
          >
            {showRevoked ? "Hide" : "Show"} revoked · {revokedKeys.length}
          </Button>
        )}
      </div>
      {visibleKeys.length === 0 ? (
        <span className="text-vf-micro text-vf-muted">
          No active keys. {revokedKeys.length} revoked hidden.
        </span>
      ) : (
        <ul className="divide-vf-border/60 flex flex-col divide-y">
          {visibleKeys.map((credential) => (
            <li
              key={credential.id}
              className="flex items-start justify-between gap-3 py-3"
            >
              <div className="flex min-w-0 flex-col gap-1">
                <span className="text-vf-body text-vf-text truncate">
                  {credential.display_name}
                </span>
                <SecretMask prefix={credential.prefix} />
                <span className="text-vf-micro text-vf-subtle">
                  Last used{" "}
                  <RelativeTime ts={credential.last_used_at} fallback="never" />
                </span>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <KeyStatusBadge credential={credential} />
                <OverflowMenu label={`Actions for ${credential.display_name}`}>
                  <OverflowMenuItem
                    danger
                    disabled={!isActiveKey(credential)}
                    onSelect={() => onRevoke(credential)}
                  >
                    Revoke key…
                  </OverflowMenuItem>
                </OverflowMenu>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Create app dialog

function CreateAppDialog({
  open,
  onOpenChange,
  users,
  profiles,
  profilesJoinState,
  profilesStale,
  onRetryProfiles,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  users: User[];
  profiles: Profile[];
  profilesJoinState: JoinState;
  profilesStale: boolean;
  onRetryProfiles: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* Mounted only while open so form state starts fresh each time. */}
      {open && (
        <CreateAppDialogContent
          onOpenChange={onOpenChange}
          users={users}
          profiles={profiles}
          profilesJoinState={profilesJoinState}
          profilesStale={profilesStale}
          onRetryProfiles={onRetryProfiles}
        />
      )}
    </Dialog>
  );
}

function CreateAppDialogContent({
  onOpenChange,
  users,
  profiles,
  profilesJoinState,
  profilesStale,
  onRetryProfiles,
}: {
  onOpenChange: (open: boolean) => void;
  users: User[];
  profiles: Profile[];
  profilesJoinState: JoinState;
  profilesStale: boolean;
  onRetryProfiles: () => void;
}) {
  const queryClient = useQueryClient();
  const [kind, setKind] = useState<AppKind>("service");
  const [name, setName] = useState("");
  const [ownerId, setOwnerId] = useState("");
  const [profileId, setProfileId] = useState("");
  const [fieldErrors, setFieldErrors] = useState<{
    name?: string;
    owner?: string;
  }>({});
  const [formError, setFormError] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: api.apps.create,
    onSuccess: async (app) => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.apps });
      toast.success(`${app.name} created`, {
        description: "Mint a key to let it call the gateway.",
      });
      onOpenChange(false);
    },
    onError: (error) => setFormError(errorMessage(error)),
  });

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const trimmed = name.trim();
    const errors: { name?: string; owner?: string } = {};
    if (!trimmed) errors.name = "Give the app a name.";
    if (kind === "personal" && !ownerId)
      errors.owner = "Pick who this app belongs to.";
    setFieldErrors(errors);
    setFormError(null);
    if (Object.keys(errors).length > 0) return;
    create.mutate({
      name: trimmed,
      kind,
      ...(profileId ? { profile_id: profileId } : {}),
      ...(kind === "personal" ? { owner_user_id: ownerId } : {}),
    });
  }

  return (
    <DialogContent>
      <DialogForm onSubmit={handleSubmit}>
        <DialogHeader>
          <DialogTitle>Create app</DialogTitle>
          <DialogDescription>
            An app groups the keys for one caller — a service, a script, or a
            person&apos;s device.
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          <RadioCards
            value={kind}
            onValueChange={(value) => setKind(value as AppKind)}
            aria-label="App type"
          >
            <RadioCard
              value="service"
              title="Shared"
              description="Used by a service or several people."
            />
            <RadioCard
              value="personal"
              title="Personal"
              description="Belongs to one person."
            />
          </RadioCards>
          <Field label="Name" required error={fieldErrors.name}>
            <Input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="e.g. brother-laptop"
              autoFocus
              disabled={create.isPending}
            />
          </Field>
          {kind === "personal" && (
            <Field label="Owner" required error={fieldErrors.owner}>
              <Select
                value={ownerId}
                onChange={(event) => setOwnerId(event.target.value)}
                disabled={create.isPending}
              >
                <option value="">Select a person…</option>
                {users.map((user) => (
                  <option key={user.id} value={user.id}>
                    {userLabel(user)}
                  </option>
                ))}
              </Select>
            </Field>
          )}
          <Field
            label="Profile"
            help="Controls what the app can reach. You can change it later."
          >
            {profilesJoinState !== "available" && (
              <div className="text-vf-small text-vf-muted mb-2 flex items-center gap-2">
                <span>
                  {profilesJoinState === "unavailable"
                    ? "Profiles are unavailable; retry before choosing one."
                    : "Loading profiles before you choose one."}
                </span>
                {profilesJoinState === "unavailable" && (
                  <Button type="button" size="sm" onClick={onRetryProfiles}>
                    Retry
                  </Button>
                )}
              </div>
            )}
            {profilesStale && (
              <div className="text-vf-small text-vf-muted mb-2 flex items-center gap-2">
                <span>
                  Showing retained profile facts; refresh them before saving.
                </span>
                <Button type="button" size="sm" onClick={onRetryProfiles}>
                  Retry
                </Button>
              </div>
            )}
            <Select
              value={profileId}
              onChange={(event) => setProfileId(event.target.value)}
              disabled={
                create.isPending ||
                profilesJoinState !== "available" ||
                profilesStale
              }
            >
              {profilesJoinState === "available" && !profilesStale ? (
                <>
                  <option value="">Use gateway default</option>
                  {profiles.map((profile) => (
                    <option key={profile.id} value={profile.id}>
                      {profile.name}
                    </option>
                  ))}
                </>
              ) : (
                <option value="">Profiles are not ready</option>
              )}
            </Select>
          </Field>
          {formError && <FormErrorSummary message={formError} />}
        </DialogBody>
        <DialogFooter>
          <Button
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={create.isPending}
          >
            Cancel
          </Button>
          <Button
            type="submit"
            variant="primary"
            loading={create.isPending}
            disabled={profilesJoinState !== "available" || profilesStale}
          >
            Create app
          </Button>
        </DialogFooter>
      </DialogForm>
    </DialogContent>
  );
}

// ---------------------------------------------------------------------------
// Change profile dialog (never an inline select-per-row)

function ChangeProfileDialog({
  app,
  onOpenChange,
  profiles,
  profilesJoinState,
  profilesStale,
  onRetryProfiles,
}: {
  app: App | null;
  onOpenChange: (open: boolean) => void;
  profiles: Profile[];
  profilesJoinState: JoinState;
  profilesStale: boolean;
  onRetryProfiles: () => void;
}) {
  return (
    <Dialog open={app !== null} onOpenChange={onOpenChange}>
      {app && (
        <ChangeProfileDialogContent
          app={app}
          onOpenChange={onOpenChange}
          profiles={profiles}
          profilesJoinState={profilesJoinState}
          profilesStale={profilesStale}
          onRetryProfiles={onRetryProfiles}
        />
      )}
    </Dialog>
  );
}

function ChangeProfileDialogContent({
  app,
  onOpenChange,
  profiles,
  profilesJoinState,
  profilesStale,
  onRetryProfiles,
}: {
  app: App;
  onOpenChange: (open: boolean) => void;
  profiles: Profile[];
  profilesJoinState: JoinState;
  profilesStale: boolean;
  onRetryProfiles: () => void;
}) {
  const queryClient = useQueryClient();
  const [profileId, setProfileId] = useState(app.default_profile_id ?? "");
  const [formError, setFormError] = useState<string | null>(null);

  const update = useMutation({
    mutationFn: () =>
      api.apps.update(
        app.id,
        profileId ? { default_profile_id: profileId } : {},
      ),
    onSuccess: async (serverApp) => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.apps });
      toast.success(`${serverApp.name} now uses ${profileLabel(serverApp)}`);
      onOpenChange(false);
    },
    onError: (error) => setFormError(errorMessage(error)),
  });

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setFormError(null);
    update.mutate();
  }

  return (
    <DialogContent className="max-w-md">
      <DialogForm onSubmit={handleSubmit}>
        <DialogHeader>
          <DialogTitle>Change profile</DialogTitle>
          <DialogDescription>
            New keys and existing keys of {app.name} follow this profile.
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          <Field label="Profile">
            {profilesJoinState !== "available" && (
              <div className="text-vf-small text-vf-muted mb-2 flex items-center gap-2">
                <span>
                  {profilesJoinState === "unavailable"
                    ? "Profiles are unavailable; retry before changing this app."
                    : "Loading profiles before you change this app."}
                </span>
                {profilesJoinState === "unavailable" && (
                  <Button type="button" size="sm" onClick={onRetryProfiles}>
                    Retry
                  </Button>
                )}
              </div>
            )}
            {profilesStale && (
              <div className="text-vf-small text-vf-muted mb-2 flex items-center gap-2">
                <span>
                  Showing retained profile facts; refresh them before saving.
                </span>
                <Button type="button" size="sm" onClick={onRetryProfiles}>
                  Retry
                </Button>
              </div>
            )}
            <Select
              value={profileId}
              onChange={(event) => setProfileId(event.target.value)}
              autoFocus
              disabled={
                update.isPending ||
                profilesJoinState !== "available" ||
                profilesStale
              }
            >
              {profilesJoinState === "available" && !profilesStale ? (
                <>
                  <option value="">Use gateway default</option>
                  {profiles.map((profile) => (
                    <option key={profile.id} value={profile.id}>
                      {profile.name}
                    </option>
                  ))}
                </>
              ) : (
                <option value="">Profiles are not ready</option>
              )}
            </Select>
          </Field>
          {formError && <FormErrorSummary message={formError} />}
        </DialogBody>
        <DialogFooter>
          <Button
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={update.isPending}
          >
            Cancel
          </Button>
          <Button
            type="submit"
            variant="primary"
            loading={update.isPending}
            disabled={profilesJoinState !== "available" || profilesStale}
          >
            Save
          </Button>
        </DialogFooter>
      </DialogForm>
    </DialogContent>
  );
}

// ---------------------------------------------------------------------------
// Mint key dialog

function MintKeyDialog({
  app,
  onOpenChange,
  onMinted,
}: {
  app: App | null;
  onOpenChange: (open: boolean) => void;
  onMinted: (credential: MintedCredential, app: App) => void;
}) {
  return (
    <Dialog open={app !== null} onOpenChange={onOpenChange}>
      {app && (
        <MintKeyDialogContent
          app={app}
          onOpenChange={onOpenChange}
          onMinted={onMinted}
        />
      )}
    </Dialog>
  );
}

function MintKeyDialogContent({
  app,
  onOpenChange,
  onMinted,
}: {
  app: App;
  onOpenChange: (open: boolean) => void;
  onMinted: (credential: MintedCredential, app: App) => void;
}) {
  const queryClient = useQueryClient();
  const [label, setLabel] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  const mint = useMutation({
    mutationFn: () =>
      api.apps.mintKey(app.id, label.trim() ? { label: label.trim() } : {}),
    onSuccess: async (credential) => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.credentials });
      onMinted(credential, app);
    },
    onError: (error) => setFormError(errorMessage(error)),
  });

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setFormError(null);
    mint.mutate();
  }

  return (
    <DialogContent className="max-w-md">
      <DialogForm onSubmit={handleSubmit}>
        <DialogHeader>
          <DialogTitle>Mint key</DialogTitle>
          <DialogDescription>
            Creates a new key for {app.name}. The full key is shown once, right
            after minting.
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          <Field
            label="Label"
            help="Shown in lists so you can tell keys apart. Not secret."
          >
            <Input
              value={label}
              onChange={(event) => setLabel(event.target.value)}
              placeholder="e.g. laptop, CI"
              autoFocus
              disabled={mint.isPending}
            />
          </Field>
          {formError && <FormErrorSummary message={formError} />}
        </DialogBody>
        <DialogFooter>
          <Button
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={mint.isPending}
          >
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={mint.isPending}>
            Mint key
          </Button>
        </DialogFooter>
      </DialogForm>
    </DialogContent>
  );
}

// ---------------------------------------------------------------------------
// Create admin key dialog

function CreateAdminKeyDialog({
  open,
  onOpenChange,
  canMintOwner,
  onMinted,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  canMintOwner: boolean;
  onMinted: (credential: MintedCredential) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {open && (
        <CreateAdminKeyDialogContent
          onOpenChange={onOpenChange}
          canMintOwner={canMintOwner}
          onMinted={onMinted}
        />
      )}
    </Dialog>
  );
}

function CreateAdminKeyDialogContent({
  onOpenChange,
  canMintOwner,
  onMinted,
}: {
  onOpenChange: (open: boolean) => void;
  canMintOwner: boolean;
  onMinted: (credential: MintedCredential) => void;
}) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [role, setRole] = useState<"admin" | "owner">("admin");
  const [nameError, setNameError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () => api.adminKeys.create({ name: name.trim(), role }),
    onSuccess: async (credential) => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.credentials });
      onMinted(credential);
    },
    onError: (error) => setFormError(errorMessage(error)),
  });

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setFormError(null);
    if (!name.trim()) {
      setNameError("Give the key a name.");
      return;
    }
    setNameError(null);
    create.mutate();
  }

  return (
    <DialogContent>
      <DialogForm onSubmit={handleSubmit}>
        <DialogHeader>
          <DialogTitle>Create admin key</DialogTitle>
          <DialogDescription>
            For automation that manages the gateway itself. The full key is
            shown once, right after creating.
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          <Field label="Name" required error={nameError}>
            <Input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="e.g. deploy-bot"
              autoFocus
              disabled={create.isPending}
            />
          </Field>
          <RadioCards
            value={role}
            onValueChange={(value) => setRole(value as "admin" | "owner")}
            aria-label="Role"
          >
            <RadioCard
              value="admin"
              title="Admin"
              description="Manage apps, keys, and profiles."
            />
            <RadioCard
              value="owner"
              title="Owner"
              description={
                canMintOwner
                  ? "Everything, including other admin keys."
                  : "Only owners can mint owner keys."
              }
              disabled={!canMintOwner}
            />
          </RadioCards>
          {formError && <FormErrorSummary message={formError} />}
        </DialogBody>
        <DialogFooter>
          <Button
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={create.isPending}
          >
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={create.isPending}>
            Create key
          </Button>
        </DialogFooter>
      </DialogForm>
    </DialogContent>
  );
}

// ---------------------------------------------------------------------------
// Reveal-once key dialog (mint result). The secret lives only in this
// dialog's props; closing it discards the value — masked-after-close, never
// logged, never refetchable.

function RevealKeyDialog({
  revealed,
  onClose,
}: {
  revealed: RevealedKey | null;
  onClose: () => void;
}) {
  return (
    <Dialog
      open={revealed !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      {revealed && (
        <RevealKeyDialogContent revealed={revealed} onClose={onClose} />
      )}
    </Dialog>
  );
}

function RevealKeyDialogContent({
  revealed,
  onClose,
}: {
  revealed: RevealedKey;
  onClose: () => void;
}) {
  const { credential, context, kind } = revealed;
  const origin = typeof window === "undefined" ? "" : window.location.origin;
  // Honest, runnable check — same pattern as the portal's reveal flow.
  const curl =
    kind === "admin"
      ? `curl ${origin}/admin/api/overview \\\n  -H "Authorization: Bearer ${credential.api_key}"`
      : `curl ${origin}/v1/models \\\n  -H "Authorization: Bearer ${credential.api_key}"`;

  // No accidental dismissal: the key is gone after close, so closing is the
  // explicit "Done" action only.
  const block = (event: Event) => event.preventDefault();

  return (
    <DialogContent
      hideClose
      onEscapeKeyDown={block}
      onPointerDownOutside={block}
      onInteractOutside={block}
    >
      <DialogHeader>
        <DialogTitle>Save your new key</DialogTitle>
        <DialogDescription>
          The key {context} is shown only this once. Copy it somewhere safe —
          after you close this it can&apos;t be retrieved.
        </DialogDescription>
      </DialogHeader>
      <DialogBody>
        <KeyRevealBlock secret={credential.api_key} label="Your key" canCopy />
        <CopyBlock label="Run this" value={curl} />
      </DialogBody>
      <DialogFooter>
        <Button variant="primary" onClick={onClose}>
          Done — I saved it
        </Button>
      </DialogFooter>
    </DialogContent>
  );
}

/** Titled mono block with its own copy button (reveal dialog only). */
function CopyBlock({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between gap-2">
        <span className="text-vf-micro text-vf-subtle font-mono uppercase">
          {label}
        </span>
        <CopyButton value={value} aria-label={`Copy ${label.toLowerCase()}`} />
      </div>
      <pre className="rounded-vf-md border-vf-border bg-vf-surface-2 text-vf-micro text-vf-text border p-3 font-mono break-all whitespace-pre-wrap">
        {value}
      </pre>
    </div>
  );
}
