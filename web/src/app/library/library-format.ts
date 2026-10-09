/**
 * Pure helpers for the Library page: tile dates, model display names and
 * in-progress work read from the home payload. Kept out of page.tsx so they
 * can be tested without rendering the page.
 */

const DAY_MS = 86_400_000;

function startOfDay(date: Date): number {
  return new Date(
    date.getFullYear(),
    date.getMonth(),
    date.getDate(),
  ).getTime();
}

/**
 * A creation's date as its tile caption says it: "Today", "Yesterday", the
 * weekday within the last week, then "Oct 12", with the year only when it is
 * not the current year. One continuous grid with dated captions replaces week
 * groups (D1.3: a part-filled first group read as a gap).
 */
export function tileDate(date: Date | null, now: Date): string {
  if (!date) return "Undated";
  const days = Math.round((startOfDay(now) - startOfDay(date)) / DAY_MS);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return date.toLocaleDateString("en-US", { weekday: "long" });
  return date.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    ...(date.getFullYear() === now.getFullYear() ? {} : { year: "numeric" }),
  });
}

/**
 * The day label for each creation in a newest-first list, on the first
 * creation of each day only ("Today", "Yesterday", "Monday", "Oct 12");
 * null elsewhere. D1.6: date groups broke the grid into part-filled rows
 * (round 6: "Today and Yesterday rows are mostly blank"), so the grid stays
 * continuous and the day rides on its first tile, as Apple Photos' All
 * Photos scroll labels its days without breaking rows.
 */
export function dayMarks(dates: (Date | null)[], now: Date): (string | null)[] {
  let previous: string | undefined;
  return dates.map((date) => {
    const label = tileDate(date, now);
    if (label === previous) return null;
    previous = label;
    return label;
  });
}

export {
  modelDisplayName,
  prettifyModelId,
  type CapabilityNameRow,
} from "@/lib/model-name";

export type InProgressKind = "image" | "video" | "audio";

export interface InProgressReceipt {
  request_id: string;
  status?: string | null;
  intent?: string | null;
  path?: string | null;
  accepted_at_ms?: number | null;
  _stored_at?: number | null;
}

export interface InProgressItem {
  id: string;
  kind: InProgressKind;
  /** Epoch seconds, or null when the receipt has no start time. */
  startedAt: number | null;
}

const RUNNING = new Set([
  "running",
  "accepted",
  "queued",
  "in_progress",
  "pending",
]);

function receiptKind(receipt: InProgressReceipt): InProgressKind | null {
  const intent = receipt.intent ?? "";
  const path = receipt.path ?? "";
  if (intent.startsWith("image") || path.includes("/images/")) return "image";
  if (intent.startsWith("video") || path.includes("/videos")) return "video";
  if (intent.startsWith("audio") || path.includes("/audio/")) return "audio";
  return null;
}

/** Media requests the gateway has accepted but not finished. */
export function inProgressMedia(
  receipts: InProgressReceipt[] | null | undefined,
): InProgressItem[] {
  const items: InProgressItem[] = [];
  for (const receipt of receipts ?? []) {
    if (!RUNNING.has((receipt.status ?? "").toLowerCase())) continue;
    const kind = receiptKind(receipt);
    if (!kind) continue;
    items.push({
      id: receipt.request_id,
      kind,
      startedAt: receipt.accepted_at_ms
        ? receipt.accepted_at_ms / 1000
        : (receipt._stored_at ?? null),
    });
  }
  return items;
}

export const MAKING_COPY: Record<InProgressKind, string> = {
  image: "Making your picture",
  video: "Making your video",
  audio: "Making your audio",
};

/**
 * A tile caption from a prompt: its first sentence without the framing a
 * prompt opens with ("An overhead photograph of a …", "A …"), so the first
 * words are the ones that tell creations apart ("Raised garden bed with
 * ripe tomatoes"), cut at a whole word within `max` characters. Library
 * shows it on hover or focus; the full prompt is in the viewer.
 */
export function captionTitle(
  prompt: string | null | undefined,
  fallback: string,
  max = 40,
): string {
  const sentence = (prompt ?? "").trim().split(/(?<=[.!?])\s/)[0] ?? "";
  const words = sentence
    .replace(/[.!?]$/, "")
    .replace(
      /^(?:(?:a|an|the)\s+)?(?:(?:[\w-]+\s+){0,2}(?:photo(?:graph)?|picture|image|shot|illustration|painting|render(?:ing)?|portrait|scene)\s+of\s+)?(?:(?:a|an|the)\s+)?/i,
      "",
    )
    .trim();
  if (!words) return fallback;
  // Stop at a whole word within `max` characters, never on a joining word,
  // so a short title never ends mid-word.
  const space = words.lastIndexOf(" ", max);
  const short =
    words.length <= max
      ? words
      : `${words
          .slice(0, space > 0 ? space : max)
          // Never end on a joining word ("… on a…").
          .replace(
            /(?:\s+(?:a|an|the|and|or|but|to|of|in|on|at|by|for|with|from))+$/i,
            "",
          )
          .replace(/[,;:]$/, "")}…`;
  return short[0].toUpperCase() + short.slice(1);
}
