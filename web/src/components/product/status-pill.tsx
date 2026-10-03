import { Check, CircleSlash, Loader2, Clock3 } from "lucide-react";
import { Badge } from "@/components/ui/badge";

export type AvailabilityStatus = "ready" | "starting" | "busy" | "unavailable";
export interface StatusPillProps {
  status: AvailabilityStatus;
  reason?: string;
}
const states = {
  ready: { label: "Ready", icon: Check, color: "text-success" },
  starting: { label: "Starting", icon: Loader2, color: "text-info" },
  busy: { label: "Busy", icon: Clock3, color: "text-warning" },
  unavailable: {
    label: "Unavailable",
    icon: CircleSlash,
    color: "text-muted-foreground",
  },
};
export function StatusPill({ status, reason }: StatusPillProps) {
  const { label, icon: Icon, color } = states[status];
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
