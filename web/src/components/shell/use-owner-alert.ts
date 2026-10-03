"use client";

import { useQuery } from "@tanstack/react-query";
import { api, type CurrentHealth } from "@/lib/api";
import {
  useOptionalProviderStatus,
  type ProviderStatusView,
} from "@/lib/provider-status";

/** Only a current, explicit engine outage warrants an owner-wide alert. */
export function ownerEngineAlert(health?: CurrentHealth): string | null {
  if (!health?.observation_available) return null;
  const failed = health.services.find(
    (service) =>
      service.configured && service.enabled && service.observation === "down",
  );
  return failed
    ? `${failed.name} is unavailable. Check its connection on Status.`
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
    : `${failed.name} is unreachable. Check its connection on Status.`;
}

export function useOwnerAlert(enabled: boolean) {
  const providers = useOptionalProviderStatus(enabled);
  const { data, isError } = useQuery({
    queryKey: ["foundation", "health"],
    queryFn: api.status,
    enabled,
    refetchInterval: 30_000,
  });
  if (!enabled) return null;
  const engineAlert = !isError ? ownerEngineAlert(data?.current_health) : null;
  return (
    engineAlert ??
    (providers.state === "ready"
      ? ownerProviderAlert(providers.providers)
      : null)
  );
}
