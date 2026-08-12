import type { CurrentHealth, StatusPayload } from "./api";

export type StatusHealthQueryState =
  | "loading"
  | "known"
  | "stale-known"
  | "unavailable";

export type StatusHealthTone = "ok" | "warn" | "bad" | "neutral";

export interface StatusHealthProjection {
  state: StatusHealthQueryState;
  health: CurrentHealth | null;
  tone: StatusHealthTone;
  label: string;
  detail: string;
}

/**
 * Project React Query's transport state and the gateway's current-health
 * payload into the one operator-facing state used by admin read surfaces.
 *
 * A failed refresh does not erase a previously known health projection. The
 * caller can therefore retain those facts while stating that they are stale.
 */
export function projectStatusHealth({
  data,
  isPending,
  isError,
}: {
  data: StatusPayload | undefined;
  isPending: boolean;
  isError: boolean;
}): StatusHealthProjection {
  const health = data?.current_health;
  if (health) {
    return {
      state: isError ? "stale-known" : "known",
      health,
      tone: healthTone(health.tone),
      label: isError ? "Showing last known status" : health.label,
      detail: isError ? `${health.label}. ${health.detail}` : health.detail,
    };
  }

  if (isPending && !isError) {
    return {
      state: "loading",
      health: null,
      tone: "neutral",
      label: "Checking current gateway health",
      detail: "Waiting for the gateway health projection.",
    };
  }

  return {
    state: "unavailable",
    health: null,
    tone: "bad",
    label: "Status unavailable",
    detail: "The gateway did not return current health.",
  };
}

function healthTone(value: string): StatusHealthTone {
  return value === "ok" || value === "warn" || value === "bad"
    ? value
    : "neutral";
}
