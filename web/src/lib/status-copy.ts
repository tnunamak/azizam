/** Product wording for state and reason codes returned by gateway APIs. */
import { capabilityLabelFor } from "./terms";

export type AvailabilityReasonCode =
  | "ready"
  | "ready_on_request"
  | "starting"
  | "busy"
  | "degraded"
  | "unknown"
  | "unavailable"
  | "unavailable_disabled"
  | "unavailable_unconfigured"
  | "configured"
  | "not_available"
  | "no_capabilities"
  | "policy_denied"
  | "backend_unavailable"
  | "engine_transitioning"
  | "model_not_deployed"
  | "current_model_unavailable"
  | "service_disabled"
  | "service_maintenance"
  | "health_unknown"
  | "required_file_missing"
  | string;

export const stateReason: Readonly<Record<string, string>> = {
  ready: "Ready to use.",
  ready_on_request: "Ready to start when you make a request.",
  idle: "Ready to start when you make a request.",
  starting: "Starting. Try again shortly.",
  busy: "Busy. Try again shortly.",
  degraded: "Degraded. Requests may fail. Try again, and ask the owner if it continues.",
  unknown: "Status could not be checked. Ask the owner to check the service.",
  configured: "Set up but not started. Ask the owner to check it.",
  unavailable: "Unavailable. Ask the owner to check this capability.",
  unavailable_disabled: "Unavailable. Ask the owner to enable this capability.",
  unavailable_unconfigured: "Unavailable. Ask the owner to finish setup.",
  not_available: "Unavailable. Ask the owner to check this capability.",
  policy_denied: "Unavailable under this access plan. Ask the owner about changing access.",
  backend_unavailable: "This model's service is unavailable. Ask the owner to check it.",
  engine_transitioning: "This model is starting. Try again shortly.",
  model_not_deployed: "This model is not installed. Ask the owner to make it available.",
  current_model_unavailable: "No model is loaded. Ask the owner to load one.",
  service_disabled: "This model's service is turned off. Ask the owner to turn the service on.",
  service_maintenance: "This model is down for maintenance. Ask the owner when it will return.",
  health_unknown: "This model's status could not be checked. Ask the owner to check its service.",
  required_file_missing: "This model is missing a file it needs. Ask the owner to install it.",
};

export function availabilityReason(
  state: string,
  reasonCode?: AvailabilityReasonCode,
): string | undefined {
  const code = reasonCode || state;
  const copy = stateReason[code] ?? stateReason[state] ?? "";
  return copy || undefined;
}

export function statusSummaryCopy(summary: {
  state: string;
  reason_code?: AvailabilityReasonCode;
  affected_capability_ids?: string[];
  counts?: { affected?: number };
}): string | undefined {
  if (summary.state === "ready") return undefined;
  const ids = summary.affected_capability_ids ?? [];
  const labels = ids.map((id) => capabilityLabelFor(id) ?? id).filter(Boolean);
  if (!labels.length && summary.reason_code === "no_capabilities") {
    return "No capabilities are available to your account yet. Ask the owner to check your account access.";
  }
  if (!labels.length) return availabilityReason(summary.state, summary.reason_code);
  const count = summary.counts?.affected ?? labels.length;
  const names = labels.length > 4
    ? `${labels.slice(0, 3).join(", ")}, and ${count - 3} more capabilities`
    : labels.length === 1
      ? labels[0]
      : labels.length === 2
        ? `${labels[0]} and ${labels[1].toLowerCase()}`
        : `${labels.slice(0, -1).join(", ")}, and ${labels.at(-1)?.toLowerCase()}`;
  if (summary.state === "unavailable") {
    const verb = labels.length === 1 && !["Images", "3D models", "Decisions", "Sound effects"].includes(labels[0]) ? "is" : "are";
    const code = summary.reason_code;
    const ending = code === "unavailable_disabled"
      ? "disabled"
      : code === "unavailable_unconfigured"
        ? "not set up"
        : "unavailable";
    return count > labels.length && labels.length <= 4
      ? `${names} and ${count - labels.length} more ${count - labels.length === 1 ? "capability is" : "capabilities are"} ${ending}.`
      : `${names} ${verb} ${ending}.`;
  }
  return availabilityReason(summary.state, summary.reason_code);
}
