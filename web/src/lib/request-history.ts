/**
 * Request history: what the person's apps sent and what came back, as the gateway keeps it
 * (docs/design/REQUEST_RETENTION.md sections 3.4, 8, 10 and 13.3). The History page, the member's
 * settings, the Home notice and Try's keep-request-history switch share these shapes and words, so none of
 * them says something different.
 */
/** The member's choice, as `GET` and `PUT /me/api/retention` answer. */
export interface RequestHistoryChoice {
  keep: boolean;
  /** A period id from the gateway's data, such as "7_days". */
  retention: string;
  /** The parts kept: "accepted" is what was sent, "received" is what came back. */
  parts: string[];
  /** The periods the gateway offers, shortest first. */
  choices: string[];
  /** The parts the gateway offers. */
  allowed_parts: string[];
  /**
   * True when the owner has turned request history off for everyone. Nothing new is kept then, whatever the member
   * chose, so the screens offer no way to turn it on (REQUEST_RETENTION.md section 17, slice 5b1 r2).
   */
  household_off: boolean;
  /** True until a member who predates request history turns it on or off, or dismisses the notice. */
  notice_due: boolean;
}

export interface RequestSummary {
  route?: string | null;
  model?: string | null;
  status?: number | null;
  latency_ms?: number | null;
  /** What the request asked, in one line, cut when it was kept. Absent on rows kept before the preview existed. */
  preview?: string | null;
}

/** One row of the list. Only metadata: the content is read one request at a time. */
export interface RequestHistoryItem {
  record_id: string;
  request_id: string | null;
  kind: string;
  /** Unix seconds. */
  created_at: number;
  /** Unix seconds, or null when nothing ends the keeping. */
  kept_until: number | null;
  origin_kind: string | null;
  /** The app the request came through, or null when it came from the browser. */
  app_name: string | null;
  kept_parts: string[];
  summary: RequestSummary;
}

export interface RequestHistoryPage {
  requests: RequestHistoryItem[];
  next_cursor: string | null;
}

/** One kept request, with its content as the gateway kept it. */
export interface RequestHistoryRecord {
  record_id: string;
  kind: string;
  created_at: number;
  kept_until: number | null;
  origin_kind: string | null;
  request_id: string | null;
  /** `accepted` is what was sent; `received` is what came back, when it was kept. */
  parts: { accepted?: unknown; received?: unknown };
  omissions: string[];
  summary: RequestSummary;
}

/** One entry of a receipt's `retention` list (receipts.py `validate_retention`). */
export interface ReceiptRetentionEntry {
  kind: string;
  decision: "kept" | "not_kept" | "failed";
  reason: string;
  kept_until_ms?: number | null;
  parts?: string[];
  limited_by?: string;
}

export const REQUEST_HISTORY_PAGE_SIZE = 20;

/** The react-query key for the signed-in person's choice and its notice. */
export const requestHistoryKey = ["account", "request-history"] as const;

/** The react-query key for the person's kept requests. */
export const requestHistoryListKey = ["account", "request-history", "list"] as const;

/** "7_days" reads "7 days". The words are the gateway's ids, spelled out; the screens add no others. */
export function periodWords(id: string): string {
  return id.replaceAll("_", " ");
}

/** The parts a choice keeps, in words. */
export function partsWords(parts: string[]): string {
  return parts.includes("received") ? "the request and its reply" : "the request";
}

/** The HTTP status, in words: "OK" for 2xx, "Failed (500)" otherwise, a dash when none was kept. */
export function statusWords(status: number | null | undefined): string {
  if (typeof status !== "number") return "—";
  return status >= 200 && status < 300 ? "OK" : `Failed (${status})`;
}

/** The route, without its query. It is for Technical details and the route's kind, not for a reader's line. */
export function routeWords(route: string | null | undefined): string {
  return route ? route.replace(/\?.*$/, "") : "";
}

/**
 * How a request's record reads: a conversation, a picture, sound or video, a transcription, embeddings, or a request
 * whose shape the screens do not know. The label is what the screens call the kind when nothing else names it.
 */
export type RequestShape = "conversation" | "media" | "transcription" | "embeddings" | "other";

export interface RequestKind {
  shape: RequestShape;
  label: string;
}

/** The kind of a route, from its path (the gateway's routes for chat, pictures, sound, video, transcription, embeddings). */
export function requestKind(route: string | null | undefined): RequestKind {
  const path = routeWords(route).toLowerCase();
  if (/\/(chat\/completions|responses|messages|api\/chat)$/.test(path)) return { shape: "conversation", label: "Chat" };
  if (/\/(embeddings|api\/embed)$/.test(path)) return { shape: "embeddings", label: "Document search" };
  if (/\/audio\/(transcriptions|translations)$/.test(path)) return { shape: "transcription", label: "Transcription" };
  if (/\/images\//.test(path)) return { shape: "media", label: "Picture" };
  if (/\/videos(\/|$)/.test(path)) return { shape: "media", label: "Video" };
  if (/\/audio\/(speech|generate)$/.test(path)) return { shape: "media", label: "Audio" };
  return { shape: "other", label: "Request" };
}

/** The markers the gateway writes in place of a removed value (request_history.py REMOVED and URL_REMOVED). */
const REMOVED_PART = /\[(?:url )?removed\]/;

/** True when a kept value, at any depth, holds a removed part: a credential, or a part of a link. */
export function hasRemovedPart(value: unknown): boolean {
  if (typeof value === "string") return REMOVED_PART.test(value);
  if (Array.isArray(value)) return value.some(hasRemovedPart);
  if (value && typeof value === "object") return Object.values(value).some(hasRemovedPart);
  return false;
}

/** The text with each removed marker taken out, so no reader sees "[removed]" inside a sentence. */
export function withoutRemovedParts(text: string): string {
  return text.replace(/[ \t]*\[(?:url )?removed\][ \t]*/g, " ").replace(/[ \t]+$/gm, "");
}

/** A byte count in words: "512 bytes", "2.1 MB". */
export function sizeWords(bytes: number): string {
  if (bytes < 1024) return `${bytes} bytes`;
  let value = bytes / 1024;
  let unit = 0;
  const units = ["KB", "MB", "GB"];
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

const NOT_KEPT_REASON: Record<string, string> = {
  server_disabled: "Azizam doesn’t keep request history on this gateway.",
  request_forget: "Keep request history was off for this request.",
  no_person: "No one was signed in, so there was no history to keep it in.",
  member_disabled: "Request history was off for you.",
  app_off: "Not kept: request history is off for this App.",
  app_disabled: "Not kept: this App, or the person who manages it, is turned off.",
  no_scope: "The app had no stable scope to keep it under.",
  no_content: "There was no content to keep.",
  storage_unavailable: "Storage wasn’t available.",
  erasure_cutoff: "Your history was deleted while this request ran.",
  expired: "Its keep time ran out, so it was deleted.",
  write_failed: "Saving it failed.",
  incomplete: "The request didn’t finish, so nothing was kept.",
};

const LIMITED_BY: Record<string, string> = {
  owner_cap: "The gateway limit is shorter than your choice.",
  prior_policy: "An older setting limited how long it is kept.",
};

/**
 * What the receipt says was kept, and why, in one or two sentences. Null when the receipt has no entry
 * for request history.
 */
export function keptSentence(
  entry: ReceiptRetentionEntry | null | undefined,
): string | null {
  if (!entry || entry.kind !== "request_history") return null;
  if (entry.decision === "kept") {
    const until =
      typeof entry.kept_until_ms === "number"
        ? ` until ${new Date(entry.kept_until_ms).toLocaleString(undefined, {
            dateStyle: "medium",
            timeStyle: "short",
          })}`
        : " until you delete it";
    const noun = partsWords(entry.parts ?? ["accepted"]);
    const limit = entry.limited_by ? LIMITED_BY[entry.limited_by] : undefined;
    return `Kept ${noun}${until}.${limit ? ` ${limit}` : ""}`;
  }
  if (entry.decision === "failed") return "Not kept. Saving it failed.";
  return `Not kept. ${NOT_KEPT_REASON[entry.reason] ?? "Nothing was kept for a reason this page doesn’t show."}`;
}
