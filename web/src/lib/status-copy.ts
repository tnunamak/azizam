/** Product wording for state and reason codes returned by gateway APIs. */
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
  | string;

const stateReason: Record<string, string> = {
  ready: "Ready to use.",
  ready_on_request: "Ready to start when you make a request.",
  idle: "Ready to start when you make a request.",
  starting: "Starting. Try again shortly.",
  busy: "Busy. Try again shortly.",
  degraded: "Degraded. Requests may fail.",
  unknown: "",
  configured: "",
  unavailable: "",
  unavailable_disabled: "Unavailable. Ask the owner to enable this capability.",
  unavailable_unconfigured: "Unavailable. Ask the owner to finish setup.",
  not_available: "Unavailable.",
  policy_denied: "Unavailable under this access plan.",
};

const capabilityLabel: Record<string, string> = {
  chat: "Chat",
  image: "Images",
  video: "Video",
  speech: "Speech",
  transcribe: "Transcription",
  music: "Music",
  sfx: "Sound effects",
  audio: "Audio",
  decide: "Decisions",
  embed: "Embeddings",
  rerank: "Reranking",
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
  const labels = ids.map((id) => capabilityLabel[id] ?? id).filter(Boolean);
  if (!labels.length && summary.reason_code === "no_capabilities") {
    return "No capabilities are available to your account yet.";
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
    const verb = labels.length === 1 && !["Images", "Decisions", "Embeddings", "Sound effects"].includes(labels[0]) ? "is" : "are";
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
