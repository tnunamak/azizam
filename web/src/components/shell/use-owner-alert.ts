"use client";

import { engineShortName } from "@/lib/health-presentation";
import { useQuery } from "@tanstack/react-query";
import {
  api,
  queryKeys,
  type ServiceRegistry,
  type CurrentHealth,
} from "@/lib/api";
import {
  useOptionalProviderStatus,
  type ProviderStatusView,
} from "@/lib/provider-status";

/** Only a current, explicit engine outage warrants an owner-wide alert. */
export function ownerEngineAlert(
  health?: CurrentHealth,
  registry?: ServiceRegistry,
): string | null {
  if (!health?.observation_available) return null;
  const failed = health.services.find(
    (service) =>
      service.configured &&
      service.enabled &&
      service.in_use === true &&
      service.observation === "down",
  );
  return failed
    ? `${engineShortName(registry?.services[failed.service_id], failed)} is down. Check its connection on Status.`
    : null;
}

export function ownerProviderAlert(
  providers: ProviderStatusView[],
): string | null {
  const failed = providers.find(
    (provider) =>
      provider.enabled === true &&
      (provider.reachable === false || provider.keyState === "missing"),
  );
  if (!failed) return null;
  return failed.keyState === "missing"
    ? `${failed.name} is missing its provider key. Check provider settings on Status.`
    : `${failed.name} is down. Check its connection on Status.`;
}

export function useOwnerAlertTarget(
  enabled: boolean,
): { message: string; section: "Engines" | "Providers" } | null {
  const providers = useOptionalProviderStatus(enabled);
  const { data: registry } = useQuery({
    queryKey: queryKeys.serviceRegistry,
    queryFn: api.services.registry,
    enabled,
  });
  const { data, isError } = useQuery({
    queryKey: ["foundation", "health"],
    queryFn: api.status,
    enabled,
    refetchInterval: 30_000,
  });
  if (!enabled) return null;
  const engineAlert = !isError
    ? ownerEngineAlert(data?.current_health, registry)
    : null;
  if (engineAlert) return { message: engineAlert, section: "Engines" };
  const providerAlert =
    providers.state === "ready"
      ? ownerProviderAlert(providers.providers)
      : null;
  return providerAlert
    ? { message: providerAlert, section: "Providers" }
    : null;
}

export function useOwnerAlert(enabled: boolean) {
  return useOwnerAlertTarget(enabled)?.message ?? null;
}
