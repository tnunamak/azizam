import { Check, CircleHelp, CircleSlash, Loader2, Clock3 } from "lucide-react";
import { Badge } from "@/components/ui/badge";

export type AvailabilityStatus =
  | "ready"
  | "starting"
  | "busy"
  | "idle"
  | "idle-startable"
  | "standby_stopped"
  | "unknown"
  | "not_measured"
  | "not measured yet"
  | "unavailable"
  | string;
export interface StatusPillProps {
  status: AvailabilityStatus;
  reason?: string;
  /** Summaries omit unobserved values; details still explain unknown status. */
  summary?: boolean;
}
const states = {
  ready: { label: "Ready", icon: Check, color: "text-success" },
  starting: { label: "Starting", icon: Loader2, color: "text-info" },
  busy: { label: "Busy", icon: Clock3, color: "text-warning" },
  idle: { label: "Ready to start", icon: Check, color: "text-success" },
  "idle-startable": {
    label: "Ready to start",
    icon: Check,
    color: "text-success",
  },
  standby_stopped: {
    label: "Ready to start",
    icon: Check,
    color: "text-success",
  },
  unknown: {
    label: "Unknown",
    icon: CircleHelp,
    color: "text-muted-foreground",
  },
  not_measured: {
    label: "Unknown",
    icon: CircleHelp,
    color: "text-muted-foreground",
  },
  "not measured yet": {
    label: "Unknown",
    icon: CircleHelp,
    color: "text-muted-foreground",
  },
  unavailable: {
    label: "Unavailable",
    icon: CircleSlash,
    color: "text-muted-foreground",
  },
};

function normalizeStatus(status: AvailabilityStatus): keyof typeof states {
  const normalized = String(status).toLowerCase().replaceAll("_", "-");
  if (normalized === "standby-stopped") return "standby_stopped";
  if (normalized === "not-measured" || normalized === "not measured yet")
    return "not_measured";
  if (normalized in states) return normalized as keyof typeof states;
  return "unknown";
}

export function StatusPill({
  status,
  reason,
  summary = false,
}: StatusPillProps) {
  const normalized = normalizeStatus(status);
  if (
    summary &&
    ["unknown", "not_measured", "not measured yet"].includes(normalized)
  )
    return null;
  const { label, icon: Icon, color } = states[normalized];
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <Badge variant="secondary">
        <Icon aria-hidden="true" className={color} />
        {label}
      </Badge>
      {reason && (
        <span className="text-muted-foreground text-xs">{reason}</span>
      )}
    </span>
  );
}
