/**
 * Request history: what the person's apps sent and what came back, as the gateway keeps it
 * (docs/design/REQUEST_RETENTION.md sections 3.4, 8, 10 and 13.3). The History page, the member's
 * settings, the Home notice and Try's Incognito switch share these shapes and words, so none of
 * them says something different.
 */
import { formatTimestamp } from "@/lib/format";

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
  /** True until a member who predates request history turns it on or off, or dismisses the notice. */
  notice_due: boolean;
}

export interface RequestSummary {
  route?: string | null;
  model?: string | null;
  status?: number | null;
  latency_ms?: number | null;
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

/** The route, without its query, as the list shows it under the model. */
export function routeWords(route: string | null | undefined): string {
  return route ? route.replace(/\?.*$/, "") : "";
}

const NOT_KEPT_REASON: Record<string, string> = {
  server_disabled: "Azizam doesn’t keep request history on this gateway.",
  request_forget: "This request was sent as Incognito.",
  no_person: "No one was signed in, so there was no history to keep it in.",
  member_disabled: "Request history was off for you.",
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

/** The time a row shows, in the same words as the rest of the app. */
export function whenWords(seconds: number): string {
  return formatTimestamp(seconds).children;
}
