/**
 * One mapping from a workflow's catalog availability to what a person sees:
 * whether it runs, the word a picker shows, why it does not run, and (for the
 * owner only) where to fix it. The Workflows page and Try both read it, so
 * they cannot word the same state two ways.
 */
import { availabilityReason } from "@/lib/status-copy";

export interface WorkflowAvailability {
  status?: string;
  reason_code?: string;
  available?: boolean;
  /**
   * Plain-language reason the gateway gives for a workflow it composes from
   * engines, naming the engine it waits on ("ComfyUI is down.").
   */
  reason?: string;
  /** The engine that limits a composed workflow; used for the owner's link. */
  engine?: string;
  evidence?: { kind?: string; source?: string };
}

/**
 * - ready: runs now.
 * - ready_to_start: runs; an on-demand engine starts on the request.
 * - wait: starting, busy or degraded; it fixes itself.
 * - down: an engine it needs is down or unchecked, and the gateway names it.
 * - setup: the owner has to set something up.
 */
export type WorkflowReadiness =
  | "ready"
  | "ready_to_start"
  | "wait"
  | "down"
  | "setup";

export interface WorkflowFix {
  href: string;
  action: string;
}

export interface WorkflowAvailabilityView {
  readiness: WorkflowReadiness;
  runnable: boolean;
  /** Short note a picker shows after the title; null when ready. */
  pickerNote: string | null;
  /** Why, in plain language; null when there is nothing to explain. */
  text: string | null;
  /** Where the owner fixes it. Members never get one. */
  fix: WorkflowFix | null;
}

export const READY_TO_START = "Ready to start";

export const MEMBER_SETUP_NOTE =
  "Not available right now. The person who manages Azizam can set it up.";

const WAIT_NOTE: Record<string, string> = {
  starting: "Try again in a moment.",
  busy: "Busy with other requests. Try again soon.",
  degraded: "Requests may fail.",
};

const WAIT_WORD: Record<string, string> = {
  starting: "Starting",
  busy: "Busy",
  degraded: "Degraded",
};

function enginesHref(engine: string | null | undefined) {
  const id = engine?.trim();
  // "gateway" is the gateway itself, not an engine the Engines page lists.
  return id && id !== "gateway" && !id.includes(",")
    ? `/engines?service=${encodeURIComponent(id)}`
    : "/engines";
}

/** What is missing and where the owner fixes it, from the catalog's codes. */
function ownerSetupHelp(
  availability: WorkflowAvailability,
  engineBinding: string | null | undefined,
): { text: string; fix: WorkflowFix } {
  const { status, reason_code: code } = availability;
  const engines = { href: enginesHref(engineBinding), action: "Open Engines" };
  const statusPage = { href: "/status", action: "Open Status" };
  if (code === "unavailable_disabled")
    return { text: "Turned off. Turn it on in Engines.", fix: engines };
  if (code === "unavailable_unconfigured" || status === "not_configured")
    return { text: "Setup is not finished. Finish it in Engines.", fix: engines };
  if (code === "not_available" || status === "not_available")
    return {
      text: "No engine is set up for it. Add one in Engines.",
      fix: engines,
    };
  if (code === "policy_denied")
    return {
      text: "An access plan blocks it. Review plans in People.",
      fix: { href: "/people?tab=plans", action: "Open access plans" },
    };
  if (status === "unknown" || code === "unknown")
    return {
      text: "No health check has reported on it yet. See Status.",
      fix: statusPage,
    };
  return { text: "Its engine is not answering. See Status.", fix: statusPage };
}

export function workflowAvailabilityView(
  availability: WorkflowAvailability | null | undefined,
  {
    owner,
    engineBinding,
  }: { owner: boolean; engineBinding?: string | null },
): WorkflowAvailabilityView {
  const status = String(availability?.status ?? "").toLowerCase();
  const reason = availability?.reason?.trim() || null;
  if (status === "idle")
    return {
      readiness: "ready_to_start",
      runnable: true,
      pickerNote: READY_TO_START,
      text: reason ?? availabilityReason("idle") ?? null,
      fix: null,
    };
  if (availability?.available !== false)
    return {
      readiness: "ready",
      runnable: true,
      pickerNote: null,
      text: null,
      fix: null,
    };
  if (status in WAIT_NOTE)
    return {
      readiness: "wait",
      runnable: false,
      pickerNote: WAIT_WORD[status],
      text: reason ?? WAIT_NOTE[status],
      fix: null,
    };
  if (reason)
    return {
      readiness: "down",
      runnable: false,
      pickerNote: reason,
      text: reason,
      fix: owner
        ? {
            href: enginesHref(availability?.engine ?? engineBinding),
            action: "Open Engines",
          }
        : null,
    };
  if (!owner)
    return {
      readiness: "setup",
      runnable: false,
      pickerNote: "Unavailable",
      text: MEMBER_SETUP_NOTE,
      fix: null,
    };
  const help = ownerSetupHelp(availability ?? {}, engineBinding);
  return {
    readiness: "setup",
    runnable: false,
    pickerNote: "Unavailable",
    text: help.text,
    fix: help.fix,
  };
}
