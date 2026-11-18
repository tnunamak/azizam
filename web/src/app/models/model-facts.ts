import type { CatalogRow } from "@/lib/product-api";

const dateFields = [
  ["Released", "released_at"],
  ["Added here", "added_at"],
  ["Updated", "updated_at"],
] as const;

function dateISO(value: unknown): string | null {
  let timestamp: number;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return null;
    timestamp = value > 1e12 ? value : value * 1000;
  } else if (typeof value === "string") {
    // Require a full ISO calendar date. A time must carry its own zone,
    // otherwise displaying it would guess from this browser's local zone.
    const match = /^(\d{4})-(\d{2})-(\d{2})(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,3})?)?(?:Z|[+-]\d{2}:\d{2}))?$/.exec(value);
    if (!match) return null;
    const year = Number(match[1]), month = Number(match[2]), day = Number(match[3]);
    const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
    const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    if (month < 1 || month > 12 || day < 1 || day > days[month - 1]) return null;
    timestamp = Date.parse(value);
  } else {
    return null;
  }
  const date = new Date(timestamp);
  return Number.isFinite(date.getTime()) ? date.toISOString() : null;
}

// CatalogRow is a generated interface without an index signature. Accept it
// directly as well as raw records; do not invent fields in the API contract.
export function modelDates(row: Record<string, unknown> | CatalogRow): Array<{
  label: (typeof dateFields)[number][0];
  iso: string;
}> {
  const facts = row as Record<string, unknown>;
  return dateFields.flatMap(([label, field]) => {
    const iso = dateISO(facts[field]);
    return iso === null ? [] : [{ label, iso }];
  });
}

export function modelSource(row: Record<string, unknown> | CatalogRow): string | null {
  const value = (row as Record<string, unknown>).source_url;
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:" ? value : null;
  } catch {
    return null;
  }
}
