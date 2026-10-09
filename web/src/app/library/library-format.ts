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
 * A readable name from a raw model id: "krea2-turbo" -> "Krea2 Turbo",
 * "tts-1" -> "TTS 1". Short all-letter words read as acronyms.
 */
export function prettifyModelId(id: string): string {
  return id
    .split(/[-_./:\s]+/)
    .filter(Boolean)
    .map((word) =>
      /^[a-z]{1,3}$/i.test(word)
        ? word.toUpperCase()
        : word[0].toUpperCase() + word.slice(1),
    )
    .join(" ");
}

interface CatalogNameRow {
  id: string;
  title?: string | null;
  display_name?: string | null;
}

export interface CapabilityNameRow {
  recommended_model?: string | null;
  use_name?: string | null;
  use_names?: Array<string> | null;
  model?: string | null;
}

/**
 * The name a person reads for a model: the catalog's title, else the
 * recommended name of a capability that answers to this id, else the id made
 * readable. The raw id stays in Details as "Model name".
 */
export function modelDisplayName(
  model: string,
  catalog?: { aliases?: CatalogNameRow[]; models?: CatalogNameRow[] },
  capabilities: CapabilityNameRow[] = [],
): string {
  const row = [...(catalog?.aliases ?? []), ...(catalog?.models ?? [])].find(
    (item) => item.id === model,
  );
  const titled = [row?.title, row?.display_name].find(
    (name) => name && name.trim() && name !== model,
  );
  if (titled) return titled;
  const capability = capabilities.find(
    (item) =>
      item.recommended_model &&
      [item.use_name, item.model, ...(item.use_names ?? [])].includes(model),
  );
  if (capability?.recommended_model) return capability.recommended_model;
  return prettifyModelId(model);
}

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
 * ripe tomatoes"). The caption truncates; the full prompt is in the viewer.
 */
export function captionTitle(
  prompt: string | null | undefined,
  fallback: string,
): string {
  const sentence = (prompt ?? "").trim().split(/(?<=[.!?])\s/)[0] ?? "";
  const words = sentence
    .replace(/[.!?]$/, "")
    .replace(
      /^(?:(?:a|an|the)\s+)?(?:(?:[\w-]+\s+){0,2}(?:photo(?:graph)?|picture|image|shot|illustration|painting|render(?:ing)?|portrait|scene)\s+of\s+)?(?:(?:a|an|the)\s+)?/i,
      "",
    )
    .trim();
  return words ? words[0].toUpperCase() + words.slice(1) : fallback;
}
