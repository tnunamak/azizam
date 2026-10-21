/**
 * Prompt memory: what the gateway keeps of the words a person types, and the
 * plain words the UI uses for it (docs/design/PROMPT_MEMORY.md).
 *
 * The gateway keeps a prompt with what it made, for the maker only. A member
 * chooses on or off and how long; the owner can turn it off for everyone and
 * cap how long anyone's prompts are kept. These helpers hold the copy and the
 * rules the screens share, so Try, Library, the member's settings and the
 * owner's Settings never say different things.
 */
import { adminFetch, ApiError } from "./api";

/** How long prompts are kept: a choice id from the gateway's retention file (`registry/retention.yml`). */
export type Retention = string;

/** One way to keep prompts. `seconds: null` keeps them with no deadline. */
export interface RetentionChoice {
  id: Retention;
  seconds: number | null;
}

/**
 * The choices and defaults of the prompts group, as the gateway's retention
 * file states them. Both prompt-memory responses carry it; the screens keep no
 * copy of their own.
 */
export interface RetentionContract {
  /** In the order the screens list them. */
  choices: RetentionChoice[];
  /** Existing saved periods whose durations are still known, but which cannot be chosen now. */
  known_unoffered?: RetentionChoice[];
  defaults: {
    default_remember: boolean;
    never_keep: boolean;
    retention_cap: Retention;
    remember: boolean;
    retention: Retention;
  };
}

export interface HouseholdPromptMemory {
  default_remember: boolean;
  never_keep: boolean;
  retention_cap: Retention;
}

export interface MemberPromptMemory {
  remember: boolean;
  retention: Retention;
  effective_remember: boolean;
  effective_retention: Retention;
  household: HouseholdPromptMemory;
}

/** Pending database erasure or file cleanup. No prompt text. */
export interface PromptPurge {
  pending?: boolean;
  database_pending?: boolean;
  /** Startup could not shorten saved prompt deadlines yet (owner status only). */
  deadline_repair_pending?: boolean;
  retry_scheduled?: boolean;
  retry_required?: boolean;
  metadata_stripping?: {
    status?: "queued" | "retrying" | "complete";
    queued_at?: number | null;
    attempts?: number;
    last_attempt_at?: number | null;
    files_pending?: number | null;
    last_error?: string | null;
  };
}

export interface MemberPromptMemoryResponse {
  prompt_memory: MemberPromptMemory;
  /** Raw saved IDs absent from the offered choices, before normalization. */
  unoffered?: { retention?: Retention; retention_cap?: Retention };
  prompt_purge?: PromptPurge;
  contract?: RetentionContract;
}

export interface HouseholdPromptMemoryResponse {
  prompt_memory: HouseholdPromptMemory;
  /** Raw saved IDs absent from the offered choices, before normalization. */
  unoffered?: { retention?: Retention; retention_cap?: Retention };
  revision: string;
  prompt_purge?: PromptPurge;
  contract?: RetentionContract;
}

export interface DeletePromptsResponse {
  deleted_prompts: number;
  prompt_purge?: PromptPurge;
}

/** The react-query key for the signed-in person's prompt memory. */
export const promptMemoryKey = ["account", "prompt-memory"] as const;

let current: RetentionContract | null = null;

/** Remember the contract a prompt-memory response carries. */
export function setRetentionContract(
  next: RetentionContract | null | undefined,
) {
  if (next && Array.isArray(next.choices)) current = next;
}

/** Keep the contract of a response, then hand the response on. */
export function learnContract<T extends { contract?: RetentionContract }>(
  response: T,
): T {
  setRetentionContract(response?.contract);
  return response;
}

/** The choices, in the order the screens list them (empty until a response has arrived). */
export function retentionChoices(): Retention[] {
  return (current?.choices ?? []).map((choice) => choice.id);
}

/** The period a missing or unrecognised value stands for, from the gateway's defaults. */
export function defaultCap(): Retention {
  return current?.defaults.retention_cap ?? "";
}

export function defaultRetention(): Retention {
  return current?.defaults.retention ?? "";
}

/** How long a choice keeps prompts; a choice with no deadline is the longest. */
function length(value: Retention): number {
  const choice = knownChoice(value);
  if (!choice) return Number.NaN;
  return choice.seconds ?? Number.POSITIVE_INFINITY;
}

function knownChoice(value: Retention): RetentionChoice | undefined {
  return (
    current?.choices.find((choice) => choice.id === value) ??
    current?.known_unoffered?.find((choice) => choice.id === value)
  );
}

/** True for the choice that keeps prompts with no deadline. */
export function keepsForever(value: Retention): boolean {
  return knownChoice(value)?.seconds === null;
}

function words(value: Retention): string {
  return value.replaceAll("_", " ");
}

/** The member's choice: "Forget prompts after" … */
export function memberRetentionLabel(value: Retention): string {
  return keepsForever(value) ? "Never" : words(value);
}

/** The owner's cap: "Keep prompts for" … */
export function capLabel(value: Retention): string {
  return keepsForever(value) ? "No limit" : words(value);
}

export function isRetention(value: unknown): value is Retention {
  return (
    typeof value === "string" &&
    (current?.choices.some((choice) => choice.id === value) ?? false)
  );
}

/** Known durations may remain in saved settings after they leave the offered choices. */
export function isKnownRetention(value: unknown): value is Retention {
  return typeof value === "string" && knownChoice(value) !== undefined;
}

/** Explain a saved period that cannot be newly selected, including a deleted ID's fallback. */
export function unofferedRetentionNote(
  stored: Retention | undefined,
  effective: Retention,
  label: (value: Retention) => string,
): string | null {
  if (!stored) return null;
  const previous = label(stored).toLowerCase();
  return stored === effective
    ? `${previous} (no longer offered).`
    : `Previous choice ${previous} (no longer offered). Using ${label(effective).toLowerCase()}.`;
}

/**
 * Explain a member's saved period that cannot be newly selected. A period
 * that still exists keeps its meaning, so a shorter effective one is the
 * gateway limit's doing; a deleted ID gives way to the fallback.
 */
export function memberUnofferedNote(
  stored: Retention | undefined,
  effective: Retention,
  cap: Retention,
): string | null {
  if (!stored) return null;
  const previous = memberRetentionLabel(stored).toLowerCase();
  const using = memberRetentionLabel(effective).toLowerCase();
  if (!knownChoice(stored))
    return `Your previous choice, ${previous}, was removed. Using ${using}.`;
  if (stored === effective)
    return `Your choice, ${previous}, is no longer offered but still applies.`;
  return cap === effective
    ? `Your choice, ${previous}, is no longer offered. The gateway limit of ${capLabel(cap).toLowerCase()} applies instead.`
    : `Your choice, ${previous}, is no longer offered. Using ${using}.`;
}

/** True when `next` keeps prompts for less time than `current`. */
export function isShorter(next: Retention, previous: Retention): boolean {
  return length(next) < length(previous);
}

/** A member may choose any period no longer than the household's cap. */
export function withinCap(value: Retention, cap: Retention): boolean {
  return length(value) <= length(cap);
}

/**
 * What a run on Try does with the words, before Run is pressed.
 * - `choice`: the person keeps prompts, so each run can opt out.
 * - `member-off`: they turned prompt memory off for themselves.
 * - `household-off`: the owner turned it off for everyone.
 * - `unknown`: the setting could not be read; Try offers the opt-out and
 *   claims nothing about keeping.
 */
export type TryKeepState =
  | "choice"
  | "member-off"
  | "household-off"
  | "unknown";

export function tryKeepState(
  settings: MemberPromptMemory | null | undefined,
): TryKeepState {
  if (!settings) return "unknown";
  if (settings.household.never_keep) return "household-off";
  if (!settings.effective_remember) return "member-off";
  return "choice";
}

/**
 * The one line on prompts in a new person's welcome card, or null when the
 * setting could not be read (the card then claims nothing).
 */
export function welcomePromptNote(state: TryKeepState): string | null {
  if (state === "choice")
    return "Azizam can keep what you type so you can find it again. Only you can see it. Change this in Your settings.";
  if (state === "member-off")
    return "Azizam doesn’t keep what you type. Change this in Your settings.";
  if (state === "household-off")
    return "This gateway doesn’t keep what you type.";
  return null;
}

/**
 * Why the maker's item has no prompt, when today's settings explain it: the
 * person or the household turned memory off, which also deleted earlier
 * prompts. Otherwise null; the gateway does not record why one prompt is
 * missing (forgotten on one request, deleted, expired, or made before
 * prompts were kept), so the Library does not guess.
 */
export function promptNotSavedReason(
  state: TryKeepState,
  owner: boolean,
): string | null {
  if (state === "member-off") return "You turned off prompt keeping.";
  if (state === "household-off")
    return owner
      ? "You turned off prompt keeping for everyone."
      : "This gateway doesn’t keep prompts.";
  return null;
}

/** The request extension that tells the gateway not to keep one prompt. */
export const FORGET_EXTENSION = "com.azizam.prompts";

/**
 * Add `extensions["com.azizam.prompts"] = {mode: "forget"}` to a JSON body or
 * a multipart form's `extensions` field (which may be a JSON string). Other
 * extensions are kept as they are.
 */
export function withForget<T extends Record<string, unknown>>(
  body: T,
): T & { extensions: unknown } {
  const raw = body.extensions;
  const wasString = typeof raw === "string";
  let extensions: Record<string, unknown> = {};
  if (wasString) {
    try {
      const parsed = JSON.parse(raw as string);
      if (parsed && typeof parsed === "object") extensions = parsed;
    } catch {
      extensions = {};
    }
  } else if (raw && typeof raw === "object") {
    extensions = { ...(raw as Record<string, unknown>) };
  }
  extensions[FORGET_EXTENSION] = { mode: "forget" };
  // Queued workflow routes execute the nested request or parameters body.
  const nested: Record<string, unknown> = {};
  for (const field of ["request", "parameters"]) {
    const value = body[field];
    if (value && typeof value === "object" && !Array.isArray(value)) {
      nested[field] = withForget(value as Record<string, unknown>);
    }
  }
  return {
    ...body,
    ...nested,
    extensions: wasString ? JSON.stringify(extensions) : extensions,
  };
}

/**
 * One line on pending deletion, or null when it has finished. Database
 * failures and copies inside older files are retried in the background.
 */
export function purgeStatus(
  purge: PromptPurge | null | undefined,
): string | null {
  if (!purge?.pending) return null;
  if (purge.database_pending)
    return "Prompt deletion hasn't finished. Azizam keeps retrying.";
  if (purge.deadline_repair_pending)
    return "Saved prompts haven't been shortened to the current limits yet. Azizam keeps retrying.";
  const stripping = purge.metadata_stripping;
  const left = stripping?.files_pending;
  const count =
    typeof left === "number" && left > 0
      ? ` ${left} ${left === 1 ? "file" : "files"} left.`
      : "";
  if (stripping?.status === "retrying")
    return `Some older files still contain prompts. Azizam keeps retrying.${count}`;
  return `Removing copies from older files in the background.${count}`;
}

/** The note shown with a new key: whether prompts sent with it are kept. */
export function keyPromptNote(
  promptMemory:
    | { person_id?: string | null; description?: string }
    | null
    | undefined,
): string | null {
  if (!promptMemory) return null;
  if (promptMemory.description) return promptMemory.description;
  return promptMemory.person_id
    ? "This key acts as you. Anyone who has it can see the prompts you saved."
    : "Media prompts sent with this key are not remembered. Requests to /v1/responses with store:true can save the conversation until it is explicitly deleted.";
}

/**
 * A save the gateway kept but could not fully clean up answers 202 with an
 * error-shaped body ("The setting was saved. Older prompt cleanup is pending
 * ..."). It used to answer 503, which the fetch layers threw as an ApiError.
 * Throw it the same way so each screen still shows that message and refetches
 * the saved settings, rather than storing the error body as the settings.
 */
export function rejectPendingCleanup<T>(payload: T): T {
  const error = (
    payload as { error?: { code?: unknown; message?: unknown } } | null
  )?.error;
  if (error && typeof error === "object" && typeof error.message === "string") {
    throw new ApiError(
      typeof error.code === "string" ? error.code : "prompt_retention_pending",
      error.message,
      202,
    );
  }
  return payload;
}

export const householdPromptMemoryApi = {
  get: () =>
    adminFetch<HouseholdPromptMemoryResponse>(
      "/admin/api/settings/prompt-memory",
    ).then(learnContract),
  put: (value: HouseholdPromptMemory, revision: string) =>
    adminFetch<HouseholdPromptMemoryResponse>(
      "/admin/api/settings/prompt-memory",
      {
        method: "PUT",
        body: value,
        headers: { "If-Match": revision },
      },
    )
      .then(rejectPendingCleanup)
      .then(learnContract),
};
