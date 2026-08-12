"use client";

/** Admin → Status — read-first diagnosis. Configuration remains Services. */

import Link from "next/link";
import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";

import { api, queryKeys, type CurrentHealthService } from "@/lib/api";
import { adminServiceHref, adminStatusHref } from "@/lib/admin-links";
import {
  Button,
  EmptyOperationalState,
  IncidentBanner,
  MetricStrip,
  ObjectList,
  ObjectRow,
  SectionActionHeader,
  Skeleton,
  StatusDotLabel,
  type HealthTone,
} from "@/components/ui";

function toneFor(service: CurrentHealthService): HealthTone {
  if (
    service.tone === "ok" ||
    service.tone === "warn" ||
    service.tone === "bad"
  ) {
    return service.tone;
  }
  return "neutral";
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

function formatServiceCount(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

/** Facts derived only from the P0 current-health projection.  Keeping this
 * local makes the read-first Status surface explicit without inventing a
 * second health model or chart data. */
function statusMetrics(
  health: NonNullable<Awaited<ReturnType<typeof api.status>>["current_health"]>,
) {
  const services = health.services;
  const observed = services.filter(
    (service) => service.observation !== "unavailable",
  ).length;
  const routable = services.filter(
    (service) => service.routable === true,
  ).length;
  const attention = services.filter(
    (service) => service.tone === "warn" || service.tone === "bad",
  ).length;

  return [
    {
      label: "Observed",
      value: health.observation_available
        ? formatServiceCount(observed, "service")
        : "Unavailable",
      detail: health.observation_available
        ? `of ${formatServiceCount(services.length, "configured service")}`
        : "Live probes did not report",
      tone: health.observation_available ? "neutral" : "warn",
    },
    {
      label: "Routable",
      value: formatServiceCount(routable, "service"),
      detail: services.some((service) => service.routable === null)
        ? "Some route coverage is unknown"
        : "Current route declaration",
      tone: services.some((service) => service.routable === null)
        ? "warn"
        : "neutral",
    },
    {
      label: "Needs attention",
      value: formatServiceCount(attention, "service"),
      detail: attention > 0 ? "Degraded or down" : "No service advisories",
      tone: attention > 0 ? "warn" : "ok",
    },
  ] as const;
}

function StatusContents() {
  const searchParams = useSearchParams();
  const selectedServiceId = searchParams.get("service");
  const { data, error, isPending, refetch } = useQuery({
    queryKey: queryKeys.status,
    queryFn: api.status,
  });
  const health = data?.current_health;

  if (isPending) {
    return (
      <div className="flex flex-col gap-4">
        <SectionActionHeader
          title="Status"
          description="Checking current gateway health."
        />
        <Skeleton className="h-28 w-full" />
        <Skeleton className="h-12 w-full" />
      </div>
    );
  }

  if (error || !health) {
    return (
      <div className="flex flex-col gap-4">
        <SectionActionHeader
          title="Status"
          description="Current health could not be loaded."
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

  const selectedService = health.services.find(
    (service) => service.service_id === selectedServiceId,
  );

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-end justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-0.5">
          <h1 className="text-vf-title text-vf-text font-semibold">Status</h1>
          <p className="text-vf-small text-vf-muted">
            Current health and the next object to inspect.
          </p>
        </div>
        {selectedServiceId && (
          <Link
            href={adminStatusHref()}
            className="text-vf-small text-vf-muted hover:text-vf-text"
          >
            Clear selection
          </Link>
        )}
      </div>

      <IncidentBanner
        tone={
          health.state === "bad"
            ? "bad"
            : health.state === "warn"
              ? "warn"
              : "info"
        }
        title={health.label}
        detail={health.detail}
      />

      <MetricStrip items={[...statusMetrics(health)]} />

      <section className="flex flex-col gap-3">
        <SectionActionHeader title="Current services" />
        {health.services.length === 0 ? (
          <EmptyOperationalState
            title="No services configured"
            detail="Add a service in Services to begin routing gateway requests."
          />
        ) : (
          <ObjectList>
            {health.services.map((service) => {
              const selected = selectedServiceId === service.service_id;
              return (
                <ObjectRow
                  key={service.service_id}
                  href={adminStatusHref(service.service_id)}
                  title={service.name}
                  meta={
                    <span className="flex flex-wrap items-center gap-2">
                      <StatusDotLabel
                        tone={toneFor(service)}
                        label={observationLabel(service)}
                      />
                      <span className="text-vf-micro text-vf-subtle">
                        {!service.configured
                          ? "unregistered"
                          : service.enabled
                            ? service.role
                            : "disabled"}
                      </span>
                      {service.routable === null &&
                        service.observation === "up" && (
                          <span className="text-vf-micro text-vf-warn">
                            route coverage unknown
                          </span>
                        )}
                      {service.last_error && (
                        <span className="text-vf-micro text-vf-muted">
                          {service.last_error}
                        </span>
                      )}
                    </span>
                  }
                  side={selected ? "Viewing" : undefined}
                  className={selected ? "bg-vf-surface-2" : undefined}
                />
              );
            })}
          </ObjectList>
        )}
      </section>
      {selectedService?.configured && (
        <Link
          href={adminServiceHref(selectedService.service_id)}
          className="text-vf-small text-vf-accent hover:text-vf-text"
        >
          Configure selected service
        </Link>
      )}
    </div>
  );
}

export default function AdminStatusPage() {
  return (
    <Suspense fallback={<Skeleton className="h-28 w-full" />}>
      <StatusContents />
    </Suspense>
  );
}
