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
      <SectionActionHeader
        title="Status"
        description="Current health and the next object to inspect."
        action={
          selectedServiceId ? (
            <Link
              href={adminStatusHref()}
              className="text-vf-small text-vf-muted hover:text-vf-text"
            >
              Clear selection
            </Link>
          ) : undefined
        }
      />

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
                      {service.routable === false && (
                        <span className="text-vf-micro text-vf-warn">
                          not routable
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
