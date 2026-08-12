"use client";

/** Admin → Services — backend service registry.
 *
 * Spec: docs/design/WEB_UI_STEERING.md "Services" section.
 * Contract: docs/design/web-api-contract.md "Surface 2 — Services".
 *
 * EDITABILITY LAW: all rows are fully editable/removable regardless of source.
 * Sole exception: a gateway-managed substrate — no remove; endpoint fields
 * locked; rename/enable/note only. Source is provenance (history) not
 * permission. The `editable` field on each service record is authoritative.
 *
 * Presentation law: RelativeTime for every timestamp, EntityRef for every id,
 * no window.confirm, destructive actions in OverflowMenu + ConfirmDialog,
 * loading skeleton + designed empty + error states. */

import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  api,
  ApiError,
  queryKeys,
  type BackendHistory,
  type Service,
  type ServiceRegistry,
  type StatusPayload,
  type StatusSeriesPoint,
} from "@/lib/api";
import {
  deriveServicesHealthSummary,
  type ServicesHealthSummary,
} from "@/lib/services-health-summary";
import { projectStatusHealth } from "@/lib/status-health-projection";
import {
  Badge,
  Button,
  type ChartPoint,
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
  IncidentBanner,
  Input,
  OverflowMenu,
  OverflowMenuItem,
  OverflowMenuSeparator,
  RightContextPanel,
  type RightContextPanelSection,
  SectionActionHeader,
  Select,
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  Skeleton,
  StatusDotLabel,
  Switch,
  toast,
  TrendLineChart,
} from "@/components/ui";
import { AddServiceDialog } from "@/components/admin/add-service-dialog";
import { safeAuditReturnPath } from "@/lib/admin-links";

// ---------------------------------------------------------------------------
// History series helpers

/** Convert [timestamp_s, value][] from the status API to ChartPoint[]. */
function toChartPoints(series: StatusSeriesPoint[] | undefined): ChartPoint[] {
  if (!series || series.length === 0) return [];
  return series
    .filter(
      (pt) => Array.isArray(pt) && pt.length >= 2 && Number.isFinite(pt[1]),
    )
    .map((pt) => ({ x: pt[0], y: pt[1] }));
}

/** Tone for the latency sparkline based on p95. */
function latencyTone(
  history: BackendHistory | undefined,
): "ok" | "warn" | "bad" {
  const p95 = history?.p95_latency_ms_24h;
  if (p95 == null) return "ok";
  if (p95 > 2000) return "bad";
  if (p95 > 800) return "warn";
  return "ok";
}

/** Format uptime pct, returning null when there is no data. */
function formatUptime(pct: number | null | undefined): string | null {
  if (pct == null) return null;
  return `${pct.toFixed(1)}%`;
}

// ---------------------------------------------------------------------------
// Types used only in this surface

interface TestProbe {
  ok: boolean;
  status: number | null;
  error: string;
  url: string;
}

// ---------------------------------------------------------------------------
function capabilityLabel(cap: string): string {
  const readable = cap.replace(/[._]/g, " ").replace(/-/g, " ");
  return readable.charAt(0).toUpperCase() + readable.slice(1).toLowerCase();
}

// ---------------------------------------------------------------------------
// Helpers

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 412) {
      return "The service registry changed elsewhere (412 Precondition Failed). Refresh and retry.";
    }
    return error.message;
  }
  return "Something went wrong. Please try again.";
}

/** Best-effort endpoint display: use base_url if present, else host:port. */
function endpointDisplay(service: Service): string {
  if (service.config.base_url) {
    try {
      const url = new URL(service.config.base_url);
      return url.host;
    } catch {
      return service.config.base_url;
    }
  }
  if (service.config.host) {
    const port = service.config.port ? `:${service.config.port}` : "";
    return `${service.config.host}${port}`;
  }
  return "—";
}

function servicesFromRegistry(registry: ServiceRegistry): Service[] {
  return Object.values(registry.services ?? {}).sort((a, b) =>
    a.name.localeCompare(b.name),
  );
}

/** Keep local toggle choices visible until their own requests settle. */
function withPendingToggleIntents(
  registry: ServiceRegistry | undefined,
  intents: ReadonlyMap<string, boolean>,
): ServiceRegistry | undefined {
  if (!registry || intents.size === 0) return registry;
  return {
    ...registry,
    services: Object.fromEntries(
      Object.entries(registry.services).map(([serviceId, service]) => [
        serviceId,
        intents.has(serviceId)
          ? { ...service, enabled: intents.get(serviceId)! }
          : service,
      ]),
    ),
  };
}

function RouteServiceTargetSync({
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
      target: searchParams.get("service"),
      returnPath: safeAuditReturnPath(searchParams.get("return")),
    });
  }, [onChange, searchParams]);
  return null;
}

function backendByServiceId(status: StatusPayload | undefined) {
  const result = new Map<
    string,
    NonNullable<StatusPayload["backends"]>[number]
  >();
  for (const backend of status?.backends ?? []) {
    const serviceId = backend.service_id || backend.key;
    if (serviceId) result.set(serviceId, backend);
  }
  return result;
}

function healthByServiceId(status: StatusPayload | undefined) {
  return new Map(
    (status?.current_health?.services ?? []).map((service) => [
      service.service_id,
      service,
    ]),
  );
}

function serviceLiveTone(
  service: Service,
  health:
    | NonNullable<StatusPayload["current_health"]>["services"][number]
    | undefined,
): { tone: "ok" | "warn" | "bad" | "neutral"; label: string } {
  if (!service.enabled) {
    return {
      tone: "neutral",
      label: service.role === "standby" ? "standby, disabled" : "disabled",
    };
  }
  if (!health) {
    return { tone: "neutral" as const, label: "live status unavailable" };
  }
  const tone: "ok" | "warn" | "bad" | "neutral" =
    health.tone === "ok" || health.tone === "warn" || health.tone === "bad"
      ? health.tone
      : "neutral";
  if (health.observation === "standby_stopped") {
    return { tone, label: "standby, stopped" };
  }
  if (health.observation === "unavailable") {
    return { tone, label: "live status unavailable" };
  }
  if (health.observation === "down") {
    return {
      tone,
      label:
        service.role === "required"
          ? "required service down"
          : service.role === "optional"
            ? "optional service down"
            : "standby service unavailable",
    };
  }
  if (health.observation === "degraded") {
    return { tone, label: "service degraded" };
  }
  return { tone, label: health.observation.replace(/_/g, " ") };
}

function serviceLiveMetrics(
  backend: NonNullable<StatusPayload["backends"]>[number] | undefined,
): Array<{ label: string; value: string }> | undefined {
  if (!backend) return undefined;
  const metrics: Array<{ label: string; value: string }> = [];
  if (typeof backend.latency_ms === "number" && backend.latency_ms !== null) {
    metrics.push({
      label: "latency",
      value: `${Math.round(backend.latency_ms)} ms`,
    });
  }
  if (metrics.length === 0) return undefined;
  return metrics;
}

/** True when a service record has gateway-derived connection fields. */
function isManagedSubstrate(service: Service): boolean {
  return service.editable === false;
}

// ---------------------------------------------------------------------------
// Source badge — provenance label only (no lock icon; all rows are editable)

type BadgeSource = "provisioned" | "ui" | "builtin" | "imported" | "discovered";

function SourceBadge({
  source,
  managed,
}: {
  source: BadgeSource;
  managed: boolean;
}) {
  if (managed) {
    return (
      <span title="Managed by the gateway — endpoint fields are derived">
        <Badge sm tone="neutral">
          managed
        </Badge>
      </span>
    );
  }
  if (source === "provisioned") {
    return (
      <span title="Imported from your config file — your YAML is not modified and will not re-import it">
        <Badge sm tone="neutral">
          config
        </Badge>
      </span>
    );
  }
  return (
    <Badge sm tone="info">
      ui
    </Badge>
  );
}

// ---------------------------------------------------------------------------
// ServiceTableRow — dense table-style row with inline name/endpoint/caps header

interface ServiceTableRowProps {
  svc: Service;
  tone: "ok" | "warn" | "bad" | "neutral";
  statusLabel: string;
  metrics: Array<{ label: string; value: string }> | undefined;
  history?: BackendHistory;
  isSelected: boolean;
  togglePending: boolean;
  onSelect: () => void;
  onToggle: (checked: boolean) => void;
  onEdit: () => void;
  onModels: () => void;
  onTest: () => void;
  onDetect: () => void;
  onRemove: () => void;
}

function ServiceTableRow({
  svc,
  tone,
  statusLabel,
  metrics,
  history,
  isSelected,
  togglePending,
  onSelect,
  onToggle,
  onEdit,
  onModels,
  onTest,
  onDetect,
  onRemove,
}: ServiceTableRowProps) {
  const endpoint = isManagedSubstrate(svc) ? null : endpointDisplay(svc);
  const caps = svc.capabilities;
  const latencyMetric = metrics?.find((m) => m.label === "latency");
  const latencyPoints = toChartPoints(history?.latency_series);
  const sparklineTone = latencyTone(history);

  return (
    <div
      className={`vf-service-row transition-colors ${isSelected ? "bg-[var(--vf-row-hover)]" : ""}`}
    >
      {/* Top line: name + endpoint + caps + actions */}
      <div className="vf-service-row-main">
        <button
          type="button"
          className="min-w-0 flex-1 cursor-pointer text-left"
          onClick={onSelect}
          aria-label={`${svc.name} — click for details`}
          aria-expanded={isSelected}
        >
          <span className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <span className="text-vf-body text-vf-text shrink-0 font-semibold">
              {svc.name}
            </span>
            {isManagedSubstrate(svc) ? (
              <span className="text-vf-micro text-vf-muted shrink-0 font-mono">
                managed
              </span>
            ) : endpoint ? (
              <span className="text-vf-micro text-vf-subtle shrink-0 font-mono">
                {endpoint}
              </span>
            ) : null}
            {caps.length > 0 && (
              <span className="flex flex-wrap gap-x-2 gap-y-0.5">
                {caps.map((c) => (
                  <span
                    key={c}
                    className="text-vf-micro text-vf-info font-mono"
                  >
                    {capabilityLabel(c)}
                  </span>
                ))}
              </span>
            )}
          </span>
        </button>
        {/* Actions always on the right */}
        <div
          className="vf-service-row-actions shrink-0"
          onClick={(e) => e.stopPropagation()}
        >
          <Switch
            checked={svc.enabled}
            disabled={togglePending}
            aria-label={`Toggle ${svc.name}`}
            onCheckedChange={onToggle}
          />
          <OverflowMenu label={`Actions for ${svc.name}`}>
            <OverflowMenuItem onSelect={onEdit}>Edit…</OverflowMenuItem>
            <OverflowMenuItem onSelect={onModels}>Models…</OverflowMenuItem>
            <OverflowMenuItem onSelect={onTest}>
              Test connection…
            </OverflowMenuItem>
            <OverflowMenuItem onSelect={onDetect}>
              Detect models…
            </OverflowMenuItem>
            {isManagedSubstrate(svc) ? (
              <>
                <OverflowMenuSeparator />
                <OverflowMenuItem onSelect={() => onToggle(!svc.enabled)}>
                  {svc.enabled ? "Disable" : "Enable"}
                </OverflowMenuItem>
              </>
            ) : (
              <>
                <OverflowMenuSeparator />
                <OverflowMenuItem danger onSelect={onRemove}>
                  Remove…
                </OverflowMenuItem>
              </>
            )}
          </OverflowMenu>
        </div>
      </div>

      {/* Secondary line: status dot + latency + sparkline */}
      <div className="flex min-h-0 items-center gap-4">
        <StatusDotLabel tone={tone} label={statusLabel} pulse={tone === "ok"} />
        {latencyMetric && (
          <span className="flex items-baseline gap-1">
            <span className="text-vf-small text-vf-text font-mono tabular-nums">
              {latencyMetric.value}
            </span>
            <span className="text-vf-micro text-vf-muted font-mono uppercase">
              latency
            </span>
          </span>
        )}
        {latencyPoints.length >= 3 && (
          <TrendLineChart
            series={latencyPoints}
            tone={sparklineTone}
            height={24}
            className="w-16 shrink-0"
          />
        )}
        {history?.uptime_pct_24h != null && (
          <span className="text-vf-micro text-vf-muted ml-auto font-mono tabular-nums">
            {history.uptime_pct_24h.toFixed(1)}% up 24h
          </span>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// StatusSummaryRail — aggregate right rail when no service is selected.
// Uses RightContextPanel sections so visual treatment matches the per-service detail panel.

interface StatusSummaryRailProps {
  healthLabel: string;
  healthTone: "ok" | "warn" | "bad" | "neutral";
  enabledCount: number;
  totalCount: number;
  healthSummary: ServicesHealthSummary;
  avgLatencyMs: number | null;
  modelMappingCount: number;
  capabilityCoverage: string[];
  avgUptime24h: number | null;
  avgP95Latency24h: number | null;
}

function StatusSummaryRail({
  healthLabel,
  healthTone,
  enabledCount,
  totalCount,
  healthSummary,
  avgLatencyMs,
  modelMappingCount,
  capabilityCoverage,
  avgUptime24h,
  avgP95Latency24h,
}: StatusSummaryRailProps) {
  const railTone = healthTone;
  const railTitle = healthLabel;

  type MetricRow = {
    label: string;
    value: string;
    tone?: "ok" | "warn" | "bad";
  };
  const metricRows: MetricRow[] = [
    {
      label: "Backends enabled",
      value: `${enabledCount} / ${totalCount}`,
      tone:
        healthSummary.metricTone === "neutral"
          ? undefined
          : healthSummary.metricTone,
    },
    {
      label: "Avg latency",
      value: avgLatencyMs !== null ? `${Math.round(avgLatencyMs)} ms` : "—",
      tone: avgLatencyMs !== null && avgLatencyMs > 1000 ? "warn" : undefined,
    },
    { label: "Model mappings", value: String(modelMappingCount) },
  ];
  if (avgUptime24h != null) {
    metricRows.push({
      label: "Avg uptime 24h",
      value: `${avgUptime24h.toFixed(1)}%`,
      tone: avgUptime24h < 95 ? "bad" : avgUptime24h < 99 ? "warn" : "ok",
    });
  }
  if (avgP95Latency24h != null) {
    metricRows.push({
      label: "p95 latency 24h",
      value: `${avgP95Latency24h} ms`,
      tone:
        avgP95Latency24h > 2000
          ? "bad"
          : avgP95Latency24h > 800
            ? "warn"
            : undefined,
    });
  }
  if (healthSummary.badCount > 0) {
    metricRows.push({
      label: "Action needed",
      value: String(healthSummary.badCount),
      tone: "bad",
    });
  }
  if (healthSummary.warnCount > 0) {
    metricRows.push({
      label: "Needs attention",
      value: String(healthSummary.warnCount),
      tone: "warn",
    });
  }
  if (healthSummary.incompleteCount > 0) {
    metricRows.push({
      label: "Coverage incomplete",
      value: String(healthSummary.incompleteCount),
    });
  }

  const sections: RightContextPanelSection[] = [
    {
      title: "Health",
      content: (
        <div className="flex flex-col gap-2">
          {metricRows.map((row) => (
            <div
              key={row.label}
              className="flex items-center justify-between gap-2"
            >
              <span className="text-vf-micro text-vf-subtle">{row.label}</span>
              <span
                className={
                  row.tone === "bad"
                    ? "text-vf-small text-vf-bad font-mono tabular-nums"
                    : row.tone === "warn"
                      ? "text-vf-small text-vf-warn font-mono tabular-nums"
                      : row.tone === "ok"
                        ? "text-vf-small text-vf-ok font-mono tabular-nums"
                        : "text-vf-small text-vf-text font-mono tabular-nums"
                }
              >
                {row.value}
              </span>
            </div>
          ))}
        </div>
      ),
    },
    ...(capabilityCoverage.length > 0
      ? [
          {
            title: "Capabilities",
            content: (
              <div className="flex flex-wrap gap-1.5">
                {capabilityCoverage.map((cap) => (
                  <span
                    key={cap}
                    className="text-vf-micro text-vf-subtle rounded border border-[var(--vf-border)] bg-[var(--vf-surface-2)] px-1.5 py-0.5 font-mono"
                  >
                    {cap}
                  </span>
                ))}
              </div>
            ),
          },
        ]
      : []),
  ];

  return (
    <RightContextPanel
      title={railTitle}
      status={
        <StatusDotLabel tone={railTone} label="" pulse={railTone === "ok"} />
      }
      sections={sections}
    />
  );
}

// ---------------------------------------------------------------------------
// Live-apply banner (shown after every write)

function LiveApplyBanner({ onDismiss }: { onDismiss: () => void }) {
  return (
    <IncidentBanner
      tone="info"
      title="Saved — applied live. No restart needed."
      onDismiss={onDismiss}
    />
  );
}

// ---------------------------------------------------------------------------
// Error banner

function ErrorBanner({
  title,
  onRetry,
}: {
  title: string;
  onRetry: () => void;
}) {
  return (
    <IncidentBanner
      tone="bad"
      title={title}
      actions={
        <Button size="sm" onClick={onRetry}>
          Retry
        </Button>
      }
    />
  );
}

/** Keep the compact modal and desktop context rail mutually exclusive. */
function useDesktopServiceContext() {
  const query = "(min-width: 980px)";
  const [isDesktop, setIsDesktop] = useState(
    () => typeof window !== "undefined" && window.matchMedia(query).matches,
  );

  useEffect(() => {
    const media = window.matchMedia(query);
    const update = () => setIsDesktop(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  return isDesktop;
}

// ---------------------------------------------------------------------------
// The surface

export default function AdminServicesPage() {
  const queryClient = useQueryClient();
  const [routeServiceTarget, setRouteServiceTarget] = useState<string | null>(
    null,
  );
  const [auditReturnPath, setAuditReturnPath] = useState<string | null>(null);

  const {
    data: registry,
    isPending,
    isError,
    error,
    refetch,
  } = useQuery({
    queryKey: queryKeys.serviceRegistry,
    queryFn: api.services.registry,
  });
  const {
    data: statusData,
    isPending: statusPending,
    isError: statusError,
  } = useQuery({
    queryKey: queryKeys.status,
    queryFn: api.status,
    staleTime: 10_000,
  });

  const liveBackends = useMemo(
    () => backendByServiceId(statusData),
    [statusData],
  );
  const currentHealthByService = useMemo(
    () => healthByServiceId(statusData),
    [statusData],
  );
  const [addOpen, setAddOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<Service | null>(null);
  const [removeTarget, setRemoveTarget] = useState<Service | null>(null);
  const [testTarget, setTestTarget] = useState<Service | null>(null);
  const [detectTarget, setDetectTarget] = useState<Service | null>(null);
  const [modelsTarget, setModelsTarget] = useState<Service | null>(null);
  const [panelService, setPanelService] = useState<Service | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const consumedServiceTargetRef = useRef<string | null>(null);
  /** Banner shown after any write confirming live apply. */
  const [restartBanner, setRestartBanner] = useState(false);
  const [pendingToggleIntents, setPendingToggleIntents] = useState<
    ReadonlyMap<string, boolean>
  >(new Map());
  const isDesktopServiceContext = useDesktopServiceContext();

  const displayedRegistry = useMemo(
    () => withPendingToggleIntents(registry, pendingToggleIntents),
    [registry, pendingToggleIntents],
  );
  const services = useMemo(
    () => (displayedRegistry ? servicesFromRegistry(displayedRegistry) : []),
    [displayedRegistry],
  );
  const pendingToggleServiceIds = useMemo(
    () => new Set(pendingToggleIntents.keys()),
    [pendingToggleIntents],
  );
  const enabledCount = services.filter((svc) => svc.enabled).length;
  const modelMappingCount = services.reduce(
    (count, svc) => count + (svc.config.models?.length ?? 0),
    0,
  );

  function showRestartBanner() {
    setRestartBanner(true);
  }

  function updateRegistry(newRegistry: ServiceRegistry) {
    queryClient.setQueryData(queryKeys.serviceRegistry, newRegistry);
  }

  function setPendingToggleIntent(serviceId: string, enabled: boolean | null) {
    setPendingToggleIntents((current) => {
      const next = new Map(current);
      if (enabled === null) next.delete(serviceId);
      else next.set(serviceId, enabled);
      return next;
    });
  }

  // Toggle enabled — optimistic update
  const toggleMutation = useMutation({
    mutationFn: ({
      serviceId,
      enabled,
    }: {
      serviceId: string;
      enabled: boolean;
    }) => api.services.toggleEnabled(serviceId, enabled),
    onMutate: async ({ serviceId, enabled }) => {
      setPendingToggleIntent(serviceId, enabled);
      await queryClient.cancelQueries({ queryKey: queryKeys.serviceRegistry });
      const previous = queryClient.getQueryData<ServiceRegistry>(
        queryKeys.serviceRegistry,
      );
      queryClient.setQueryData<ServiceRegistry>(
        queryKeys.serviceRegistry,
        (reg) => {
          if (!reg) return reg;
          const existing = reg.services[serviceId];
          if (!existing) return reg;
          return {
            ...reg,
            services: {
              ...reg.services,
              [serviceId]: { ...existing, enabled },
            },
          };
        },
      );
      return { previous };
    },
    onError: (err, variables, context) => {
      if (context?.previous) {
        queryClient.setQueryData<ServiceRegistry>(
          queryKeys.serviceRegistry,
          (current) => {
            if (!current || !context.previous) return context.previous;
            const previousService = context.previous.services[variables.serviceId];
            const currentService = current.services[variables.serviceId];
            if (!previousService || !currentService) return current;
            return {
              ...current,
              services: {
                ...current.services,
                [variables.serviceId]: previousService,
              },
            };
          },
        );
      }
      toast.error(`Couldn't update service`, {
        description: errorMessage(err),
      });
    },
    onSuccess: (newRegistry) => {
      updateRegistry(newRegistry);
      showRestartBanner();
    },
    onSettled: (_data, _error, variables) => {
      setPendingToggleIntent(variables.serviceId, null);
      void queryClient.invalidateQueries({
        queryKey: queryKeys.serviceRegistry,
      });
      void queryClient.invalidateQueries({ queryKey: queryKeys.status });
    },
  });

  // Remove mutation
  const removeMutation = useMutation({
    mutationFn: (serviceId: string) => api.services.remove(serviceId),
    onSuccess: (newRegistry) => {
      updateRegistry(newRegistry);
      toast.success("Service removed");
      showRestartBanner();
      setRemoveTarget(null);
      setPanelService(null);
    },
    onError: (err) => {
      toast.error("Couldn't remove service", {
        description: errorMessage(err),
      });
    },
    onSettled: () => {
      void queryClient.invalidateQueries({
        queryKey: queryKeys.serviceRegistry,
      });
      void queryClient.invalidateQueries({ queryKey: queryKeys.status });
    },
  });

  const filteredServices = useMemo(() => {
    if (!searchQuery.trim()) return services;
    const q = searchQuery.toLowerCase();
    return services.filter(
      (svc) =>
        svc.name.toLowerCase().includes(q) ||
        endpointDisplay(svc).toLowerCase().includes(q) ||
        svc.capabilities.some((c) =>
          capabilityLabel(c).toLowerCase().includes(q),
        ),
    );
  }, [services, searchQuery]);

  const routeSelectedService = useMemo(() => {
    if (!routeServiceTarget) return null;
    return services.find((svc) => svc.id === routeServiceTarget) ?? null;
  }, [routeServiceTarget, services]);

  useEffect(() => {
    if (!routeServiceTarget) {
      consumedServiceTargetRef.current = null;
      return;
    }
    if (consumedServiceTargetRef.current === routeServiceTarget) {
      return;
    }
    if (!routeSelectedService) {
      return;
    }
    if (panelService?.id !== routeSelectedService.id) {
      setPanelService(routeSelectedService);
    }
    consumedServiceTargetRef.current = routeServiceTarget;
  }, [panelService?.id, routeServiceTarget, routeSelectedService]);

  const missingRouteService =
    routeServiceTarget !== null &&
    routeSelectedService === null &&
    !isPending &&
    services.length > 0;

  useEffect(() => {
    if (routeServiceTarget && !routeSelectedService && panelService !== null) {
      setPanelService(null);
    }
  }, [panelService, routeSelectedService, routeServiceTarget]);

  const panelSections = useMemo((): RightContextPanelSection[] => {
    if (!panelService) return [];
    const svc = panelService;
    const backend = liveBackends.get(svc.id);
    const sections: RightContextPanelSection[] = [];

    // Connection info
    sections.push({
      title: "Connection",
      content: (
        <div className="flex flex-col gap-2">
          {!isManagedSubstrate(svc) && (
            <div className="text-vf-small text-vf-subtle font-mono break-all">
              {endpointDisplay(svc)}
            </div>
          )}
          <div className="flex flex-wrap gap-1">
            <SourceBadge
              source={svc.source as BadgeSource}
              managed={isManagedSubstrate(svc)}
            />
            {svc.role && svc.role !== "required" && (
              <Badge sm tone="neutral">
                {svc.role}
              </Badge>
            )}
          </div>
          {backend?.last_error && (
            <p className="text-vf-micro text-vf-bad font-mono break-all">
              {backend.last_error}
            </p>
          )}
        </div>
      ),
    });

    // Capabilities + model count
    if (svc.capabilities.length > 0 || (svc.config.models?.length ?? 0) > 0) {
      sections.push({
        title: "Routing",
        content: (
          <div className="flex flex-col gap-2">
            {svc.capabilities.length > 0 && (
              <div className="flex flex-wrap gap-1">
                {svc.capabilities.map((cap) => (
                  <Badge key={cap} sm tone="neutral">
                    {capabilityLabel(cap)}
                  </Badge>
                ))}
              </div>
            )}
            {(svc.config.models?.length ?? 0) > 0 && (
              <p className="text-vf-micro text-vf-muted">
                {svc.config.models!.length} model
                {svc.config.models!.length !== 1 ? "s" : ""} mapped
              </p>
            )}
          </div>
        ),
      });
    }

    // History — only when real sample data exists
    const hist = backend?.history;
    const latencyPts = toChartPoints(hist?.latency_series);
    const statusPts = toChartPoints(hist?.status_series);
    if (hist && (hist.sample_count_24h > 0 || latencyPts.length >= 2)) {
      const ltTone = latencyTone(hist);
      sections.push({
        title: "History (24h)",
        content: (
          <div className="flex flex-col gap-3">
            {/* Numeric summary row */}
            <div className="grid grid-cols-2 gap-x-4 gap-y-1.5">
              {hist.uptime_pct_24h != null && (
                <>
                  <span className="text-vf-micro text-vf-subtle">
                    Uptime 24h
                  </span>
                  <span
                    className={`text-vf-small text-right font-mono tabular-nums ${
                      hist.uptime_pct_24h < 95
                        ? "text-vf-bad"
                        : hist.uptime_pct_24h < 99
                          ? "text-vf-warn"
                          : "text-vf-ok"
                    }`}
                  >
                    {formatUptime(hist.uptime_pct_24h)}
                  </span>
                </>
              )}
              {hist.uptime_pct_7d != null && (
                <>
                  <span className="text-vf-micro text-vf-subtle">
                    Uptime 7d
                  </span>
                  <span
                    className={`text-vf-small text-right font-mono tabular-nums ${
                      hist.uptime_pct_7d < 95
                        ? "text-vf-bad"
                        : hist.uptime_pct_7d < 99
                          ? "text-vf-warn"
                          : "text-vf-ok"
                    }`}
                  >
                    {formatUptime(hist.uptime_pct_7d)}
                  </span>
                </>
              )}
              {hist.avg_latency_ms_24h != null && (
                <>
                  <span className="text-vf-micro text-vf-subtle">
                    Avg latency 24h
                  </span>
                  <span className="text-vf-small text-vf-text text-right font-mono tabular-nums">
                    {hist.avg_latency_ms_24h} ms
                  </span>
                </>
              )}
              {hist.p95_latency_ms_24h != null && (
                <>
                  <span className="text-vf-micro text-vf-subtle">
                    p95 latency 24h
                  </span>
                  <span
                    className={`text-vf-small text-right font-mono tabular-nums ${
                      hist.p95_latency_ms_24h > 2000
                        ? "text-vf-bad"
                        : hist.p95_latency_ms_24h > 800
                          ? "text-vf-warn"
                          : "text-vf-text"
                    }`}
                  >
                    {hist.p95_latency_ms_24h} ms
                  </span>
                </>
              )}
              {hist.sample_count_24h > 0 && (
                <>
                  <span className="text-vf-micro text-vf-subtle">Probes</span>
                  <span className="text-vf-small text-vf-muted text-right font-mono tabular-nums">
                    {hist.sample_count_24h}
                  </span>
                </>
              )}
            </div>
            {/* Latency sparkline */}
            {latencyPts.length >= 3 && (
              <div className="flex flex-col gap-1">
                <span className="text-vf-micro text-vf-subtle">
                  Latency trend (24h)
                </span>
                <TrendLineChart
                  series={latencyPts}
                  tone={ltTone}
                  height={36}
                  showTooltip
                />
              </div>
            )}
            {/* Status sparkline */}
            {statusPts.length >= 3 && (
              <div className="flex flex-col gap-1">
                <span className="text-vf-micro text-vf-subtle">
                  Status trend (24h)
                </span>
                <TrendLineChart series={statusPts} tone="ok" height={24} />
              </div>
            )}
          </div>
        ),
      });
    }

    // Operator note
    if (svc.config.note) {
      sections.push({
        title: "Note",
        content: (
          <p className="text-vf-small text-vf-muted">{svc.config.note}</p>
        ),
      });
    }

    sections.push({
      title: "Actions",
      content: (
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            variant="secondary"
            onClick={() => {
              setEditTarget(svc);
              setPanelService(null);
            }}
          >
            Edit…
          </Button>
          <Button
            size="sm"
            variant="secondary"
            onClick={() => {
              setModelsTarget(svc);
              setPanelService(null);
            }}
          >
            Models…
          </Button>
          <Button
            size="sm"
            variant="secondary"
            onClick={() => {
              setTestTarget(svc);
              setPanelService(null);
            }}
          >
            Test connection…
          </Button>
          <Button
            size="sm"
            variant="secondary"
            onClick={() => {
              setDetectTarget(svc);
              setPanelService(null);
            }}
          >
            Detect models…
          </Button>
          {!isManagedSubstrate(svc) && (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                setRemoveTarget(svc);
                setPanelService(null);
              }}
            >
              Remove…
            </Button>
          )}
        </div>
      ),
    });

    return sections;
  }, [panelService, liveBackends]);

  // Derive aggregate live metrics from backend status
  const avgLatencyMs = useMemo(() => {
    const latencies = (statusData?.backends ?? [])
      .map((b) => b.latency_ms)
      .filter((v): v is number => typeof v === "number" && Number.isFinite(v));
    if (latencies.length === 0) return null;
    return latencies.reduce((sum, v) => sum + v, 0) / latencies.length;
  }, [statusData]);

  const healthProjection = projectStatusHealth({
    data: statusData,
    isPending: statusPending,
    isError: statusError,
  });
  const currentHealth = healthProjection.health;
  const healthSummary = deriveServicesHealthSummary(
    currentHealth,
    enabledCount,
    services.length,
  );
  const heroTone = healthProjection.tone;
  const heroTitle = healthProjection.label;
  const heroDetail =
    services.length === 0
      ? "No services configured yet."
      : healthProjection.detail;

  // Capability coverage for the right rail
  const capabilityCoverage = useMemo(() => {
    const caps = new Set<string>();
    services
      .filter((s) => s.enabled)
      .forEach((s) => s.capabilities.forEach((c) => caps.add(c)));
    return [...caps].map(capabilityLabel);
  }, [services]);

  const servicesWithErrors = (currentHealth?.services ?? []).filter(
    (service) =>
      (service.tone === "bad" || service.tone === "warn") && service.last_error,
  );

  // Aggregate history stats across all backends for the summary rail
  const avgUptime24h = useMemo(() => {
    const vals = (statusData?.backends ?? [])
      .map((b) => b.history?.uptime_pct_24h)
      .filter((v): v is number => typeof v === "number" && Number.isFinite(v));
    if (vals.length === 0) return null;
    return vals.reduce((sum, v) => sum + v, 0) / vals.length;
  }, [statusData]);

  const avgP95Latency24h = useMemo(() => {
    const vals = (statusData?.backends ?? [])
      .map((b) => b.history?.p95_latency_ms_24h)
      .filter((v): v is number => typeof v === "number" && Number.isFinite(v));
    if (vals.length === 0) return null;
    return Math.round(vals.reduce((sum, v) => sum + v, 0) / vals.length);
  }, [statusData]);

  return (
    <div className="flex w-full flex-col gap-8">
      <Suspense fallback={null}>
        <RouteServiceTargetSync
          onChange={({ target, returnPath }) => {
            setRouteServiceTarget(target);
            setAuditReturnPath(returnPath);
          }}
        />
      </Suspense>
      <section className="flex flex-col gap-4">
        <SectionActionHeader
          kicker="infrastructure"
          title="Services"
          description="Configure upstream services. Diagnose current health on Status."
          count={services.length || undefined}
          action={
            <>
              {auditReturnPath && (
                <Button asChild variant="secondary">
                  <Link href={auditReturnPath}>Back to audit</Link>
                </Button>
              )}
              <Button variant="primary" onClick={() => setAddOpen(true)}>
                Add service
              </Button>
            </>
          }
        />

        {/* Operational status hero — vf-status-hero gives it the panel + ambient glow */}
        <div className="vf-status-hero">
          <div className="vf-status-hero-top">
            <div className="flex flex-col gap-2">
              <h2 className="vf-status-hero-title">{heroTitle}</h2>
              <StatusDotLabel
                tone={heroTone}
                label={heroDetail}
                pulse={heroTone === "ok"}
              />
            </div>
          </div>
          <div className="vf-status-hero-metrics">
            <div className="vf-health-metric">
              <span
                className={`vf-health-metric-value tabular-nums ${
                  healthSummary.metricTone === "bad"
                    ? "text-vf-bad"
                    : healthSummary.metricTone === "warn"
                      ? "text-vf-warn"
                      : healthSummary.metricTone === "ok"
                        ? "text-vf-ok"
                        : ""
                }`}
              >
                {enabledCount}/{services.length || "0"}
              </span>
              <span className="vf-health-metric-label">
                {healthSummary.metricLabel}
              </span>
            </div>
            <div className="vf-health-metric">
              <span
                className={`vf-health-metric-value tabular-nums ${
                  avgLatencyMs !== null && avgLatencyMs > 1000
                    ? "text-vf-warn"
                    : ""
                }`}
              >
                {avgLatencyMs !== null ? `${Math.round(avgLatencyMs)} ms` : "—"}
              </span>
              <span className="vf-health-metric-label">avg latency</span>
            </div>
            <div className="vf-health-metric">
              <span className="vf-health-metric-value tabular-nums">
                {modelMappingCount}
              </span>
              <span className="vf-health-metric-label">model mappings</span>
            </div>
          </div>
        </div>

        {/* Incident banner — only shown when real problems exist */}
        {healthSummary.incidentTone && servicesWithErrors.length > 0 && (
          <IncidentBanner
            tone={healthSummary.incidentTone}
            title={heroTitle}
            detail={servicesWithErrors[0]?.last_error ?? undefined}
          />
        )}

        {restartBanner && (
          <LiveApplyBanner onDismiss={() => setRestartBanner(false)} />
        )}

        {isError && !registry ? (
          <ErrorBanner
            title={`Couldn't load services. ${errorMessage(error)}`}
            onRetry={() => void refetch()}
          />
        ) : (
          <>
            <FilterToolbar
              search={
                <input
                  type="search"
                  className="vf-field-input w-full"
                  placeholder="Filter services…"
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  aria-label="Filter services"
                />
              }
            />
            {missingRouteService && (
              <IncidentBanner
                tone="warn"
                title="No service matches this link"
                detail={`The service target "${routeServiceTarget}" is not in the current service list.`}
              />
            )}

            {/* Two-column layout: service feed left, right context rail — vf-page-layout applies brand sizing */}
            <div className="vf-page-layout">
              <div className="vf-page-main">
                {isPending ? (
                  <div
                    className="vf-feed-panel flex flex-col gap-0"
                    aria-busy="true"
                  >
                    <Skeleton className="h-16 w-full rounded-none border-b border-[var(--vf-border)]" />
                    <Skeleton className="h-16 w-full rounded-none border-b border-[var(--vf-border)]" />
                    <Skeleton className="h-16 w-full rounded-none" />
                  </div>
                ) : filteredServices.length === 0 ? (
                  services.length === 0 ? (
                    <EmptyOperationalState
                      title="No services configured"
                      detail="Add an upstream backend to start routing traffic."
                      action={
                        <Button onClick={() => setAddOpen(true)}>
                          Add service
                        </Button>
                      }
                    />
                  ) : (
                    <EmptyOperationalState
                      title="No services match the filter"
                      action={
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => setSearchQuery("")}
                        >
                          Clear filter
                        </Button>
                      }
                    />
                  )
                ) : (
                  <div className="vf-feed-panel overflow-hidden">
                    {filteredServices.map((svc) => {
                      const backend = liveBackends.get(svc.id);
                      const { tone, label } = serviceLiveTone(
                        svc,
                        currentHealthByService.get(svc.id),
                      );
                      const metrics = serviceLiveMetrics(backend);
                      const isSelected = panelService?.id === svc.id;
                      return (
                        <ServiceTableRow
                          key={svc.id}
                          svc={svc}
                          tone={tone}
                          statusLabel={label}
                          metrics={metrics}
                          history={backend?.history}
                          isSelected={isSelected}
                          togglePending={pendingToggleServiceIds.has(svc.id)}
                          onSelect={() =>
                            setPanelService(isSelected ? null : svc)
                          }
                          onToggle={(checked) =>
                            toggleMutation.mutate({
                              serviceId: svc.id,
                              enabled: checked,
                            })
                          }
                          onEdit={() => setEditTarget(svc)}
                          onModels={() => setModelsTarget(svc)}
                          onTest={() => setTestTarget(svc)}
                          onDetect={() => setDetectTarget(svc)}
                          onRemove={() => setRemoveTarget(svc)}
                        />
                      );
                    })}
                  </div>
                )}
              </div>

              {isDesktopServiceContext ? (
                <div className="vf-right-rail flex">
                  {panelService ? (
                    <RightContextPanel
                      title={panelService.name}
                      subtitle={
                        isManagedSubstrate(panelService)
                          ? "managed substrate"
                          : endpointDisplay(panelService)
                      }
                      status={(() => {
                        const { tone, label } = serviceLiveTone(
                          panelService,
                          currentHealthByService.get(panelService.id),
                        );
                        return (
                          <StatusDotLabel
                            tone={tone}
                            label={label}
                            pulse={tone === "ok"}
                          />
                        );
                      })()}
                      sections={panelSections}
                    />
                  ) : (
                    <StatusSummaryRail
                      healthLabel={heroTitle}
                      healthTone={heroTone}
                      enabledCount={enabledCount}
                      totalCount={services.length}
                      healthSummary={healthSummary}
                      avgLatencyMs={avgLatencyMs}
                      modelMappingCount={modelMappingCount}
                      capabilityCoverage={capabilityCoverage}
                      avgUptime24h={avgUptime24h}
                      avgP95Latency24h={avgP95Latency24h}
                    />
                  )}
                </div>
              ) : (
                <Sheet
                  open={panelService !== null}
                  onOpenChange={(open) => {
                    if (!open) setPanelService(null);
                  }}
                >
                  <SheetContent>
                    {panelService && (
                      <>
                        <SheetHeader>
                          <SheetTitle>{panelService.name}</SheetTitle>
                          <SheetDescription>
                            Live service context and configuration details.
                          </SheetDescription>
                        </SheetHeader>
                        <div className="mt-4">
                          <RightContextPanel
                            title={panelService.name}
                            subtitle={
                              isManagedSubstrate(panelService)
                                ? "managed substrate"
                                : endpointDisplay(panelService)
                            }
                            status={(() => {
                              const { tone, label } = serviceLiveTone(
                                panelService,
                                currentHealthByService.get(panelService.id),
                              );
                              return (
                                <StatusDotLabel
                                  tone={tone}
                                  label={label}
                                  pulse={tone === "ok"}
                                />
                              );
                            })()}
                            sections={panelSections}
                            className="border-0 shadow-none"
                          />
                        </div>
                      </>
                    )}
                  </SheetContent>
                </Sheet>
              )}
            </div>
          </>
        )}
      </section>

      {/* Dialogs */}
      <AddServiceDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        onSaved={(newRegistry) => {
          updateRegistry(newRegistry);
          void queryClient.invalidateQueries({
            queryKey: queryKeys.serviceRegistry,
          });
          void queryClient.invalidateQueries({ queryKey: queryKeys.status });
          setAddOpen(false);
          showRestartBanner();
          toast.success("Service added");
        }}
      />

      <EditServiceDialog
        service={editTarget}
        onOpenChange={(open) => {
          if (!open) setEditTarget(null);
        }}
        onSaved={(newRegistry) => {
          updateRegistry(newRegistry);
          void queryClient.invalidateQueries({
            queryKey: queryKeys.serviceRegistry,
          });
          void queryClient.invalidateQueries({ queryKey: queryKeys.status });
          setEditTarget(null);
          showRestartBanner();
          toast.success("Service updated");
        }}
        onOpenModels={(svc) => {
          setEditTarget(null);
          setModelsTarget(svc);
        }}
      />

      <ModelsDialog
        service={modelsTarget}
        onOpenChange={(open) => {
          if (!open) setModelsTarget(null);
        }}
        onSaved={(newRegistry) => {
          updateRegistry(newRegistry);
          void queryClient.invalidateQueries({
            queryKey: queryKeys.serviceRegistry,
          });
          void queryClient.invalidateQueries({ queryKey: queryKeys.status });
          showRestartBanner();
        }}
      />

      <TestConnectionDialog
        service={testTarget}
        onOpenChange={(open) => {
          if (!open) setTestTarget(null);
        }}
      />

      <DetectModelsDialog
        service={detectTarget}
        onOpenChange={(open) => {
          if (!open) setDetectTarget(null);
        }}
        onOpenModels={(service) => {
          setDetectTarget(null);
          setModelsTarget(service);
        }}
      />

      {/* Remove confirm — typed-name; provenance-aware copy for config rows */}
      <ConfirmDialog
        open={removeTarget !== null}
        onOpenChange={(open) => {
          if (!open) setRemoveTarget(null);
        }}
        title="Remove service?"
        description={
          removeTarget ? (
            removeTarget.source === "provisioned" ? (
              <>
                <strong>{removeTarget.name}</strong> will be removed from the
                registry. Routes that depend on it stop working immediately when
                this live change is applied.{" "}
                <em>
                  Its provisioned seed is not re-imported on the next restart.
                </em>
              </>
            ) : (
              <>
                <strong>{removeTarget.name}</strong> will be removed from the
                registry. Routes that depend on it stop working immediately when
                this live change is applied.
              </>
            )
          ) : undefined
        }
        confirmLabel="Remove service"
        typedName={removeTarget?.name}
        onConfirm={async () => {
          if (removeTarget) await removeMutation.mutateAsync(removeTarget.id);
        }}
      />
    </div>
  );
}

function ProbeResultRow({ probe }: { probe: TestProbe }) {
  return (
    <div
      className={`rounded-vf-md flex items-center gap-2 border px-3 py-2 text-sm ${
        probe.ok
          ? "border-vf-ok/40 bg-vf-ok/10 text-vf-text"
          : "border-vf-bad/40 bg-vf-bad/10 text-vf-text"
      }`}
    >
      <span>{probe.ok ? "✓" : "✗"}</span>
      <span className="text-vf-small">
        {probe.ok
          ? `Connected (HTTP ${probe.status ?? "ok"}) — ${probe.url}`
          : probe.error || "Connection failed"}
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Edit service dialog
//
// Managed substrate rows (editable=false): show only name/enabled/note with an
// explanation that endpoint/protocol are managed by the gateway.
// All other rows: full-fidelity form with all config fields.

// ---------------------------------------------------------------------------
// Edit form state — richer than the Add form

interface EditFormState {
  // Connection
  name: string;
  base_url: string;
  api_key_env: string;
  protocol: string;
  health_endpoint: string;
  timeout: string;
  // Routing
  capabilities: string[];
  native_passthrough: boolean;
  // Admission (advanced)
  role: string;
  priority: string;
  gpu_required: boolean;
  max_parallel: string;
  vram_gb_estimate: string;
  duration_class: string;
  note: string;
  // Top-level
  enabled: boolean;
}

// Role values per registry semantics
const ROLE_OPTIONS = [
  {
    value: "required",
    label: "Required — expected to be available; a down service needs action",
  },
  {
    value: "optional",
    label: "Optional — a down service is a warning",
  },
  {
    value: "standby",
    label: "Standby — stopped or disabled is expected and neutral",
  },
];

const DURATION_CLASS_OPTIONS = [
  { value: "interactive", label: "Interactive (low latency)" },
  { value: "batch", label: "Batch (throughput)" },
];

/** Compose a base_url display string from host/port/base_path — inverse of split_base_url. */
function composeBaseUrl(config: Service["config"]): string {
  if (config.base_url) return config.base_url;
  if (config.host) {
    const scheme = config.port === 443 ? "https" : "http";
    const port = config.port ? `:${config.port}` : "";
    const path = config.base_path ?? "";
    return `${scheme}://${config.host}${port}${path}`;
  }
  return "";
}

/** Read the api_key env var name from wherever the record stores it — config or secret_refs. */
function extractApiKeyEnv(service: Service): string {
  // api_key_env is a secret reference key (_env suffix), so split_secret_refs
  // passes it through into the safe config dict (not secret_refs).
  if (service.config.api_key_env) return String(service.config.api_key_env);
  // Fallback: some older records might store it under secret_refs
  const refs = service.secret_refs ?? {};
  for (const key of Object.keys(refs)) {
    if (key.toLowerCase().includes("api_key")) return key;
  }
  return "";
}

function serviceToEditForm(service: Service): EditFormState {
  const cfg = service.config;
  const res = cfg.resources ?? {};
  return {
    name: service.name,
    base_url: composeBaseUrl(cfg),
    api_key_env: extractApiKeyEnv(service),
    protocol: cfg.protocol ?? "",
    health_endpoint: cfg.health_endpoint ?? "",
    timeout: cfg.timeout != null ? String(cfg.timeout) : "",
    capabilities: [...(service.capabilities ?? [])],
    native_passthrough: cfg.native_passthrough ?? false,
    role: service.role ?? "required",
    priority: service.priority != null ? String(service.priority) : "100",
    gpu_required: Boolean(res.gpu_required),
    max_parallel: res.max_parallel != null ? String(res.max_parallel) : "",
    vram_gb_estimate:
      res.vram_gb_estimate != null ? String(res.vram_gb_estimate) : "",
    duration_class: String(res.duration_class ?? "interactive"),
    note: cfg.note ?? "",
    enabled: service.enabled,
  };
}

// Keys the edit form explicitly models — any config key NOT in this set that
// is present in the stored record will be shown in the "Advanced — managed in
// config" read-only disclosure (H2) so operators can see everything the record
// holds.
const FORM_MODELED_CONFIG_KEYS = new Set([
  "base_url",
  "host",
  "port",
  "base_path",
  "protocol",
  "api_key_env",
  "health_endpoint",
  "timeout",
  "native_passthrough",
  "note",
  "resources",
  "models",
]);

/** Returns config keys in `service.config` that the form has no widget for.
 * These are shown read-only so H2 (show everything the record carries) holds. */
function unknownConfigKeys(service: Service): [string, unknown][] {
  return Object.entries(service.config).filter(
    ([key]) => !FORM_MODELED_CONFIG_KEYS.has(key),
  );
}

function editFormToApiBody(form: EditFormState): Record<string, unknown> {
  const body: Record<string, unknown> = {
    name: form.name.trim(),
    base_url: form.base_url.trim(),
    enabled: form.enabled,
    capabilities: form.capabilities,
    role: form.role,
  };
  if (form.api_key_env.trim()) body.api_key_env = form.api_key_env.trim();
  if (form.protocol) body.protocol = form.protocol;
  if (form.health_endpoint.trim())
    body.health_endpoint = form.health_endpoint.trim();
  if (form.timeout.trim()) body.timeout = parseInt(form.timeout, 10);
  if (form.priority.trim()) body.priority = parseInt(form.priority, 10);
  if (form.note.trim()) body.note = form.note.trim();

  body.native_passthrough = form.native_passthrough;

  // Build resources sub-object — only include what's non-empty
  const resources: Record<string, unknown> = {
    gpu_required: form.gpu_required,
  };
  if (form.max_parallel.trim())
    resources.max_parallel = parseInt(form.max_parallel, 10);
  if (form.vram_gb_estimate.trim())
    resources.vram_gb_estimate = parseFloat(form.vram_gb_estimate);
  if (form.duration_class) resources.duration_class = form.duration_class;
  body.resources = resources;

  return body;
}

function EditServiceDialog({
  service,
  onOpenChange,
  onSaved,
  onOpenModels,
}: {
  service: Service | null;
  onOpenChange: (open: boolean) => void;
  onSaved: (registry: ServiceRegistry) => void;
  onOpenModels: (service: Service) => void;
}) {
  return (
    <Dialog open={service !== null} onOpenChange={onOpenChange}>
      {service && (
        <EditServiceDialogContent
          service={service}
          onOpenChange={onOpenChange}
          onSaved={onSaved}
          onOpenModels={onOpenModels}
        />
      )}
    </Dialog>
  );
}

function EditServiceDialogContent({
  service,
  onOpenChange,
  onSaved,
  onOpenModels,
}: {
  service: Service;
  onOpenChange: (open: boolean) => void;
  onSaved: (registry: ServiceRegistry) => void;
  onOpenModels: (service: Service) => void;
}) {
  const managed = isManagedSubstrate(service);

  const {
    data: presetsData,
    error: presetsError,
    isPending: presetsLoading,
  } = useQuery({
    queryKey: queryKeys.servicePresets,
    queryFn: api.services.presets,
  });
  const capabilityChoices = presetsData?.capabilities ?? [];

  // Full edit form state (non-managed rows)
  const [form, setForm] = useState<EditFormState>(() =>
    serviceToEditForm(service),
  );
  // Managed-row only fields
  const [managedName, setManagedName] = useState(service.name);
  const [managedEnabled, setManagedEnabled] = useState(service.enabled);
  const [managedNote, setManagedNote] = useState(
    String(service.config.note ?? ""),
  );
  const [formError, setFormError] = useState<string | null>(null);
  const [probe, setProbe] = useState<TestProbe | null>(null);
  const [testing, setTesting] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [showUnknownKeys, setShowUnknownKeys] = useState(false);

  const unknownKeys = unknownConfigKeys(service);

  function set<K extends keyof EditFormState>(key: K) {
    return (
      e: React.ChangeEvent<
        HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement
      >,
    ) => {
      setForm((prev) => ({ ...prev, [key]: e.target.value }));
    };
  }

  function toggleCapability(cap: string) {
    setForm((prev) => ({
      ...prev,
      capabilities: prev.capabilities.includes(cap)
        ? prev.capabilities.filter((c) => c !== cap)
        : [...prev.capabilities, cap],
    }));
  }

  const save = useMutation({
    mutationFn: () => {
      if (managed) {
        return api.services.edit(service.id, {
          name: managedName.trim() || service.name,
          enabled: managedEnabled,
          note: managedNote,
        });
      }
      return api.services.edit(service.id, editFormToApiBody(form));
    },
    onSuccess: onSaved,
    onError: (err) => setFormError(errorMessage(err)),
  });

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setFormError(null);
    if (!managed && !form.name.trim()) {
      setFormError("Service name is required.");
      return;
    }
    save.mutate();
  }

  async function handleTest() {
    setTesting(true);
    setProbe(null);
    try {
      const result = await api.services.testConnection({
        base_url: form.base_url.trim(),
      });
      setProbe(result);
    } catch (err) {
      toast.error("Test failed", { description: errorMessage(err) });
    } finally {
      setTesting(false);
    }
  }

  return (
    <DialogContent>
      <DialogForm onSubmit={handleSubmit}>
        <DialogHeader>
          <DialogTitle>Edit {service.name}</DialogTitle>
          <DialogDescription>
            {managed
              ? "This is a managed service. Its endpoint and protocol are derived by the gateway and cannot be changed here."
              : "Changes apply live — no restart needed."}
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          {managed ? (
            <div className="flex flex-col gap-4">
              <Field label="Name">
                <Input
                  value={managedName}
                  onChange={(e) => setManagedName(e.target.value)}
                  disabled={save.isPending}
                  autoFocus
                />
              </Field>
              <div className="flex items-center justify-between gap-4">
                <span className="text-vf-small text-vf-text font-medium">
                  Enabled
                </span>
                <Switch
                  checked={managedEnabled}
                  onCheckedChange={setManagedEnabled}
                  disabled={save.isPending}
                  aria-label="Enable or disable this service"
                />
              </div>
              <Field label="Note">
                <textarea
                  value={managedNote}
                  onChange={(event) => setManagedNote(event.target.value)}
                  rows={3}
                  disabled={save.isPending}
                  placeholder="Optional operator note about this service."
                  className="border-vf-border bg-vf-surface focus:ring-vf-accent/30 text-vf-small text-vf-text placeholder:text-vf-subtle w-full resize-none rounded-md border px-3 py-2 focus:ring-2 focus:outline-none disabled:opacity-50"
                />
              </Field>
              <p className="text-vf-small text-vf-muted rounded-vf-md border-vf-border bg-vf-surface-2 border p-3">
                Endpoint, protocol, and execution substrate are managed by the
                gateway and cannot be edited here.
              </p>
              {formError && (
                <p role="alert" className="text-vf-small text-vf-bad">
                  {formError}
                </p>
              )}
            </div>
          ) : (
            <div className="flex flex-col gap-6">
              {/* ── Connection ──────────────────────────────── */}
              <div className="flex flex-col gap-4">
                <p className="text-vf-micro text-vf-muted font-semibold uppercase">
                  Connection
                </p>

                <Field label="Name" required>
                  <Input
                    value={form.name}
                    onChange={set("name")}
                    disabled={save.isPending}
                    autoFocus
                  />
                </Field>

                <Field
                  label="Base URL"
                  required
                  help="Full URL including scheme, host, port, and path."
                >
                  <Input
                    value={form.base_url}
                    onChange={set("base_url")}
                    placeholder="http://127.0.0.1:8000/v1"
                    disabled={save.isPending}
                  />
                </Field>

                <div className="flex flex-col gap-2">
                  <Button
                    type="button"
                    size="sm"
                    variant="secondary"
                    loading={testing}
                    disabled={!form.base_url}
                    onClick={() => void handleTest()}
                  >
                    Test connection
                  </Button>
                  {probe !== null && <ProbeResultRow probe={probe} />}
                </div>

                <Field
                  label="API key env var"
                  help="Name of an env variable on the gateway host — never the key itself."
                >
                  <Input
                    value={form.api_key_env}
                    onChange={set("api_key_env")}
                    placeholder="e.g. SERVICE_API_KEY"
                    disabled={save.isPending}
                  />
                </Field>

                <Field label="Protocol">
                  <Input
                    value={form.protocol}
                    onChange={set("protocol")}
                    placeholder="Declared service protocol"
                    disabled={save.isPending}
                  />
                </Field>

                <Field
                  label="Health endpoint"
                  help="Path probed to check reachability (e.g. /health or /v1/models)."
                >
                  <Input
                    value={form.health_endpoint}
                    onChange={set("health_endpoint")}
                    placeholder="/health"
                    disabled={save.isPending}
                  />
                </Field>

                <Field label="Timeout (seconds)">
                  <Input
                    type="number"
                    min={1}
                    value={form.timeout}
                    onChange={set("timeout")}
                    placeholder="120"
                    disabled={save.isPending}
                  />
                </Field>
              </div>

              {/* ── Routing ─────────────────────────────────── */}
              <div className="flex flex-col gap-4">
                <p className="text-vf-micro text-vf-muted font-semibold uppercase">
                  Routing
                </p>

                <fieldset className="flex flex-col gap-2">
                  <legend className="text-vf-small text-vf-text mb-1 font-medium">
                    Capabilities
                  </legend>
                  <div className="flex flex-wrap gap-x-4 gap-y-2">
                    {presetsLoading && (
                      <p className="text-vf-small text-vf-muted">
                        Loading capability choices…
                      </p>
                    )}
                    {presetsError && (
                      <p role="alert" className="text-vf-small text-vf-bad">
                        Capability choices are unavailable. Existing values will
                        be preserved.
                      </p>
                    )}
                    {!presetsLoading &&
                      !presetsError &&
                      capabilityChoices.length === 0 && (
                        <p className="text-vf-small text-vf-muted">
                          The gateway did not supply capability choices.
                        </p>
                      )}
                    {capabilityChoices.map((cap) => (
                      <label
                        key={cap.id}
                        className="flex cursor-pointer items-center gap-2"
                      >
                        <input
                          type="checkbox"
                          checked={form.capabilities.includes(cap.id)}
                          onChange={() => toggleCapability(cap.id)}
                          disabled={save.isPending}
                          className="accent-vf-accent h-4 w-4"
                        />
                        <span className="text-vf-small text-vf-text">
                          {cap.label}
                        </span>
                      </label>
                    ))}
                  </div>
                </fieldset>

                <div className="flex items-center justify-between gap-4">
                  <div className="flex flex-col gap-0.5">
                    <span className="text-vf-small text-vf-text font-medium">
                      Native passthrough
                    </span>
                    <span className="text-vf-micro text-vf-muted">
                      Send requests straight to this service when its policy
                      allows it.
                    </span>
                  </div>
                  <Switch
                    checked={form.native_passthrough}
                    onCheckedChange={(checked) =>
                      setForm((prev) => ({
                        ...prev,
                        native_passthrough: checked,
                      }))
                    }
                    disabled={save.isPending}
                    aria-label="Native passthrough"
                  />
                </div>

                <p className="text-vf-small text-vf-muted">
                  Model mappings are managed separately —{" "}
                  <button
                    type="button"
                    className="text-vf-accent underline-offset-2 hover:underline"
                    onClick={() => {
                      onOpenChange(false);
                      onOpenModels(service);
                    }}
                  >
                    open Models…
                  </button>
                </p>
              </div>

              {/* ── Unknown config keys (H2) ─────────────────── */}
              {unknownKeys.length > 0 && (
                <div className="flex flex-col gap-2">
                  <button
                    type="button"
                    className="text-vf-small text-vf-muted hover:text-vf-text flex items-center gap-1.5 transition-colors"
                    onClick={() => setShowUnknownKeys((v) => !v)}
                    aria-expanded={showUnknownKeys}
                  >
                    <span
                      className={`inline-block transition-transform ${showUnknownKeys ? "rotate-90" : ""}`}
                      aria-hidden
                    >
                      ▶
                    </span>
                    Advanced — managed in config ({unknownKeys.length})
                  </button>
                  {showUnknownKeys && (
                    <div className="rounded-vf-md border-vf-border bg-vf-surface-2 border p-3">
                      <p className="text-vf-micro text-vf-muted mb-2">
                        These keys are stored in this service&apos;s config but
                        have no widget in the form. They are preserved on every
                        save and are shown here for transparency.
                      </p>
                      <dl className="flex flex-col gap-1.5">
                        {unknownKeys.map(([key, val]) => (
                          <div
                            key={key}
                            className="flex flex-wrap items-start gap-x-3 gap-y-0.5"
                          >
                            <dt className="text-vf-micro text-vf-subtle shrink-0 font-mono">
                              {key}
                            </dt>
                            <dd className="text-vf-micro text-vf-text font-mono break-all">
                              {JSON.stringify(val)}
                            </dd>
                          </div>
                        ))}
                      </dl>
                    </div>
                  )}
                </div>
              )}

              {/* ── Enabled ─────────────────────────────────── */}
              <div className="flex items-center justify-between gap-4">
                <span className="text-vf-small text-vf-text font-medium">
                  Enabled
                </span>
                <Switch
                  checked={form.enabled}
                  onCheckedChange={(checked) =>
                    setForm((prev) => ({ ...prev, enabled: checked }))
                  }
                  disabled={save.isPending}
                  aria-label="Enable or disable this service"
                />
              </div>

              {/* ── Advanced (admission) ─────────────────────── */}
              <div className="flex flex-col gap-2">
                <button
                  type="button"
                  className="text-vf-small text-vf-muted hover:text-vf-text flex items-center gap-1.5 transition-colors"
                  onClick={() => setShowAdvanced((v) => !v)}
                  aria-expanded={showAdvanced}
                >
                  <span
                    className={`inline-block transition-transform ${showAdvanced ? "rotate-90" : ""}`}
                    aria-hidden
                  >
                    ▶
                  </span>
                  Advanced
                </button>

                {showAdvanced && (
                  <div className="flex flex-col gap-4 pt-2 pl-4">
                    <fieldset className="flex flex-col gap-2">
                      <legend className="text-vf-small text-vf-text mb-1 font-medium">
                        Role
                      </legend>
                      <div className="flex flex-col gap-2">
                        {ROLE_OPTIONS.map((opt) => (
                          <label
                            key={opt.value}
                            className="flex cursor-pointer items-start gap-2"
                          >
                            <input
                              type="radio"
                              name="role"
                              value={opt.value}
                              checked={form.role === opt.value}
                              onChange={set("role")}
                              disabled={save.isPending}
                              className="accent-vf-accent mt-0.5 h-4 w-4 shrink-0"
                            />
                            <span className="text-vf-small text-vf-text">
                              {opt.label}
                            </span>
                          </label>
                        ))}
                      </div>
                    </fieldset>

                    <Field
                      label="Priority"
                      help="Lower numbers are preferred (default 100)."
                    >
                      <Input
                        type="number"
                        value={form.priority}
                        onChange={set("priority")}
                        placeholder="100"
                        disabled={save.isPending}
                      />
                    </Field>

                    <div className="flex items-center justify-between gap-4">
                      <span className="text-vf-small text-vf-text font-medium">
                        GPU required
                      </span>
                      <Switch
                        checked={form.gpu_required}
                        onCheckedChange={(checked) =>
                          setForm((prev) => ({
                            ...prev,
                            gpu_required: checked,
                          }))
                        }
                        disabled={save.isPending}
                        aria-label="GPU required"
                      />
                    </div>

                    <Field
                      label="Max parallel"
                      help="Maximum concurrent requests to this backend."
                    >
                      <Input
                        type="number"
                        min={1}
                        value={form.max_parallel}
                        onChange={set("max_parallel")}
                        placeholder="4"
                        disabled={save.isPending}
                      />
                    </Field>

                    <Field label="VRAM estimate (GB)">
                      <Input
                        type="number"
                        min={0}
                        step={0.5}
                        value={form.vram_gb_estimate}
                        onChange={set("vram_gb_estimate")}
                        placeholder="0"
                        disabled={save.isPending}
                      />
                    </Field>

                    <Field label="Duration class">
                      <Select
                        value={form.duration_class}
                        onChange={set("duration_class")}
                        disabled={save.isPending}
                      >
                        {DURATION_CLASS_OPTIONS.map((opt) => (
                          <option key={opt.value} value={opt.value}>
                            {opt.label}
                          </option>
                        ))}
                      </Select>
                    </Field>

                    <Field label="Note">
                      <textarea
                        value={form.note}
                        onChange={set("note")}
                        rows={3}
                        disabled={save.isPending}
                        placeholder="Optional operator note about this service."
                        className="border-vf-border bg-vf-surface focus:ring-vf-accent/30 text-vf-small text-vf-text placeholder:text-vf-subtle w-full resize-none rounded-md border px-3 py-2 focus:ring-2 focus:outline-none disabled:opacity-50"
                      />
                    </Field>
                  </div>
                )}
              </div>

              {formError && (
                <p role="alert" className="text-vf-small text-vf-bad">
                  {formError}
                </p>
              )}
            </div>
          )}
        </DialogBody>
        <DialogFooter>
          <Button
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={save.isPending}
          >
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={save.isPending}>
            Save
          </Button>
        </DialogFooter>
      </DialogForm>
    </DialogContent>
  );
}

// ---------------------------------------------------------------------------
// Models editor dialog
//
// Lists per-service model mappings (public name → upstream name) with add /
// edit / remove. Saves via PATCH and shows restart banner.

function ModelsDialog({
  service,
  onOpenChange,
  onSaved,
}: {
  service: Service | null;
  onOpenChange: (open: boolean) => void;
  onSaved: (registry: ServiceRegistry) => void;
}) {
  return (
    <Dialog open={service !== null} onOpenChange={onOpenChange}>
      {service && (
        <ModelsDialogContent
          service={service}
          onOpenChange={onOpenChange}
          onSaved={onSaved}
        />
      )}
    </Dialog>
  );
}

interface ModelRow {
  model_group: string;
  upstream_model: string;
}

function ModelsDialogContent({
  service,
  onOpenChange,
  onSaved,
}: {
  service: Service;
  onOpenChange: (open: boolean) => void;
  onSaved: (registry: ServiceRegistry) => void;
}) {
  const [models, setModels] = useState<ModelRow[]>(() =>
    (service.config.models ?? []).map((m) => ({
      model_group: m.model_group,
      upstream_model: m.upstream_model,
    })),
  );
  const [editingIdx, setEditingIdx] = useState<number | null>(null);
  const [addingRow, setAddingRow] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: () =>
      api.services.edit(service.id, {
        models: models.map((m) => ({
          model_group: m.model_group,
          upstream_model: m.upstream_model,
        })),
      }),
    onSuccess: (newRegistry) => {
      onSaved(newRegistry);
      toast.success("Models updated");
      onOpenChange(false);
    },
    onError: (err) => setFormError(errorMessage(err)),
  });

  function removeModel(idx: number) {
    setModels((prev) => prev.filter((_, i) => i !== idx));
  }

  function updateModel(idx: number, updated: ModelRow) {
    setModels((prev) => prev.map((m, i) => (i === idx ? updated : m)));
    setEditingIdx(null);
  }

  function addModel(row: ModelRow) {
    setModels((prev) => [...prev, row]);
    setAddingRow(false);
  }

  return (
    <DialogContent>
      <DialogHeader>
        <DialogTitle>Models — {service.name}</DialogTitle>
        <DialogDescription>
          Model mappings make this backend&apos;s models routable. Each entry
          maps a public name (what callers use) to an upstream model name.
        </DialogDescription>
      </DialogHeader>
      <DialogBody>
        {models.length === 0 && !addingRow ? (
          <div className="flex flex-col items-center gap-3 py-4 text-center">
            <p className="text-vf-small text-vf-muted">
              No models mapped — this backend&apos;s models aren&apos;t routable
              until you add one.
            </p>
            <Button
              size="sm"
              variant="secondary"
              onClick={() => setAddingRow(true)}
            >
              Add model
            </Button>
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            {models.map((model, idx) =>
              editingIdx === idx ? (
                <ModelEditRow
                  key={idx}
                  initial={model}
                  onSave={(updated) => updateModel(idx, updated)}
                  onCancel={() => setEditingIdx(null)}
                />
              ) : (
                <ModelDisplayRow
                  key={idx}
                  model={model}
                  onEdit={() => setEditingIdx(idx)}
                  onRemove={() => removeModel(idx)}
                />
              ),
            )}

            {addingRow ? (
              <ModelEditRow
                initial={{ model_group: "", upstream_model: "" }}
                onSave={addModel}
                onCancel={() => setAddingRow(false)}
                isNew
              />
            ) : (
              <Button
                size="sm"
                variant="secondary"
                onClick={() => {
                  setEditingIdx(null);
                  setAddingRow(true);
                }}
                className="mt-1 self-start"
              >
                Add model
              </Button>
            )}
          </div>
        )}

        {formError && (
          <p role="alert" className="text-vf-small text-vf-bad mt-2">
            {formError}
          </p>
        )}
      </DialogBody>
      <DialogFooter>
        <Button
          variant="ghost"
          onClick={() => onOpenChange(false)}
          disabled={save.isPending}
        >
          Cancel
        </Button>
        <Button
          variant="primary"
          loading={save.isPending}
          onClick={() => save.mutate()}
        >
          Save
        </Button>
      </DialogFooter>
    </DialogContent>
  );
}

function ModelDisplayRow({
  model,
  onEdit,
  onRemove,
}: {
  model: ModelRow;
  onEdit: () => void;
  onRemove: () => void;
}) {
  return (
    <div className="border-vf-border rounded-vf-md flex items-center justify-between gap-3 border px-3 py-2">
      <div className="flex min-w-0 flex-col gap-0.5">
        <span className="text-vf-small text-vf-text truncate font-medium">
          {model.model_group}
        </span>
        <span className="text-vf-micro text-vf-subtle truncate font-mono">
          → {model.upstream_model}
        </span>
      </div>
      <div className="flex shrink-0 items-center gap-1">
        <Button size="sm" variant="ghost" onClick={onEdit}>
          Edit
        </Button>
        <Button size="sm" variant="ghost" onClick={onRemove}>
          Remove
        </Button>
      </div>
    </div>
  );
}

function ModelEditRow({
  initial,
  onSave,
  onCancel,
  isNew = false,
}: {
  initial: ModelRow;
  onSave: (row: ModelRow) => void;
  onCancel: () => void;
  isNew?: boolean;
}) {
  const [publicName, setPublicName] = useState(initial.model_group);
  const [upstreamName, setUpstreamName] = useState(initial.upstream_model);
  const [err, setErr] = useState<string | null>(null);

  function handleSave() {
    if (!publicName.trim()) {
      setErr("Public name is required.");
      return;
    }
    if (!upstreamName.trim()) {
      setErr("Upstream model name is required.");
      return;
    }
    onSave({
      model_group: publicName.trim(),
      upstream_model: upstreamName.trim(),
    });
  }

  return (
    <div className="border-vf-border rounded-vf-md flex flex-col gap-2 border p-3">
      <div className="flex gap-2">
        <Field label="Public name" className="flex-1">
          <Input
            value={publicName}
            onChange={(e) => setPublicName(e.target.value)}
            placeholder="e.g. public-model"
            autoFocus={isNew}
          />
        </Field>
        <Field label="Upstream model" className="flex-1">
          <Input
            value={upstreamName}
            onChange={(e) => setUpstreamName(e.target.value)}
            placeholder="e.g. upstream-model"
          />
        </Field>
      </div>
      {err && (
        <p role="alert" className="text-vf-small text-vf-bad">
          {err}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <Button size="sm" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
        <Button size="sm" variant="primary" onClick={handleSave}>
          {isNew ? "Add" : "Update"}
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Test connection dialog (launched from overflow menu for existing services)

function TestConnectionDialog({
  service,
  onOpenChange,
}: {
  service: Service | null;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Dialog open={service !== null} onOpenChange={onOpenChange}>
      {service && (
        <TestConnectionDialogContent
          service={service}
          onOpenChange={onOpenChange}
        />
      )}
    </Dialog>
  );
}

function TestConnectionDialogContent({
  service,
  onOpenChange,
}: {
  service: Service;
  onOpenChange: (open: boolean) => void;
}) {
  const [probe, setProbe] = useState<TestProbe | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const endpoint = service.config.base_url
    ? { base_url: service.config.base_url }
    : { host: service.config.host, port: service.config.port };

  async function run() {
    setLoading(true);
    setErr(null);
    try {
      const result = await api.services.testConnection(endpoint);
      setProbe(result);
    } catch (e) {
      setErr(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }

  return (
    <DialogContent className="max-w-md">
      <DialogHeader>
        <DialogTitle>Test connection — {service.name}</DialogTitle>
        <DialogDescription>
          Sends a lightweight probe to the upstream to check reachability. A
          successful probe does not prove that a model is routable.
        </DialogDescription>
      </DialogHeader>
      <DialogBody>
        {probe && <ProbeResultRow probe={probe} />}
        {err && (
          <p role="alert" className="text-vf-small text-vf-bad">
            {err}
          </p>
        )}
        {!probe && !err && (
          <p className="text-vf-small text-vf-muted">
            Probing:{" "}
            <span className="font-mono">
              {service.config.base_url ?? endpointDisplay(service)}
            </span>
          </p>
        )}
      </DialogBody>
      <DialogFooter>
        <Button variant="ghost" onClick={() => onOpenChange(false)}>
          Close
        </Button>
        <Button variant="primary" loading={loading} onClick={() => void run()}>
          {probe ? "Re-test" : "Test now"}
        </Button>
      </DialogFooter>
    </DialogContent>
  );
}

// ---------------------------------------------------------------------------
// Detect models dialog — reports upstream model names; routing is configured
// explicitly in the Models editor.

function DetectModelsDialog({
  service,
  onOpenChange,
  onOpenModels,
}: {
  service: Service | null;
  onOpenChange: (open: boolean) => void;
  onOpenModels: (service: Service) => void;
}) {
  return (
    <Dialog open={service !== null} onOpenChange={onOpenChange}>
      {service && (
        <DetectModelsDialogContent
          service={service}
          onOpenChange={onOpenChange}
          onOpenModels={onOpenModels}
        />
      )}
    </Dialog>
  );
}

function DetectModelsDialogContent({
  service,
  onOpenChange,
  onOpenModels,
}: {
  service: Service;
  onOpenChange: (open: boolean) => void;
  onOpenModels: (service: Service) => void;
}) {
  const [result, setResult] = useState<{
    ok: boolean;
    models: string[];
    error: string;
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const endpoint = service.config.base_url
    ? { base_url: service.config.base_url }
    : { host: service.config.host, port: service.config.port };

  async function run() {
    setLoading(true);
    setErr(null);
    try {
      const detected = await api.services.detectModels(endpoint);
      setResult(detected);
    } catch (e) {
      setErr(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }

  return (
    <DialogContent className="max-w-md">
      <DialogHeader>
        <DialogTitle>Detect models — {service.name}</DialogTitle>
        <DialogDescription>
          Queries the upstream for observed model names. Detection does not
          create routable mappings.
        </DialogDescription>
      </DialogHeader>
      <DialogBody>
        {result && (
          <div className="flex flex-col gap-2">
            {result.ok ? (
              result.models.length === 0 ? (
                <p className="text-vf-small text-vf-muted">
                  No models reported.
                </p>
              ) : (
                <ul className="divide-vf-border flex flex-col divide-y">
                  {result.models.map((model) => (
                    <li
                      key={model}
                      className="text-vf-small text-vf-text flex items-center justify-between py-2"
                    >
                      <span className="font-mono">{model}</span>
                      <CopyButton
                        value={model}
                        aria-label={`Copy model name ${model}`}
                      />
                    </li>
                  ))}
                </ul>
              )
            ) : (
              <p role="alert" className="text-vf-small text-vf-bad">
                {result.error || "Could not detect models from this service."}
              </p>
            )}
          </div>
        )}
        {err && (
          <p role="alert" className="text-vf-small text-vf-bad">
            {err}
          </p>
        )}
        {!result && !err && (
          <p className="text-vf-small text-vf-muted">
            Will query:{" "}
            <span className="font-mono">
              {service.config.base_url ?? endpointDisplay(service)}
            </span>
          </p>
        )}
      </DialogBody>
      <DialogFooter>
        <Button variant="ghost" onClick={() => onOpenChange(false)}>
          Close
        </Button>
        <Button variant="primary" loading={loading} onClick={() => void run()}>
          {result ? "Re-detect" : "Detect models"}
        </Button>
        {result?.ok && (
          <Button
            variant="secondary"
            onClick={() => {
              onOpenChange(false);
              onOpenModels(service);
            }}
          >
            Configure mappings
          </Button>
        )}
      </DialogFooter>
    </DialogContent>
  );
}
