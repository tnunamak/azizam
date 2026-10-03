import type { CurrentHealth } from "./api";

/** Reachability and engine health prove different things; do not conflate them. */
export function gatewayHealthLabel({
  admin,
  health,
  statusError,
  reachable,
  reachabilityError,
}: {
  admin: boolean;
  health?: CurrentHealth;
  statusError: boolean;
  reachable: boolean;
  reachabilityError: boolean;
}): { label: string; tone: "ok" | "warn" | "bad" | "neutral" } {
  if (reachabilityError) return { label: "Down", tone: "bad" };
  if (!admin)
    return reachable
      ? { label: "Gateway reachable", tone: "ok" }
      : { label: "Checking gateway", tone: "neutral" };
  if (statusError) return { label: "Status unavailable", tone: "neutral" };
  if (health?.tone === "bad" || health?.tone === "warn")
    return { label: "Degraded", tone: "warn" };
  if (health?.observation_available && health.tone === "ok")
    return { label: "Healthy", tone: "ok" };
  return { label: "Not measured yet", tone: "neutral" };
}
