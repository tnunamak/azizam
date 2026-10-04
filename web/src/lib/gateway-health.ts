import type { CurrentHealth } from "./api";

/** Explain only facts visible to this viewer; member state comes from Home. */
export function gatewayHealthReason({
  admin,
  health,
  statusError,
  reachable,
  reachabilityError,
  memberStatus,
}: {
  admin: boolean;
  health?: CurrentHealth;
  statusError: boolean;
  reachable: boolean;
  reachabilityError: boolean;
  memberStatus?: { state: string; reason: string };
}): string {
  if (reachabilityError)
    return "The gateway could not be reached. Try again shortly.";
  if (!admin) {
    return (
      memberStatus?.reason ||
      (reachable
        ? "The gateway is reachable; model availability is shown on Home."
        : "Checking whether your gateway is reachable.")
    );
  }
  if (statusError)
    return "Current service health could not be loaded. Open Status to retry.";
  const failed =
    health?.observation_available &&
    health.services.find(
      (service) =>
        service.configured &&
        service.enabled &&
        service.in_use === true &&
        ["down", "degraded"].includes(service.observation),
    );
  if (failed)
    return `${failed.name} is ${failed.observation === "down" ? "unavailable" : "degraded"}. Check its connection on Status.`;
  if (health?.detail) return health.detail;
  if (health?.tone === "bad" || health?.tone === "warn")
    return "Some gateway checks need attention. Open Status for the details.";
  if (health?.observation_available && health.tone === "ok")
    return "The gateway and its measured services are ready.";
  return "Current service health has not been measured yet.";
}

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
  if (reachabilityError) return { label: "Gateway unreachable", tone: "bad" };
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
