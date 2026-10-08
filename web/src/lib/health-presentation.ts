import type { CurrentHealthService, Service } from "./api";

type Tone = "ok" | "warn" | "bad" | "neutral";

/** Registry labels take precedence over observation labels and internal names. */
export function engineDisplayName(
  service?: Pick<Service, "display_name" | "name">,
  health?: Pick<CurrentHealthService, "engine_label" | "name">,
): string {
  return (
    service?.display_name ||
    health?.engine_label ||
    service?.name ||
    health?.name ||
    "Engine"
  );
}

/** Present observations identically on every owner health surface. */
export function engineHealthPresentation(
  enabled: boolean,
  observationAvailable: boolean,
  health?: CurrentHealthService,
  backendStatus?: unknown,
): { label: string; tone: Tone } {
  if (!enabled) return { label: "Disabled", tone: "neutral" };
  if (!observationAvailable) return { label: "", tone: "neutral" };
  const observation =
    health?.observation ?? String(backendStatus ?? "").toLowerCase();
  if (observation === "up") {
    if (health?.tone === "bad") return { label: "Unavailable", tone: "bad" };
    if (health?.tone === "warn") return { label: "Degraded", tone: "warn" };
    return { label: "Ready", tone: "ok" };
  }
  if (observation === "degraded") return { label: "Degraded", tone: "warn" };
  if (observation === "down")
    return {
      label: "Unavailable",
      tone: health?.tone === "warn" ? "warn" : "bad",
    };
  if (observation === "busy") return { label: "Busy", tone: "warn" };
  if (observation === "idle")
    return { label: "Ready to start", tone: "neutral" };
  if (observation === "standby_stopped")
    return { label: "Standby, stopped", tone: "neutral" };
  if (observation === "starting") return { label: "Starting", tone: "neutral" };
  if (observation === "disabled") return { label: "Disabled", tone: "neutral" };
  return { label: "", tone: "neutral" };
}

/** The engine's own name for actions ("Inspect Video studio"), without the
 * capability suffix that status rows append ("Video studio (video)"). */
export function engineShortName(
  service?: Pick<Service, "display_name" | "name">,
  health?: Pick<CurrentHealthService, "engine_label" | "name">,
): string {
  const name =
    service?.display_name ||
    service?.name ||
    health?.engine_label ||
    health?.name ||
    "engine";
  return name.replace(/\s*\([^)]*\)\s*$/, "") || name;
}
