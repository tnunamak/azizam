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
import { adminFetch } from "./api";

export type Retention = "never" | "1_year" | "3_months";

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
  prompt_purge?: PromptPurge;
}

export interface HouseholdPromptMemoryResponse {
  prompt_memory: HouseholdPromptMemory;
  revision: string;
  prompt_purge?: PromptPurge;
}

export interface DeletePromptsResponse {
  deleted_prompts: number;
  prompt_purge?: PromptPurge;
}

/** The react-query key for the signed-in person's prompt memory. */
export const promptMemoryKey = ["account", "prompt-memory"] as const;

/** Shortest first, so a cap can be compared by position. */
const ORDER: Retention[] = ["3_months", "1_year", "never"];

export const RETENTIONS: Retention[] = ["never", "1_year", "3_months"];

/** The member's choice: "Forget prompts after" … */
export const memberRetentionLabel: Record<Retention, string> = {
  never: "Never",
  "1_year": "1 year",
  "3_months": "3 months",
};

/** The owner's cap: "Keep prompts for" … */
export const capLabel: Record<Retention, string> = {
  never: "No limit",
  "1_year": "1 year",
  "3_months": "3 months",
};

export function isRetention(value: unknown): value is Retention {
  return value === "never" || value === "1_year" || value === "3_months";
}

/** True when `next` keeps prompts for less time than `current`. */
export function isShorter(next: Retention, current: Retention): boolean {
  return ORDER.indexOf(next) < ORDER.indexOf(current);
}

/** A member may choose any period no longer than the household's cap. */
export function withinCap(value: Retention, cap: Retention): boolean {
  return ORDER.indexOf(value) <= ORDER.indexOf(cap);
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
    return "Azizam can remember what you type so you can find things again. Only you can see saved prompts. Change this in Your settings.";
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
  if (state === "member-off") return "You turned it off.";
  if (state === "household-off")
    return owner
      ? "You turned it off for everyone."
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
    return "Deletion pending. Azizam keeps retrying saved prompt deletion in the background.";
  const stripping = purge.metadata_stripping;
  const left = stripping?.files_pending;
  const count =
    typeof left === "number" && left > 0
      ? ` ${left} ${left === 1 ? "file" : "files"} left.`
      : "";
  if (stripping?.status === "retrying")
    return `Some older files still hold copies. Azizam keeps retrying in the background.${count}`;
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
    : "Media prompts sent with this key are not remembered. Requests to /v1/responses with store:true can save the conversation for up to seven days.";
}

export const householdPromptMemoryApi = {
  get: () =>
    adminFetch<HouseholdPromptMemoryResponse>(
      "/admin/api/settings/prompt-memory",
    ),
  put: (value: HouseholdPromptMemory, revision: string) =>
    adminFetch<HouseholdPromptMemoryResponse>(
      "/admin/api/settings/prompt-memory",
      {
        method: "PUT",
        body: value,
        headers: { "If-Match": revision },
      },
    ),
};
