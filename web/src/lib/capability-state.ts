/**
 * One readiness model for every surface (DESIGN.md, "Status model").
 *
 * Each capability gets exactly one state word, derived for the person who is
 * looking from recorded evidence only: the gateway's availability projection
 * and its evidence time, the engine and provider checks (owners), and the
 * viewer's own recent requests. Home, Status, the shell indicator and the
 * capability lists all read from here, so they cannot disagree.
 */

export type CapabilityStateKey =
  | "ready"
  | "busy"
  | "degraded"
  | "down"
  | "blocked"
  | "off"
  | "unknown";

export type StateTone = "ok" | "busy" | "warn" | "bad" | "off" | "unknown";

/** The fixed vocabulary. Definitions live in DESIGN.md; keep them in step. */
export const STATE_WORDS: Record<
  CapabilityStateKey,
  { word: string; tone: StateTone; rank: number }
> = {
  down: { word: "Down", tone: "bad", rank: 6 },
  blocked: { word: "Blocked", tone: "warn", rank: 5 },
  degraded: { word: "Degraded", tone: "warn", rank: 4 },
  unknown: { word: "Unknown", tone: "unknown", rank: 3 },
  busy: { word: "Busy", tone: "busy", rank: 2 },
  ready: { word: "Ready", tone: "ok", rank: 1 },
  off: { word: "Off", tone: "off", rank: 0 },
};

/** A check older than this cannot vouch for readiness now. */
export const FRESH_S = 15 * 60;

export interface EvidenceInput {
  source?: string | null;
  /** "no_response" when the latest check got no answer. */
  kind?: string | null;
  observed_at_s?: number | null;
  receipt_status?: string | null;
}

export interface CapabilityInput {
  key: string;
  /** The name people see (VOICE glossary). */
  label: string;
  /** Names the gateway uses for this capability in engine records. */
  matchLabels?: string[];
  status?: string | null;
  reason_code?: string | null;
  available?: boolean | null;
  evidence?: EvidenceInput | null;
}

export interface EngineSignal {
  serviceId: string;
  name: string;
  /** Capability labels this engine serves, e.g. ["Music", "Sound effects"]. */
  labels: string[];
  enabled: boolean;
  /** up | degraded | down | standby_stopped | disabled | unavailable | … */
  observation: string;
  /** The gateway's judgement: ok | warn | bad | neutral. */
  tone?: string | null;
  inUse?: boolean | null;
  lastError?: string | null;
  isProvider?: boolean;
  /** When the gateway's background check last recorded this engine. */
  checkedAt?: number | null;
}

export interface RequestSignal {
  capability: string | null;
  status: string;
  at: number;
  remote?: boolean | null;
  warnings?: string[] | null;
}

export interface CapabilityView {
  key: string;
  label: string;
  state: CapabilityStateKey;
  word: string;
  tone: StateTone;
  /** One plain sentence of why, or null when the word says it all. */
  detail: string | null;
  /** Health-check time behind the state (epoch s), or null. */
  checkedAt: number | null;
  /** Request time when the state rests on a request, not a check. */
  usedAt: number | null;
  /** Engines failing for this capability (owners). */
  failing: EngineSignal[];
}

const TERMINAL_FAILURE = new Set(["failed", "error", "timeout", "timed_out"]);
const SUCCESS = new Set(["succeeded", "success", "completed", "ok"]);
/**
 * A refusal because a spending limit was reached. Matches the gateway's
 * BudgetExceeded text ("daily profile global cap would exceed $5") and the
 * plan sentence ("…has used its daily remote spending allowance"); not price
 * or reconciliation holds, which need a different fix.
 */
export const CAP_WARNING = /spending allowance|\bcap\b[^.]*\bexceed/i;

/** True while a refusal of this kind can still be in force. */
export function capRefusalCurrent(
  request: RequestSignal,
  now: number,
): boolean {
  const warning =
    (request.warnings ?? []).find((text) => CAP_WARNING.test(text)) ?? "";
  if (/monthly/i.test(warning)) {
    // A monthly limit resets at the start of the calendar month (UTC).
    const date = new Date(now * 1000);
    return (
      request.at >=
      Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), 1) / 1000
    );
  }
  // A daily limit is a rolling 24 hours, so any refusal clears within a day.
  return now - request.at <= 86_400;
}

/** Request intents and paths map to the capability a person recognises. */
export function requestCapability(
  intent?: string | null,
  path?: string | null,
): string | null {
  const value = String(intent ?? "").toLowerCase();
  const route = String(path ?? "").toLowerCase();
  if (
    /^(chat|responses|text\.completions)/.test(value) ||
    /chat\/completions|\/responses/.test(route)
  )
    return "chat";
  if (value.startsWith("image") || route.includes("/images/")) return "image";
  if (value.startsWith("video") || route.includes("/videos")) return "video";
  if (/transcri/.test(value) || route.includes("/audio/transcriptions"))
    return "transcribe";
  if (/^(speech|audio\.speech)/.test(value) || route.includes("/audio/speech"))
    return "speech";
  if (/sfx/.test(value) || route.includes("/audio/sfx")) return "sfx";
  if (/music/.test(value)) return "music";
  if (/embed/.test(value) || route.includes("/embeddings")) return "embed";
  if (/rerank/.test(value)) return "rerank";
  if (/decision|systemone/.test(value)) return "decide";
  if (value.startsWith("audio")) return "audio";
  return null;
}

export function isTerminalFailure(status: string | null | undefined) {
  return TERMINAL_FAILURE.has(String(status ?? "").toLowerCase());
}
export function isSuccess(status: string | null | undefined) {
  return SUCCESS.has(String(status ?? "").toLowerCase());
}
export function isInFlight(status: string | null | undefined) {
  return ["running", "accepted", "queued", "in_progress", "pending"].includes(
    String(status ?? "").toLowerCase(),
  );
}

export function ageText(seconds: number): string {
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} days ago`;
}

/**
 * A spending-limit refusal still applies only while no later paid request
 * for the same capability has succeeded and its limit cannot have reset.
 * Returns the latest such refusal.
 */
export function activeCapRefusal(
  requests: RequestSignal[],
  capability?: string,
  now = Date.now() / 1000,
): RequestSignal | null {
  const relevant = requests
    .filter((request) => !capability || request.capability === capability)
    .toSorted((a, b) => b.at - a.at);
  for (const request of relevant) {
    if (request.remote && isSuccess(request.status)) return null;
    if (
      String(request.status).toLowerCase() === "rejected" &&
      (request.warnings ?? []).some((text) => CAP_WARNING.test(text))
    )
      return capRefusalCurrent(request, now) ? request : null;
  }
  return null;
}

/**
 * An engine fault in plain words, the same everywhere. The raw transport
 * error stays in the owner's technical details.
 */
export function engineSymptom(engine: {
  name: string;
  lastError?: string | null;
  observation?: string | null;
}): string {
  const raw = String(engine.lastError ?? "").toLowerCase();
  if (/connection refused|connect refused|econnrefused/.test(raw))
    return `${engine.name} is not accepting connections.`;
  if (/timeout|timed out/.test(raw))
    return `${engine.name} did not answer in time.`;
  if (/http\s*5\d\d|\b5\d\d\b/.test(raw))
    return `${engine.name} is answering with errors.`;
  if (engine.observation === "degraded")
    return `${engine.name} is answering, but not reliably.`;
  return `${engine.name} is not answering.`;
}

/** The first thing for the owner to try, matched to the symptom. */
export function engineRemedy(lastError?: string | null): string {
  const raw = String(lastError ?? "").toLowerCase();
  if (raw.includes("refused"))
    return "Check the engine address, or start the engine.";
  if (/timeout|timed out/.test(raw))
    return "Check whether the engine is busy or unreachable.";
  return "Check the engine’s log and restart it.";
}

function engineFailing(engine: EngineSignal): boolean {
  if (!engine.enabled) return false;
  // A stopped standby engine nobody uses is not failing: the gateway says neutral.
  if (engine.tone === "neutral" && engine.inUse === false) return false;
  if (engine.tone === "bad") return true;
  // "unavailable" with a neutral judgement means the gateway could not
  // check it: that is Unknown, not Down.
  if (engine.observation === "unavailable" && engine.tone === "neutral")
    return false;
  return ["down", "degraded", "unavailable"].includes(engine.observation);
}

export function capabilityView(
  input: CapabilityInput,
  {
    now,
    engines,
    requests = [],
  }: { now: number; engines?: EngineSignal[]; requests?: RequestSignal[] },
): CapabilityView {
  const make = (
    state: CapabilityStateKey,
    detail: string | null,
    extra: Partial<CapabilityView> = {},
  ): CapabilityView => ({
    key: input.key,
    label: input.label,
    state,
    word: STATE_WORDS[state].word,
    tone: STATE_WORDS[state].tone,
    detail,
    checkedAt: null,
    usedAt: null,
    failing: [],
    ...extra,
  });
  const code = String(input.reason_code || "").toLowerCase();
  const status = String(input.status || "").toLowerCase();
  if (code === "unavailable_disabled")
    return make("off", "Turned off by the owner.");
  if (code === "unavailable_unconfigured")
    return make("off", "Not set up yet.");
  if (code === "policy_denied")
    return make("blocked", "Not included in your access plan.");

  // Evidence time: a check is never shown as a use, or the reverse.
  const evidence = input.evidence ?? null;
  const source = String(evidence?.source ?? "");
  const at =
    typeof evidence?.observed_at_s === "number" ? evidence.observed_at_s : null;
  const isRequest = source === "request";
  const isCheck =
    Boolean(source) &&
    !["registry", "capability", "configuration", "request"].includes(source);
  const checkedAt = isCheck ? at : null;
  const usedAt = isRequest ? at : null;
  const stamp = { checkedAt, usedAt };

  const mine = requests.filter((request) => request.capability === input.key);
  const recentFailures = mine.filter(
    (request) => isTerminalFailure(request.status) && now - request.at <= 3600,
  );
  const lastSuccess = mine
    .filter((request) => isSuccess(request.status))
    .reduce((latest, request) => Math.max(latest, request.at), 0);
  const unresolvedFailures = recentFailures.filter(
    (request) => request.at > lastSuccess,
  );
  const refusal = activeCapRefusal(requests, input.key, now);

  // Owner view: engine and provider checks for this capability.
  const names = new Set(
    [input.label, ...(input.matchLabels ?? [])].map((name) =>
      name.toLowerCase(),
    ),
  );
  const serving = (engines ?? []).filter(
    (engine) =>
      engine.enabled &&
      engine.labels.some((label) => names.has(label.toLowerCase())),
  );
  const failing = serving.filter(engineFailing);
  if (serving.length && failing.length === serving.length) {
    const first = failing[0];
    // Every engine still answers, but badly: degraded, not down.
    const degraded = failing.every(
      (engine) => engine.observation === "degraded",
    );
    return make(degraded ? "degraded" : "down", engineSymptom(first), {
      ...stamp,
      failing,
    });
  }

  // The gateway's projection says unavailable even without a timed check:
  // that is a current fault, not missing knowledge.
  if (status === "unavailable" && at === null) return make("down", null, stamp);
  if (!evidence || status === "unknown" || (!at && !source)) {
    const lastChecks = serving
      .map((engine) => engine.checkedAt)
      .filter((value): value is number => typeof value === "number");
    const last = lastChecks.length ? Math.max(...lastChecks) : null;
    const noAnswer = evidence?.kind === "no_response";
    return make(
      "unknown",
      last && now - last > FRESH_S
        ? `Last checked ${ageText(now - last)}; no answer since.`
        : noAnswer
          ? "No answer to the latest check."
          : null,
      { usedAt, checkedAt: null },
    );
  }
  if (at !== null && now - at > FRESH_S) {
    return make(
      "unknown",
      `${isRequest ? "Last used" : "Last checked"} ${ageText(now - at)}.`,
      stamp,
    );
  }

  if (failing.length) {
    const first = failing[0];
    return make(
      "degraded",
      `${first.isProvider ? "Backup service" : "One engine"} (${first.name}) is failing; the others work.`,
      { ...stamp, failing },
    );
  }
  if (status === "unavailable") return make("down", null, stamp);
  if (status === "busy" || status === "starting") {
    // Busy means busy. A busy report beside a failed request is a fault.
    const failedSinceSuccess = mine.some(
      (request) =>
        isTerminalFailure(request.status) &&
        request.at > lastSuccess &&
        now - request.at <= 86_400,
    );
    if (failedSinceSuccess)
      return make(
        "degraded",
        "Reported busy, and requests have failed since the last success.",
        stamp,
      );
    if (status === "starting") return make("busy", "Starting up.", stamp);
    // The gateway's capability "busy" also covers an engine whose check
    // says "degraded" (gateway/availability.py BUSY_STATUSES), so a member
    // was told "working on other requests" while the owner saw the same
    // engine answering with errors (red team round 2, P0). Only an engine
    // check that itself says busy (owner evidence) supports a queue; without
    // it, say only what both readings share. DESIGN.md backend ask 16.
    const checkedBusy = serving.some((engine) => engine.observation === "busy");
    if (checkedBusy) return make("busy", "Working on other requests.", stamp);
    return make("degraded", "May be slow or fail right now.", stamp);
  }
  if (refusal)
    return make(
      "degraded",
      "Some models are paused: a spending limit was reached.",
      stamp,
    );
  if (unresolvedFailures.length)
    return make(
      "degraded",
      `${unresolvedFailures.length} request${unresolvedFailures.length === 1 ? "" : "s"} failed in the last hour.`,
      stamp,
    );
  if (
    ["ready", "idle", "idle-startable", "standby_stopped"].includes(status) ||
    input.available
  )
    return make("ready", null, stamp);
  return make("unknown", null, stamp);
}

export interface Verdict {
  tone: StateTone;
  text: string;
  problems: CapabilityView[];
  /** Oldest fresh check time behind the verdict. */
  checkedAt: number | null;
}

function capitalise(value: string) {
  return value ? value.charAt(0).toUpperCase() + value.slice(1) : value;
}

/** The verdict line: exceptions only, as a label, never a sentence with a full stop. */
export function verdictFor(views: CapabilityView[]): Verdict {
  const checks = views
    .map((view) => view.checkedAt)
    .filter((value): value is number => typeof value === "number");
  const checkedAt = checks.length ? Math.min(...checks) : null;
  const counted = views.filter((view) => view.state !== "off");
  if (!counted.length)
    return {
      tone: "off",
      text: "Nothing is set up yet",
      problems: [],
      checkedAt,
    };
  const problems = counted
    .filter((view) => view.state !== "ready")
    .toSorted((a, b) => STATE_WORDS[b.state].rank - STATE_WORDS[a.state].rank);
  if (!problems.length)
    return { tone: "ok", text: "Everything works", problems, checkedAt };
  const worst = problems[0];
  if (counted.every((view) => view.state === "down"))
    return {
      tone: "bad",
      text: "Nothing works right now",
      problems,
      checkedAt,
    };
  const text =
    problems.length <= 2
      ? capitalise(
          problems
            .map(
              (view) =>
                `${view.label.toLowerCase()} ${view.word.toLowerCase()}`,
            )
            .join(", "),
        )
      : `${problems.length} of ${counted.length} capabilities need attention`;
  return { tone: worst.tone, text, problems, checkedAt };
}
