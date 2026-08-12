import type { CurrentHealth } from "./api";

export interface ServicesHealthSummary {
  badCount: number;
  warnCount: number;
  incompleteCount: number;
  metricTone: "ok" | "warn" | "bad" | "neutral";
  metricLabel: string;
  incidentTone: "warn" | "bad" | null;
}

/**
 * Derive Services presentation from the server's current-health projection.
 * Row tone is authoritative: it already accounts for required/optional and
 * lifecycle semantics, so this view model must not reinterpret raw statuses.
 */
export function deriveServicesHealthSummary(
  health: CurrentHealth | null | undefined,
  enabledCount: number,
  totalCount: number,
): ServicesHealthSummary {
  let badCount = 0;
  let warnCount = 0;
  const incompleteIds = new Set<string>();

  for (const service of health?.services ?? []) {
    if (service.tone === "bad") badCount += 1;
    if (service.tone === "warn") warnCount += 1;
    if (
      service.tone === "neutral" &&
      (service.observation === "unavailable" ||
        (service.observation === "up" && service.routable === null))
    ) {
      incompleteIds.add(service.service_id);
    }
  }

  const incompleteCount = incompleteIds.size;
  if (badCount > 0) {
    return {
      badCount,
      warnCount,
      incompleteCount,
      metricTone: "bad",
      metricLabel: `enabled · ${badCount} action needed`,
      incidentTone: "bad",
    };
  }
  if (warnCount > 0) {
    return {
      badCount,
      warnCount,
      incompleteCount,
      metricTone: "warn",
      metricLabel: `enabled · ${warnCount} needs attention`,
      incidentTone: "warn",
    };
  }
  if (incompleteCount > 0) {
    return {
      badCount,
      warnCount,
      incompleteCount,
      metricTone: "neutral",
      metricLabel: `enabled · ${incompleteCount} incomplete`,
      incidentTone: null,
    };
  }
  return {
    badCount,
    warnCount,
    incompleteCount,
    metricTone:
      totalCount > 0 && enabledCount === totalCount ? "ok" : "neutral",
    metricLabel: "services enabled",
    incidentTone: null,
  };
}
