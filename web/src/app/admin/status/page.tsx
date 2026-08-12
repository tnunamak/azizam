"use client";

/** Admin → Status — read-first diagnosis. Configuration remains Services. */

import Link from "next/link";
import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";

import {
  api,
  queryKeys,
  type BackendHistory,
  type CurrentHealth,
  type CurrentHealthService,
  type StatusPayload,
  type StatusSeriesPoint,
} from "@/lib/api";
import { adminServiceHref, adminStatusHref } from "@/lib/admin-links";
import { deriveServicesHealthSummary } from "@/lib/services-health-summary";
import {
  Badge,
  Button,
  EmptyOperationalState,
  IncidentBanner,
  RelativeTime,
  RightContextPanel,
  type RightContextPanelSection,
  SectionActionHeader,
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  Skeleton,
  StatusDotLabel,
  SystemHealthBand,
  TrendBarChart,
  TrendLineChart,
  type ChartPoint,
  type HealthTone,
} from "@/components/ui";

type LiveBackend = NonNullable<StatusPayload["backends"]>[number];

function healthTone(value: string): HealthTone {
  return value === "ok" || value === "warn" || value === "bad"
    ? value
    : "neutral";
}

function observationLabel(service: CurrentHealthService): string {
  switch (service.observation) {
    case "standby_stopped":
      return "standby, stopped";
    case "unavailable":
      return "live status unavailable";
    default:
      return service.observation.replace(/_/g, " ");
  }
}

function historyPoints(series: StatusSeriesPoint[] | undefined): ChartPoint[] {
  return (series ?? [])
    .filter(
      (point) =>
        Array.isArray(point) &&
        Number.isFinite(point[0]) &&
        Number.isFinite(point[1]),
    )
    .map(([x, y]) => ({
      x,
      y,
      label: new Date(x * 1000).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      }),
    }));
}

function latencyLabel(value: number | null | undefined): string | null {
  return typeof value === "number" && Number.isFinite(value)
    ? `${Math.round(value)} ms`
    : null;
}

function endpointLabel(backend: LiveBackend | undefined): string | null {
  if (!backend?.url) return null;
  try {
    return new URL(backend.url).host;
  } catch {
    return backend.url;
  }
}

function advisoryTone(value: string): "warn" | "bad" {
  return healthTone(value) === "bad" ? "bad" : "warn";
}

/** Current advisories retain identity as a target tuple, never a display name. */
function currentAdvisories(health: CurrentHealth) {
  const serviceAdvisories = health.services
    .filter((service) => service.tone === "warn" || service.tone === "bad")
    .map((service) => ({
      target_kind: "service" as const,
      target_id: service.service_id,
      title: service.name,
      detail:
        service.last_error ??
        `${observationLabel(service)}${service.role ? ` · ${service.role}` : ""}`,
      tone: advisoryTone(service.tone),
    }));
  const signalAdvisories = (health.signals ?? []).map((signal) => ({
    target_kind: null,
    target_id: null,
    title: signal.source,
    detail: signal.reasons.join(" "),
    tone: advisoryTone(signal.tone),
  }));
  return [...serviceAdvisories, ...signalAdvisories];
}

function HistoryTreatment({
  history,
  latencyMs,
}: {
  history: BackendHistory | undefined;
  latencyMs: number | null | undefined;
}) {
  const latency = historyPoints(history?.latency_series);
  const status = historyPoints(history?.status_series);
  const scalarLatency = latencyLabel(latencyMs);
  const hasHistory = latency.length >= 3 || status.length >= 3;

  if (!history || !hasHistory) {
    return (
      <div className="flex flex-col gap-1">
        {scalarLatency && (
          <p className="text-vf-small text-vf-text">Latency: {scalarLatency}</p>
        )}
        <p className="text-vf-micro text-vf-subtle">No history available</p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3" aria-label="Observed history">
      <div className="text-vf-micro text-vf-subtle flex flex-wrap gap-x-4 gap-y-1">
        {history.uptime_pct_24h != null && (
          <span>24h availability: {history.uptime_pct_24h.toFixed(1)}%</span>
        )}
        {history.p95_latency_ms_24h != null && (
          <span>24h p95: {Math.round(history.p95_latency_ms_24h)} ms</span>
        )}
        {scalarLatency && <span>Current latency: {scalarLatency}</span>}
      </div>
      {latency.length >= 3 && (
        <div>
          <p className="text-vf-micro text-vf-subtle mb-1">
            Latency observations
          </p>
          <TrendLineChart
            series={latency}
            tone="info"
            height={72}
            accessibility={{
              mode: "summary",
              label: "Latency observations",
              summary: `${latency.length} measured latency observations.`,
            }}
          />
        </div>
      )}
      {status.length >= 3 && (
        <div>
          <p className="text-vf-micro text-vf-subtle mb-1">
            Availability observations
          </p>
          <TrendBarChart
            series={status}
            tone="ok"
            height={72}
            accessibility={{
              mode: "summary",
              label: "Availability observations",
              summary: `${status.length} observed availability samples.`,
            }}
          />
        </div>
      )}
    </div>
  );
}

function ServiceDetail({
  service,
  backend,
}: {
  service: CurrentHealthService;
  backend: LiveBackend | undefined;
}) {
  const sections = useMemo((): RightContextPanelSection[] => {
    const endpoint = endpointLabel(backend);
    const error = service.last_error ?? backend?.last_error;
    return [
      {
        title: "Current observation",
        content: (
          <div className="flex flex-col gap-2">
            <StatusDotLabel
              tone={healthTone(service.tone)}
              label={observationLabel(service)}
            />
            <div className="flex flex-wrap gap-1">
              <Badge sm tone="neutral">
                {service.enabled ? "enabled" : "disabled"}
              </Badge>
              {service.role && (
                <Badge sm tone="neutral">
                  {service.role}
                </Badge>
              )}
              {service.routable !== null && (
                <Badge sm tone={service.routable ? "ok" : "warn"}>
                  {service.routable ? "routable" : "not routable"}
                </Badge>
              )}
            </div>
            {endpoint && (
              <p className="text-vf-micro text-vf-subtle font-mono break-all">
                {endpoint}
              </p>
            )}
            {error && (
              <p className="text-vf-micro text-vf-bad font-mono break-all">
                {error}
              </p>
            )}
          </div>
        ),
      },
      {
        title: "History",
        content: (
          <HistoryTreatment
            history={backend?.history}
            latencyMs={backend?.latency_ms}
          />
        ),
      },
    ];
  }, [backend, service]);

  return (
    <RightContextPanel
      title={service.name}
      subtitle={
        service.configured ? "Service diagnosis" : "Unregistered service"
      }
      status={<StatusDotLabel tone={healthTone(service.tone)} label="" />}
      actions={
        <Link
          href={adminServiceHref(service.service_id)}
          className="vf-button vf-button-secondary vf-button-sm vf-touch-target"
        >
          Open Services configuration
        </Link>
      }
      sections={sections}
    />
  );
}

function StatusContents() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const selectedServiceId = searchParams.get("service");
  const lastTrigger = useRef<HTMLElement | null>(null);
  const [sheetOpen, setSheetOpen] = useState(false);
  const { data, error, isPending, isFetching, refetch } = useQuery({
    queryKey: queryKeys.status,
    queryFn: api.status,
  });
  const health = data?.current_health;

  const backends = useMemo(
    () =>
      new Map(
        (data?.backends ?? []).flatMap((backend) =>
          backend.service_id ? [[backend.service_id, backend] as const] : [],
        ),
      ),
    [data?.backends],
  );
  const selectedService = health?.services.find(
    (service) => service.service_id === selectedServiceId,
  );
  const advisories = health ? currentAdvisories(health) : [];

  useEffect(() => {
    const compactViewport =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(max-width: 979px)").matches;
    if (!selectedService || !compactViewport) return;
    setSheetOpen(true);
  }, [selectedService]);

  const closeDetail = () => {
    setSheetOpen(false);
    router.replace(adminStatusHref());
  };

  if (isPending && !data) {
    return <StatusLoading />;
  }

  if (!health) {
    return (
      <div className="flex flex-col gap-6">
        <SectionActionHeader
          as="h1"
          title="System Status"
          description="Current gateway health and the next object to inspect."
        />
        <IncidentBanner
          tone="bad"
          title="Could not load status"
          detail="The gateway did not return current health."
          actions={
            <Button size="sm" onClick={() => void refetch()}>
              Retry
            </Button>
          }
        />
      </div>
    );
  }

  const enabledCount = health.services.filter(
    (service) => service.enabled,
  ).length;
  const summary = deriveServicesHealthSummary(
    health,
    enabledCount,
    health.services.length,
  );
  const observedCount = health.services.filter(
    (service) => service.observation !== "unavailable",
  ).length;
  const staleFacts = Boolean(error && data);

  return (
    <div className="flex max-w-[1080px] flex-col gap-6">
      <SectionActionHeader
        as="h1"
        title="System Status"
        description="Current gateway health and the next object to inspect."
        action={
          <Button
            variant="ghost"
            size="sm"
            disabled={isFetching}
            onClick={() => void refetch()}
          >
            {isFetching ? "Refreshing…" : "Refresh"}
          </Button>
        }
      />
      {staleFacts && (
        <IncidentBanner
          tone="warn"
          title="Showing last known status"
          detail="A refresh failed. Known observations remain visible."
          actions={
            <Button size="sm" onClick={() => void refetch()}>
              Retry
            </Button>
          }
        />
      )}
      <SystemHealthBand
        tone={healthTone(health.tone)}
        title={health.label}
        detail={health.detail}
        metrics={[
          {
            label: "Observed",
            value: health.observation_available
              ? `${observedCount} of ${health.services.length}`
              : "Unavailable",
            detail: health.observation_available
              ? "configured services"
              : "live probes did not report",
            tone: health.observation_available ? "neutral" : "warn",
          },
          {
            label: "Needs attention",
            value: `${summary.badCount + summary.warnCount}`,
            detail: summary.badCount
              ? "required service failure"
              : summary.warnCount
                ? "degraded or optional service"
                : "no current advisory",
            tone: summary.metricTone,
          },
          {
            label: "Generated",
            value: data?.generated_at ? "Current" : "Not supplied",
            detail: data?.generated_at
              ? undefined
              : "observation time unavailable",
            tone: data?.generated_at ? "neutral" : "neutral",
          },
        ]}
        action={
          data?.generated_at ? (
            <p className="text-vf-micro text-vf-subtle">
              Observed <RelativeTime ts={data.generated_at} />
            </p>
          ) : undefined
        }
      />

      <section className="flex flex-col gap-3" aria-label="Current advisories">
        <SectionActionHeader title="Current advisories" />
        {advisories.length === 0 ? (
          <EmptyOperationalState
            title="No current advisories"
            detail="No observed issue requires a separate action."
          />
        ) : (
          advisories.map((advisory) => (
            <IncidentBanner
              key={advisory.target_id ?? `${advisory.title}-${advisory.detail}`}
              tone={advisory.tone}
              title={advisory.title}
              detail={advisory.detail}
              actions={
                advisory.target_kind === "service" && advisory.target_id ? (
                  <div className="flex flex-wrap gap-2">
                    <Link
                      className="vf-button vf-button-ghost vf-button-sm vf-touch-target"
                      href={adminStatusHref(advisory.target_id)}
                    >
                      Inspect
                    </Link>
                    <Link
                      className="vf-button vf-button-secondary vf-button-sm vf-touch-target"
                      href={adminServiceHref(advisory.target_id)}
                    >
                      Services
                    </Link>
                  </div>
                ) : undefined
              }
            />
          ))
        )}
      </section>

      <div className="grid min-w-0 gap-6 min-[980px]:grid-cols-[minmax(0,1fr)_20rem]">
        <section className="min-w-0" aria-label="Service health">
          <SectionActionHeader title="Service health" />
          {health.services.length === 0 ? (
            <div className="mt-3">
              <EmptyOperationalState
                title="No services configured"
                detail="Add a service in Services to begin routing gateway requests."
                action={
                  <Link
                    href="/admin/services"
                    className="vf-button vf-button-primary vf-touch-target"
                  >
                    Open Services
                  </Link>
                }
              />
            </div>
          ) : (
            <div className="vf-feed-panel mt-3 overflow-hidden">
              {health.services.map((service) => {
                const backend = backends.get(service.service_id);
                const selected = service.service_id === selectedServiceId;
                const latency = latencyLabel(backend?.latency_ms);
                const errorText = service.last_error ?? backend?.last_error;
                return (
                  <button
                    key={service.service_id}
                    type="button"
                    aria-pressed={selected}
                    className={`vf-service-row w-full text-left ${selected ? "bg-vf-row-hover" : ""}`}
                    onClick={(event) => {
                      lastTrigger.current = event.currentTarget;
                      router.push(adminStatusHref(service.service_id));
                    }}
                  >
                    <span className="vf-service-row-main">
                      <span className="min-w-0 flex-1">
                        <span className="text-vf-body text-vf-text block truncate font-semibold">
                          {service.name}
                        </span>
                        <span className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1">
                          <StatusDotLabel
                            tone={healthTone(service.tone)}
                            label={observationLabel(service)}
                          />
                          <span className="text-vf-micro text-vf-subtle">
                            {service.enabled ? service.role : "disabled"}
                          </span>
                          {latency && (
                            <span className="text-vf-micro text-vf-subtle">
                              {latency}
                            </span>
                          )}
                          {service.routable === null && (
                            <span className="text-vf-micro text-vf-warn">
                              route coverage unknown
                            </span>
                          )}
                        </span>
                      </span>
                      <span className="text-vf-micro text-vf-accent shrink-0">
                        Inspect
                      </span>
                    </span>
                    {errorText && (
                      <span className="text-vf-micro text-vf-bad mt-1 block font-mono break-all">
                        {errorText}
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
          )}
        </section>
        <aside
          className="flex min-w-0 flex-col gap-3"
          aria-label="History and durable incidents"
        >
          <SectionActionHeader title="History and incidents" />
          {selectedService ? (
            <div className="min-[980px]:hidden">
              <ServiceDetail
                service={selectedService}
                backend={backends.get(selectedService.service_id)}
              />
            </div>
          ) : (
            <EmptyOperationalState
              title="Select a service"
              detail="Open a service to inspect its observed history."
            />
          )}
          <p className="text-vf-micro text-vf-subtle">
            No durable incident history is available from the current status
            contract.
          </p>
        </aside>
        <div className="hidden min-[980px]:block">
          {selectedService ? (
            <ServiceDetail
              service={selectedService}
              backend={backends.get(selectedService.service_id)}
            />
          ) : (
            <EmptyOperationalState
              title="Select a service"
              detail="Open a service to inspect its observed state and history."
            />
          )}
        </div>
      </div>

      <Sheet
        open={sheetOpen}
        onOpenChange={(open) => {
          setSheetOpen(open);
          if (!open) closeDetail();
        }}
      >
        <SheetContent
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            lastTrigger.current?.focus();
          }}
        >
          {selectedService && (
            <>
              <SheetHeader>
                <SheetTitle>{selectedService.name}</SheetTitle>
                <SheetDescription>Current service diagnosis</SheetDescription>
              </SheetHeader>
              <ServiceDetail
                service={selectedService}
                backend={backends.get(selectedService.service_id)}
              />
            </>
          )}
        </SheetContent>
      </Sheet>
    </div>
  );
}

function StatusLoading() {
  return (
    <div className="flex flex-col gap-6" aria-busy="true">
      <SectionActionHeader
        as="h1"
        title="System Status"
        description="Checking current gateway health."
      />
      <Skeleton className="h-32 w-full" />
      <Skeleton className="h-20 w-full" />
      <Skeleton className="h-56 w-full" />
    </div>
  );
}

export default function AdminStatusPage() {
  return (
    <Suspense fallback={<StatusLoading />}>
      <StatusContents />
    </Suspense>
  );
}
